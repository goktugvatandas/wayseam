# SPDX-License-Identifier: MIT
"""Launch a Windows app in the guest's interactive session and present its HWND."""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from wayseam.apps import AppInfo
from wayseam.config import Config
from wayseam.guest.agent import AgentClient
from wayseam.paths import runtime_dir
from wayseam.present.audio import ensure_audio_bridge


@dataclass(frozen=True)
class WayseamSession:
    process: subprocess.Popen[bytes]
    hwnd: int
    guest_pid: int
    pid_file: Path
    stderr_log: Path


class WindowAlreadyPresented(RuntimeError):
    """Another presenter already owns (or is starting for) this HWND."""


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A presenter the watcher has not reaped yet is a zombie: gone for every
    # purpose that matters here (it holds no window), so its claim is stale.
    try:
        status = Path(f"/proc/{pid}/status").read_text()
    except OSError:
        return True
    for line in status.splitlines():
        if line.startswith("State:"):
            return not line.split()[1].startswith("Z")
    return True


def claim_hwnd(rd: Path, hwnd: int, *, in_flight_seconds: float = 15.0) -> Path:
    """Atomically claim the right to present ``hwnd``; raise if taken.

    ``wayseam app run`` creates the guest window seconds before it starts a
    presenter, and the window watcher polls the guest meanwhile — without a
    claim both spawn a presenter for the same HWND and the two fight over
    the tile and guest placement. The claim file is created O_EXCL and holds
    the presenter PID once spawned; an empty, recent claim is a spawn in
    flight; a claim whose PID is dead is stale and gets reclaimed.
    """
    claim = rd / f"wayseam-hwnd-{hwnd:x}.claim"
    for _ in range(2):
        try:
            fd = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                text = claim.read_text(encoding="ascii").strip()
                age = time.time() - claim.stat().st_mtime
            except OSError:
                continue
            pid = int(text) if text.isdigit() else 0
            if pid and _pid_alive(pid):
                raise WindowAlreadyPresented(
                    f"{hex(hwnd)} is already presented by pid {pid}"
                ) from None
            if not pid and age < in_flight_seconds:
                raise WindowAlreadyPresented(
                    f"a presenter for {hex(hwnd)} is already starting"
                ) from None
            claim.unlink(missing_ok=True)
            continue
        os.close(fd)
        return claim
    raise WindowAlreadyPresented(f"could not claim {hex(hwnd)}")


def application_id(slug: str) -> str:
    """Return a stable, valid Wayland application id for one app slug."""
    component = re.sub(r"[^A-Za-z0-9_]", "_", slug)
    if not component or component[0].isdigit():
        component = f"app_{component}"
    return f"org.wayseam.App.{component}"


def _ps_b64(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def _guest_launch_script(app: AppInfo, file_path: str | None) -> str:
    if file_path:
        raise RuntimeError("Wayseam file launches are not implemented yet")
    exe_b64 = _ps_b64(app.executable)
    args_b64 = _ps_b64(app.args or "")
    aumid_b64 = _ps_b64(app.launch_uri or "")
    # Parameters are base64-decoded inside PowerShell instead of interpolated
    # as source. App discovery is guest-controlled input, so quoting alone is
    # not an adequate command-injection boundary.
    return rf"""
$ErrorActionPreference = "Stop"
$exe = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String("{exe_b64}"))
$argText = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String("{args_b64}"))
$aumid = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String("{aumid_b64}"))
$session = (Get-Process -Id $PID).SessionId
if (-not ("WayseamLaunchWindow" -as [type])) {{
    Add-Type -TypeDefinition @'
using System;
using System.Text;
using System.Runtime.InteropServices;
public static class WayseamLaunchWindow {{
    public delegate bool EnumProc(IntPtr hwnd, IntPtr state);
    [StructLayout(LayoutKind.Sequential)]
    public struct RECT {{ public int Left, Top, Right, Bottom; }}
    [DllImport("user32.dll")]
    private static extern bool EnumWindows(EnumProc callback, IntPtr state);
    [DllImport("user32.dll")]
    private static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
    [DllImport("user32.dll")]
    private static extern bool IsWindowVisible(IntPtr hwnd);
    [DllImport("user32.dll")]
    private static extern bool GetWindowRect(IntPtr hwnd, out RECT rect);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)]
    private static extern int GetWindowText(IntPtr hwnd, StringBuilder text, int count);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)]
    private static extern int GetWindowTextLength(IntPtr hwnd);

    [DllImport("user32.dll", CharSet=CharSet.Unicode)]
    private static extern int GetClassName(IntPtr hwnd, StringBuilder text, int count);
    public static string ClassName(IntPtr hwnd) {{
        StringBuilder text = new StringBuilder(256);
        GetClassName(hwnd, text, text.Capacity);
        return text.ToString();
    }}
    // Shell surfaces (desktop, taskbar) are never "the app's window". For
    // explorer.exe — always running as the shell — only folder windows count,
    // otherwise launching File Explorer "reuses" the desktop itself.
    public static bool IsAppWindow(IntPtr hwnd, bool explorerProcess) {{
        string cls = ClassName(hwnd).ToLowerInvariant();
        if (cls == "progman" || cls == "workerw" || cls == "shell_traywnd" || cls == "shell_secondarytraywnd") return false;
        if (explorerProcess) return cls == "cabinetwclass" || cls == "explorewclass";
        return true;
    }}
    public delegate bool ChildProc(IntPtr hwnd, IntPtr state);
    [DllImport("user32.dll")]
    private static extern bool EnumChildWindows(IntPtr parent, ChildProc callback, IntPtr state);
    // UWP apps: the visible window is an ApplicationFrameHost frame whose
    // Windows.UI.Core.CoreWindow child belongs to the app's process.
    public static IntPtr FindHostedFrame(uint wantedPid) {{
        IntPtr found = IntPtr.Zero;
        EnumWindows(delegate(IntPtr frame, IntPtr state) {{
            if (!IsWindowVisible(frame) || ClassName(frame) != "ApplicationFrameWindow") return true;
            bool hosts = false;
            EnumChildWindows(frame, delegate(IntPtr child, IntPtr s2) {{
                uint pid; GetWindowThreadProcessId(child, out pid);
                if (pid == wantedPid && ClassName(child) == "Windows.UI.Core.CoreWindow") {{ hosts = true; return false; }}
                return true;
            }}, IntPtr.Zero);
            if (hosts) {{ found = frame; return false; }}
            return true;
        }}, IntPtr.Zero);
        return found;
    }}
    public static IntPtr FindVisible(uint wantedPid, bool explorerProcess) {{
        IntPtr found = IntPtr.Zero;
        long bestArea = 0;
        EnumWindows(delegate(IntPtr hwnd, IntPtr state) {{
            uint pid;
            RECT rect;
            GetWindowThreadProcessId(hwnd, out pid);
            if (pid != wantedPid || !IsWindowVisible(hwnd) || !GetWindowRect(hwnd, out rect) ||
                rect.Right - rect.Left < 64 || rect.Bottom - rect.Top < 64 ||
                !IsAppWindow(hwnd, explorerProcess)) {{
                return true;
            }}
            long area = (long)(rect.Right - rect.Left) * (rect.Bottom - rect.Top);
            if (area > bestArea || (area == bestArea && GetWindowTextLength(hwnd) > 0)) {{
                found = hwnd;
                bestArea = area;
            }}
            return true;
        }}, IntPtr.Zero);
        if (found == IntPtr.Zero) found = FindHostedFrame(wantedPid);
        return found;
    }}

    public static string Title(IntPtr hwnd) {{
        StringBuilder text = new StringBuilder(512);
        GetWindowText(hwnd, text, text.Capacity);
        return text.ToString();
    }}
}}
'@
}}
if (-not [string]::IsNullOrWhiteSpace($aumid)) {{
    $aumidParts = $aumid.Split('!', 2)
    if ($aumidParts.Count -ne 2) {{
        Write-Error "packaged application has an invalid AUMID"
        exit 2
    }}
    $family = $aumidParts[0]
    $appId = $aumidParts[1]
    $package = Get-AppxPackage -ErrorAction SilentlyContinue | Where-Object {{
        $_.PackageFamilyName -ieq $family
    }} | Sort-Object Version -Descending | Select-Object -First 1
    if ($null -eq $package) {{
        Write-Error "packaged application is not installed"
        exit 2
    }}
    $manifest = Get-AppxPackageManifest -Package $package
    $manifestApp = @($manifest.Package.Applications.Application) | Where-Object {{
        $_.Id -ieq $appId
    }} | Select-Object -First 1
    $relativeExe = [string]$manifestApp.Executable
    if ([string]::IsNullOrWhiteSpace($relativeExe)) {{
        Write-Error "packaged application has no executable"
        exit 2
    }}
    $exe = Join-Path ([string]$package.InstallLocation) $relativeExe
}}
function Find-WayseamAppWindow {{
    foreach ($candidate in @(Get-Process -ErrorAction SilentlyContinue | Where-Object {{
        $_.SessionId -eq $session
    }})) {{
        try {{
            if ($candidate.Path -ieq $exe) {{
                $isExplorer = [bool]($candidate.Path -like '*\explorer.exe')
                $handle = [WayseamLaunchWindow]::FindVisible([uint32]$candidate.Id, $isExplorer)
                if ($handle -ne [IntPtr]::Zero) {{
                    return [pscustomobject]@{{
                        Process = $candidate
                        Handle = $handle
                        Title = [WayseamLaunchWindow]::Title($handle)
                    }}
                }}
            }}
        }} catch {{ }}
    }}
    return $null
}}
$app = Find-WayseamAppWindow
if ($null -eq $app) {{
    if (-not [string]::IsNullOrWhiteSpace($aumid)) {{
        Start-Process -FilePath "explorer.exe" -ArgumentList "shell:AppsFolder\$aumid"
    }} elseif ([string]::IsNullOrWhiteSpace($argText)) {{
        Start-Process -FilePath $exe
    }} else {{
        Start-Process -FilePath $exe -ArgumentList $argText
    }}
}}
$requiredStable = if ([string]::IsNullOrWhiteSpace($aumid)) {{ 1 }} else {{ 4 }}
$stableHandle = [IntPtr]::Zero
$stableCount = 0
$app = $null
for ($attempt = 0; $attempt -lt 120; $attempt++) {{
    Start-Sleep -Milliseconds 250
    $candidate = Find-WayseamAppWindow
    if ($null -eq $candidate) {{
        $stableHandle = [IntPtr]::Zero
        $stableCount = 0
        continue
    }}
    if ($candidate.Handle -eq $stableHandle) {{
        $stableCount++
    }} else {{
        $stableHandle = $candidate.Handle
        $stableCount = 1
    }}
    if ($stableCount -ge $requiredStable) {{
        $app = $candidate
        break
    }}
}}
if ($null -eq $app) {{
    Write-Error "application did not expose a top-level window"
    exit 2
}}
[pscustomobject]@{{
    pid = [int]$app.Process.Id
    hwnd = [int64]$app.Handle
    title = [string]$app.Title
}} | ConvertTo-Json -Compress
""".strip()


def _launch_guest_window(
    client: AgentClient, app: AppInfo, file_path: str | None
) -> dict[str, object]:
    result = client.exec(_guest_launch_script(app, file_path), timeout=40)
    if not result.ok:
        detail = (result.stderr or result.stdout or "guest launch failed").strip()
        raise RuntimeError(detail[:500])
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    try:
        payload = json.loads(lines[-1])
        hwnd = int(payload["hwnd"])
        pid = int(payload["pid"])
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("guest launch returned invalid window metadata") from exc
    if hwnd <= 0 or pid <= 0:
        raise RuntimeError("guest launch returned invalid window metadata")
    return {"hwnd": hwnd, "pid": pid, "title": str(payload.get("title", ""))}


def _python_with_gtk() -> str:
    system_python = Path("/usr/bin/python")
    return str(system_python) if system_python.is_file() else sys.executable


def launch_wayseam_app(
    cfg: Config,
    app: AppInfo,
    *,
    file_path: str | None = None,
    wait: bool = False,
    max_fps: int = 60,
) -> WayseamSession | None:
    """Launch or reuse the guest HWND, then start its native Wayseam presenter.

    Returns ``None`` when a presenter for the window already exists.
    """
    metadata = _launch_guest_window(AgentClient(cfg), app, file_path)
    from wayseam.present.watch import ensure_window_watcher

    try:
        session = present_wayseam_window(
            cfg,
            app,
            hwnd=int(metadata["hwnd"]),
            guest_pid=int(metadata["pid"]),
            title=str(metadata.get("title") or app.full_name),
            max_fps=max_fps,
        )
    except WindowAlreadyPresented:
        # The watcher beat us to this window (it polls the guest while the
        # app was starting). One presenter is exactly what we wanted.
        ensure_window_watcher()
        return None
    ensure_window_watcher()
    if wait:
        session.process.wait()
        session.pid_file.unlink(missing_ok=True)
    return session


def present_wayseam_window(
    cfg: Config,
    app: AppInfo,
    *,
    hwnd: int,
    guest_pid: int,
    title: str,
    max_fps: int = 60,
    automatic: bool = False,
) -> WayseamSession:
    """Start one presenter for an already-identified guest root HWND."""
    if hwnd <= 0 or guest_pid <= 0:
        raise ValueError("presenter requires positive hwnd and guest_pid")

    # Dockur exposes one VM-wide PCM stream. The singleton bridge is shared by
    # every Wayseam surface, so apps get sound without per-profile settings.
    ensure_audio_bridge()

    rd = runtime_dir()
    rd.mkdir(parents=True, exist_ok=True)
    claim = claim_hwnd(rd, hwnd)
    suffix = f"auto-{hwnd:x}" if automatic else app.name
    pid_file = rd / f"wayseam-{suffix}.pid"
    stderr_log = rd / f"wayseam-{suffix}.log"
    cmd = [
        _python_with_gtk(),
        "-m",
        "wayseam.present.window",
        "--hwnd",
        hex(hwnd),
        "--title",
        title,
        "--max-fps",
        str(max_fps),
        "--app-id",
        application_id(app.name),
        "--icon-name",
        f"wayseam-{app.name}",
    ]
    env = os.environ.copy()
    # The presenter runs under the system interpreter (for GTK bindings), so
    # make this source tree importable even when the caller is a venv or a
    # process that never received the launcher wrapper's PYTHONPATH.
    source_root = str(Path(__file__).resolve().parents[2])
    inherited = env.get("PYTHONPATH", "")
    if source_root not in inherited.split(os.pathsep):
        env["PYTHONPATH"] = source_root + (os.pathsep + inherited if inherited else "")
    log_stream = stderr_log.open("ab", buffering=0)
    try:
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log_stream,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )
    except Exception:
        claim.unlink(missing_ok=True)
        raise
    finally:
        log_stream.close()
    pid_file.write_text(f"{process.pid}\n", encoding="ascii")
    claim.write_text(f"{process.pid}\n", encoding="ascii")
    time.sleep(0.25)
    rc = process.poll()
    if rc is not None:
        pid_file.unlink(missing_ok=True)
        claim.unlink(missing_ok=True)
        raise RuntimeError(f"Wayseam presenter exited with code {rc}; see {stderr_log}")
    return WayseamSession(process, hwnd, guest_pid, pid_file, stderr_log)
