# SPDX-License-Identifier: MIT
"""Sanity checks for guest/oem/agent/agent.ps1 (Phase 2).

The .ps1 script runs inside Windows, so we cannot exercise it from CI.
We instead pin the invariants the host code depends on:

- pwsh AST parse (skipped when pwsh isn't on PATH; CI has it).
- The literal markers Phase 1 + Phase 2 promise to ship: HttpListener,
  Prefix, /health, Wait-Token (Phase 1) and Test-Auth, /exec, 401,
  Bearer, base64 decoding (Phase 2).
- The bind prefix is loopback only (127.0.0.1, never 0.0.0.0 / `+` / `*`).
  Anti-goal of v0.2.2.x design: a non-loopback bind would expose the
  agent on the QEMU NAT, breaking the threat model.
- /health stays no-auth (anti-goal: don't auth-protect the readiness
  signal). The test asserts the dispatch shape: /health is matched
  before any Test-Auth call.
- The token is never logged or echoed back. The /exec script content
  lands in agent.log only as a SHA256 hash, never the raw payload.
- Wait-Token / Read-Token never raise. anti-goal #6: throwing kills
  the process and HKCU\\Run does not respawn it.
- Brace balance — guards against partial edits leaving the file
  half-parsed in the absence of pwsh on the dev box.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENT_PS1 = REPO_ROOT / "guest" / "oem" / "agent" / "agent.ps1"
WGC_CS = REPO_ROOT / "guest" / "oem" / "agent" / "wayseam_wgc.cs"


@pytest.fixture(scope="module")
def agent_source() -> str:
    assert AGENT_PS1.is_file(), f"agent.ps1 missing at {AGENT_PS1}"
    return AGENT_PS1.read_text(encoding="utf-8")


def _strip_comments(source: str) -> str:
    """Strip `#` line comments only, preserving string literals.

    Tests that need to see tokens which appear inside string literals
    (e.g. `'/health'` in dispatch, `"hash=$x"` in log lines) use this.
    """
    return "\n".join(
        (raw if (idx := raw.find("#")) == -1 else raw[:idx]) for raw in source.splitlines()
    )


def _strip_comments_and_strings(source: str) -> str:
    """Coarse strip of `#` line comments AND quoted string literals.

    Used by tests that scan for executable tokens (`throw`, function
    calls, log-sink calls) where matches inside a string literal would
    be a false positive. Not a full PowerShell tokenizer — but
    agent.ps1 doesn't use here-strings or backtick-escaped quotes
    inside strings, so this is sufficient.
    """
    cleaned: list[str] = []
    for raw in source.splitlines():
        idx = raw.find("#")
        line = raw if idx == -1 else raw[:idx]
        out_chars: list[str] = []
        in_str: str | None = None
        i = 0
        while i < len(line):
            ch = line[i]
            if in_str is None:
                if ch in ("'", '"'):
                    in_str = ch
                else:
                    out_chars.append(ch)
            else:
                if ch == in_str:
                    in_str = None
            i += 1
        cleaned.append("".join(out_chars))
    return "\n".join(cleaned)


def test_agent_ps1_exists():
    assert AGENT_PS1.is_file()


def test_agent_ps1_pwsh_parse():
    """If pwsh is on PATH, the script must parse without errors."""
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("pwsh not installed on this host")
    cmd = [
        pwsh,
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        (
            "$errors = $null; "
            f"[void][System.Management.Automation.Language.Parser]::ParseFile('{AGENT_PS1}', "
            "[ref]$null, [ref]$errors); "
            "if ($errors -and $errors.Count -gt 0) { "
            "  $errors | ForEach-Object { Write-Error $_.ToString() }; exit 1 "
            "}"
        ),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, (
        f"pwsh parse failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )


def test_agent_ps1_has_phase1_markers(agent_source: str):
    for marker in ("HttpListener", "Prefix", "/health", "Wait-Token"):
        assert marker in agent_source, f"missing required Phase 1 marker: {marker!r}"


def test_agent_ps1_has_phase2_markers(agent_source: str):
    """Phase 2 surface area: auth + /exec must all be wired in."""
    expected = (
        "Test-Auth",
        "/exec",
        "Bearer",
        "Compare-Constant",
        "FromBase64String",
        "401",
        "unauthorized",
    )
    for marker in expected:
        assert marker in agent_source, f"missing required Phase 2 marker: {marker!r}"


def test_agent_ps1_has_wayseam_frame_route(agent_source: str):
    """Wayseam frames are authenticated HWND renders, never desktop crops."""
    for marker in (
        "/wayseam/frame",
        "Get-WayseamWindowFrame",
        "PrintWindow",
        "IsWindowVisible",
        "image/png",
        "invalid_hwnd",
        "window_size_rejected",
        "FrameWorkerBody",
        "ClearLostPopupAlpha",
        "alpha_repaired",
    ):
        assert marker in agent_source

    body = _strip_comments(agent_source)
    auth_call = re.search(r"elseif \(-not \(Test-Auth \$req\)\)", body)
    frame_route = re.search(r"-eq '/wayseam/frame'", body)
    assert auth_call is not None
    assert frame_route is not None
    assert auth_call.start() < frame_route.start(), "frame endpoint must remain behind bearer auth"
    assert ".BeginInvoke()" in body, "frame capture must not block the input accept loop"


def test_wayseam_delta_capture_reuses_buffers_and_scans_only_damaged_rows(
    agent_source: str,
):
    """Large app surfaces must not allocate and scan every pixel while idle."""
    for marker in (
        "CaptureDeltaFrame",
        "CaptureBitmap",
        "CaptureGraphics",
        "CompareMemory",
        "Marshal.Copy(rowPointer, currentRow",
    ):
        assert marker in agent_source

    delta_branch = agent_source.split("if ($encoding -eq 'delta')", 1)[1]
    assert "[WayseamNativeCapture]::CaptureDeltaFrame(" in delta_branch
    assert "$memoryStream = $null" in agent_source
    assert "$stream = $null" not in agent_source


def test_wayseam_delta_capture_prefers_per_hwnd_wgc_with_safe_fallback(
    agent_source: str,
):
    """Interactive frames use WGC first but retain the proven fallback."""
    for marker in (
        "wayseam_wgc.cs",
        "WayseamWgcCapture",
        "Initialize-WayseamWgc",
        "CaptureDeltaFrame",
        "WayseamWgcAvailable",
    ):
        assert marker in agent_source

    delta_branch = agent_source.split("if ($encoding -eq 'delta')", 1)[1]
    wgc_call = delta_branch.index("[WayseamWgcCapture]::CaptureDeltaFrame(")
    fallback_call = delta_branch.index("[WayseamNativeCapture]::CaptureDeltaFrame(")
    assert wgc_call < fallback_call


def test_wayseam_wgc_uses_reported_damage_for_partial_gpu_readback():
    source = WGC_CS.read_text(encoding="utf-8")
    for marker in (
        "DirtyRegionMode = GraphicsCaptureDirtyRegionMode.ReportOnly",
        "frame.DirtyRegions",
        "CopySubresourceRegionDelegate",
        "SourceVersion",
        "MinUpdateInterval = TimeSpan.FromMilliseconds(1)",
    ):
        assert marker in source


def test_agent_ps1_does_not_log_every_wayseam_hot_path_success(agent_source: str):
    """60 FPS frame/cursor polling must not grow agent.log without bound."""
    assert "$routineWayseamSuccess" in agent_source
    assert "'/wayseam/cursor', '/wayseam/windows'" in agent_source
    assert "$sw.ElapsedMilliseconds -ge 50" in agent_source
    assert "$frame.bytes.Length -ge 1048576" in agent_source


def test_agent_ps1_exposes_authenticated_top_level_window_inventory(
    agent_source: str,
):
    for marker in (
        "/wayseam/top-level",
        "TopLevelWindows",
        "process_path",
        "GetWindowThreadProcessId",
        "QueryFullProcessImageName",
        "IsIndependentTopLevel",
    ):
        assert marker in agent_source


def test_wayseam_resize_expands_and_restores_the_guest_canvas(agent_source: str):
    for marker in (
        "EnsureDisplayCanvas",
        "RestoreDisplayCanvas",
        "EnumDisplaySettings",
        "ChangeDisplaySettingsEx",
        "actualWidth",
        "actualHeight",
    ):
        assert marker in agent_source

    assert "[WayseamNativeCapture]::RestoreDisplayCanvas()" in agent_source

    body = _strip_comments(agent_source)
    auth_call = re.search(r"elseif \(-not \(Test-Auth \$req\)\)", body)
    route = re.search(r"-eq '/wayseam/top-level'", body)
    assert auth_call is not None
    assert route is not None
    assert auth_call.start() < route.start()


def test_agent_ps1_has_bounded_wayseam_pointer_route(agent_source: str):
    for marker in (
        "/wayseam/input",
        "invalid_input",
        "SetForegroundWindow",
        "InjectPointer",
        "SendInput",
        "0x2000",
        "input_injection_failed",
    ):
        assert marker in agent_source
    assert "$action -notin @('move','down','up','wheel')" in agent_source


def test_agent_ps1_delivers_bounded_scroll_wheel_events(agent_source: str):
    # Wheel events ride the authenticated pointer route: the pointer is moved
    # first so Windows scrolls the HWND under the host cursor, then raw
    # WHEEL_DELTA units go out as vertical (0x0800) / horizontal (0x1000)
    # SendInput events. Deltas are bounded to 100 notches per request.
    for marker in (
        "InjectWheel",
        "0x0800",
        "0x1000",
        "delta_y",
        "delta_x",
        "[Math]::Abs($deltaY) -gt 12000",
        "$extraLog -like 'action=wheel*'",
    ):
        assert marker in agent_source


def test_agent_ps1_does_not_log_fast_successful_frame_fetches(agent_source: str):
    assert "($path -eq '/wayseam/frame' -and $sw.ElapsedMilliseconds -lt 250)" in agent_source


def test_agent_ps1_exposes_authenticated_clipboard_and_session_routes(
    agent_source: str,
):
    # Both routes sit behind Test-Auth (they appear after the 401 branch) and
    # never use OLE/STA clipboard APIs: raw CF_UNICODETEXT only, bounded to
    # MaxTextChars, and the session route can only reattach the interactive
    # session to the console (tscon /dest:console), nothing else.
    unauthorized = agent_source.index("error = 'unauthorized'")
    for marker in (
        "$path -eq '/wayseam/clipboard'",
        "$path -eq '/wayseam/session'",
    ):
        assert agent_source.index(marker) > unauthorized
    for marker in (
        "CF_UNICODETEXT",
        "GetClipboardSequenceNumber",
        "MaxTextChars = 1048576",
        "invalid_clipboard_text",
        "clipboard_busy",
        "WTSQuerySessionInformation",
        "WTSGetActiveConsoleSessionId",
        "'/dest:console'",
        "invalid_session_action",
        "$action -ne 'console'",
    ):
        assert marker in agent_source
    assert "System.Windows.Forms.Clipboard" not in agent_source


def test_wayseam_pointer_maps_gdk_buttons_to_windows_flags(agent_source: str):
    """GDK uses 1=left, 2=middle, 3=right; Win32 flag values differ."""
    route_match = re.search(
        r"-eq '/wayseam/input'(?P<body>.*?)-eq '/wayseam/resize'",
        agent_source,
        re.DOTALL,
    )
    assert route_match is not None
    route = route_match.group("body")
    assert re.search(
        r"\$button -eq 1.*?0x0002.*?0x0004.*?"
        r"\$button -eq 2.*?0x0020.*?0x0040.*?"
        r"else.*?0x0008.*?0x0010",
        route,
        re.DOTALL,
    )


def test_wayseam_pointer_defers_non_client_window_management_to_wayland(
    agent_source: str,
):
    """Guest chrome may render, but guest move/size gestures must stay inert."""
    for marker in (
        "SendMessageTimeout",
        "HitTestWindow",
        "IsWindowManagementHit",
        "$script:BlockedPointerButtons",
        "managed_by_host",
    ):
        assert marker in agent_source

    route_match = re.search(
        r"-eq '/wayseam/input'(?P<body>.*?)-eq '/wayseam/keyboard'",
        agent_source,
        re.DOTALL,
    )
    assert route_match is not None
    route = route_match.group("body")
    assert "$action -eq 'down' -and $managedByHost" in route
    assert "$action -eq 'up' -and $blockedByHost" in route

    policy_match = re.search(
        r"bool IsWindowManagementHit\(int hitTest\)(?P<body>.*?)\n  \}",
        agent_source,
        re.DOTALL,
    )
    assert policy_match is not None
    policy = policy_match.group("body")
    assert "hitTest == 2" in policy  # HTCAPTION
    assert "hitTest == 9" in policy  # HTMAXBUTTON
    assert "hitTest >= 10 && hitTest <= 18" in policy  # resize borders
    assert "hitTest == 20" not in policy  # HTCLOSE remains functional


def test_agent_ps1_has_bounded_wayseam_resize_route(agent_source: str):
    for marker in (
        "/wayseam/resize",
        "invalid_resize",
        "ShowWindow",
        "MoveWindow",
        "$width -lt 160",
        "$height -lt 120",
        "33554432",
    ):
        assert marker in agent_source
    assert "[WayseamNativeCapture]::ResizeForHost(" in agent_source
    assert "$hwnd, $width, $height" in agent_source


def test_agent_ps1_has_bounded_wayseam_keyboard_route(agent_source: str):
    for marker in (
        "/wayseam/keyboard",
        "InjectVirtualKey",
        "InjectUnicode",
        "invalid_keyboard_input",
        "keyboard_injection_failed",
        "ActivateWindowFamily",
    ):
        assert marker in agent_source


def test_agent_ps1_hides_taskbar_from_wayseam_hit_testing(agent_source: str):
    for marker in (
        "/wayseam/shell",
        "SetShellVisible",
        "Shell_TrayWnd",
        "Shell_SecondaryTrayWnd",
        "invalid_shell_visibility",
    ):
        assert marker in agent_source


def test_agent_ps1_has_authenticated_window_close_route(agent_source: str):
    for marker in (
        "/wayseam/close",
        "CloseWindow",
        "invalid_window_close",
        "window_close_failed",
    ):
        assert marker in agent_source


def test_agent_ps1_reports_closed_frame_hwnd_separately(agent_source: str):
    frame_route = agent_source.split(
        "elseif ($method -eq 'GET' -and $path -eq '/wayseam/frame')", 1
    )[1].split("elseif ($method -eq 'GET' -and $path -eq '/wayseam/windows')", 1)[0]

    assert "[WayseamNativeCapture]::IsWindow" in frame_route
    assert "window_gone" in frame_route
    assert "Send-Json $resp 410" in frame_route


def test_agent_ps1_has_authenticated_owned_window_inventory(agent_source: str):
    for marker in (
        "/wayseam/windows",
        "OwnedWindows",
        "GW_OWNER",
        "invalid_root",
        "class_name",
    ):
        assert marker in agent_source

    body = _strip_comments(agent_source)
    auth_call = re.search(r"elseif \(-not \(Test-Auth \$req\)\)", body)
    route = re.search(r"-eq '/wayseam/windows'", body)
    assert auth_call is not None
    assert route is not None
    assert auth_call.start() < route.start()


def test_agent_ps1_has_authenticated_native_cursor_transport(agent_source: str):
    for marker in (
        "/wayseam/cursor",
        "CaptureCursor",
        "GetCursorInfo",
        "GetIconInfo",
        "DrawIconEx",
        "hot_x",
        "hot_y",
        "invalid_root",
    ):
        assert marker in agent_source

    body = _strip_comments(agent_source)
    auth_call = re.search(r"elseif \(-not \(Test-Auth \$req\)\)", body)
    route = re.search(r"-eq '/wayseam/cursor'", body)
    assert auth_call is not None
    assert route is not None
    assert auth_call.start() < route.start()


def test_agent_ps1_sets_401_status_code(agent_source: str):
    """The 401 path must actually pass 401 to Send-Json (which sets
    StatusCode), not just embed the literal in a comment."""
    assert re.search(r"Send-Json\s+\$resp\s+401\b", agent_source), (
        "no Send-Json call with status code 401 found"
    )


def test_agent_ps1_bind_prefix(agent_source: str):
    """Prefix must be ``http://+:8765/`` (all interfaces, port 8765).

    Why all-interfaces (``+``) and not ``127.0.0.1``: dockur's user-mode
    QEMU NAT delivers forwarded packets to the VM's slirp interface
    (10.0.2.15:8765), NOT to the VM's 127.0.0.1. A 127.0.0.1-only
    listener inside Windows means slirp's forwarded packets hit a
    closed port — kernalix7 saw "Connection reset by peer" on
    2026-04-30 from exactly this. The agent stays externally
    unreachable because compose's ``127.0.0.1:8765:8765/tcp`` mapping
    is host-loopback-only and the QEMU slirp net is private to the
    container.

    Wildcard ``0.0.0.0`` and ``*`` aren't accepted by HttpListener
    syntax for this purpose; ``+`` is the canonical "all interfaces"
    prefix.
    """
    assert "http://+:8765/" in agent_source
    # Mistakes that have shipped before — guard against regression.
    assert "http://127.0.0.1:8765/" not in agent_source
    assert "http://0.0.0.0:" not in agent_source
    assert "http://*:" not in agent_source


def test_agent_ps1_health_is_unauthenticated(agent_source: str):
    """/health must be matched BEFORE the Test-Auth gate so it answers
    even when no token has been delivered. Anti-goal: never auth-protect
    the readiness signal.

    Concretely, the `-eq '/health'` route check must precede the first
    `Test-Auth $req` call site (not the function definition).
    """
    # Comments stripped but string literals preserved — the '/health'
    # path lives inside a single-quoted string in the dispatch, which
    # _strip_comments_and_strings would erase.
    body = _strip_comments(agent_source)
    health_match = re.search(r"-eq\s+'/health'", body)
    # Match a Test-Auth call (followed by `$req`), not the
    # `function Test-Auth(` definition.
    call_match = re.search(r"(?<!function )Test-Auth\s+\$req", body)
    assert health_match is not None, "no /health route found"
    assert call_match is not None, "no Test-Auth call site found"
    assert health_match.start() < call_match.start(), (
        "/health must be dispatched before the Test-Auth gate"
    )


def test_agent_ps1_no_throw_on_missing_token(agent_source: str):
    """Wait-Token / Read-Token must never raise. Anti-goal #6 in
    AGENT_V2_DESIGN: throwing kills the process and HKCU\\Run does
    NOT auto-restart, so a transient missing-token would brick the
    agent until next user logon.

    The only legitimate throw in agent.ps1 is the HttpListener.Start()
    re-throw — that's a fatal binding failure with nothing left to do
    (urlacl missing / port already in use / etc), and the catch block
    writes the error to agent.log first so the user can see WHY.
    """
    stripped = _strip_comments_and_strings(agent_source)

    # Locate the Read-Token + Wait-Token bodies and assert no `throw`.
    for fn_name in ("Read-Token", "Wait-Token"):
        m = re.search(rf"function\s+{fn_name}\s*\{{", stripped)
        assert m is not None, f"function {fn_name} not found"
        # Walk the brace nesting to find the matching close brace.
        depth = 0
        body_start = m.end() - 1  # the `{` itself
        body_end = None
        for i in range(body_start, len(stripped)):
            ch = stripped[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    body_end = i
                    break
        assert body_end is not None, f"unterminated {fn_name} body"
        body = stripped[body_start:body_end]
        assert "throw" not in body, f"{fn_name} must not throw — anti-goal #6 in AGENT_V2_DESIGN"

    # Assert at most one throw in the whole file (the listener-Start fallback).
    assert stripped.count("throw") <= 1, (
        "more throws than expected — Wait-Token / Read-Token / request "
        "loop must all stay catch-and-continue. The single tolerated "
        "throw is the HttpListener.Start() failure re-throw, which is "
        "logged before exit."
    )


def test_agent_ps1_token_not_logged(agent_source: str):
    """`$script:Token` must never appear inside Add-Content / Write-Log
    arguments. The token is the bearer secret; logging it would defeat
    the entire auth model. /exec script payload is logged only by
    SHA256 hash."""
    body = _strip_comments_and_strings(agent_source)
    for line in body.splitlines():
        if "$script:Token" not in line:
            continue
        for sink in ("Add-Content", "Write-Log", "Write-Output", "Write-Host"):
            assert sink not in line, f"token leaked to log sink: {line!r}"


def test_agent_ps1_exec_logs_hash_not_payload(agent_source: str):
    """The /exec handler must compute Get-BytesHash on the decoded
    script and log only that hash — never the raw decoded body."""
    assert "Get-BytesHash" in agent_source, "missing Get-BytesHash helper"
    # The hash reaches Write-Log via the $extraLog channel as a string
    # interpolation `"hash=$($result.hash)"`. Comments stripped, but
    # strings preserved so the interpolation is visible.
    body = _strip_comments(agent_source)
    assert "hash=" in body, "exec hash never reaches the log line"


def test_agent_ps1_braces_balanced(agent_source: str):
    """Curly braces must balance, ignoring strings/comments crudely.

    A partial edit (missing closing brace, stray opening brace) is the most
    common breakage mode for a script we can't run on CI. This is a coarse
    check — the pwsh parse test is the authoritative gate when pwsh is on
    PATH — but it catches the obvious failures on dev boxes without pwsh.
    """
    body = _strip_comments_and_strings(agent_source)
    opens = body.count("{")
    closes = body.count("}")
    assert opens == closes, f"unbalanced braces: {opens} '{{' vs {closes} '}}'"


# --- guest /exec must not lose characters to the console code page ----------
#
# The first attempt at this prepended an encoding preamble to the caller's
# script. That made every /exec unparseable, because PowerShell requires
# param() and [CmdletBinding()] to be the first statement in their file and
# discover_apps.ps1 opens with both. Parsing agent.ps1 alone did not catch it:
# what breaks is the script agent.ps1 ASSEMBLES at runtime. These tests build
# that artifact and parse it.


def _render_launcher(agent_source: str, script_path: str) -> str:
    """Reproduce the launcher agent.ps1 writes, for a given inner script."""
    import re

    m = re.search(r'\$launcher = @"\n(.*?)\n"@', agent_source, re.S)
    assert m, "launcher here-string not found in agent.ps1"
    body = m.group(1)
    # PowerShell here-strings use ` to escape $; the only interpolation left
    # is the script path.
    return body.replace("`$", "$").replace("$tempFile", script_path)


def test_launcher_is_a_separate_file_not_a_preamble(agent_source: str):
    assert "$launchFile = " in agent_source
    assert '-File "' + "' + $launchFile + '" in agent_source
    # The caller's bytes must reach disk untouched.
    assert "[IO.File]::WriteAllBytes($tempFile, $bytes)" in agent_source


def test_exec_child_stdio_is_decoded_as_utf8(agent_source: str):
    assert "$psi.StandardOutputEncoding = New-Object Text.UTF8Encoding $false" in agent_source
    assert "$psi.StandardErrorEncoding  = New-Object Text.UTF8Encoding $false" in agent_source


def test_both_temp_files_are_cleaned_up(agent_source: str):
    assert "foreach ($f in @($tempFile, $launchFile))" in agent_source


def test_assembled_launcher_parses(agent_source: str, tmp_path):
    """Parse the launcher agent.ps1 actually writes — the check that would
    have caught the preamble regression."""
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("pwsh not installed on this host")

    launcher = tmp_path / "launch.ps1"
    launcher.write_text(
        _render_launcher(agent_source, str(tmp_path / "inner.ps1")), encoding="utf-8"
    )

    result = subprocess.run(
        [
            pwsh,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            (
                "$errors = $null; "
                f"[void][System.Management.Automation.Language.Parser]::ParseFile('{launcher}', "
                "[ref]$null, [ref]$errors); "
                "if ($errors -and $errors.Count -gt 0) { "
                "  $errors | ForEach-Object { Write-Error $_.ToString() }; exit 1 "
                "}"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"launcher does not parse:\n{result.stderr}"


def test_discovery_script_still_parses_when_launched_this_way(agent_source: str, tmp_path):
    """The real inner script keeps its param()/[CmdletBinding()] first, which
    is the property the preamble approach destroyed."""
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("pwsh not installed on this host")

    discover = REPO_ROOT / "scripts" / "windows" / "discover_apps.ps1"
    inner = tmp_path / "inner.ps1"
    # agent.ps1 writes the caller's bytes verbatim -- so this is byte-identical
    # to what the guest would run.
    inner.write_bytes(discover.read_bytes())

    launcher = tmp_path / "launch.ps1"
    launcher.write_text(_render_launcher(agent_source, str(inner)), encoding="utf-8")

    for target in (inner, launcher):
        result = subprocess.run(
            [
                pwsh,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                (
                    "$errors = $null; "
                    f"[void][System.Management.Automation.Language.Parser]::ParseFile('{target}', "
                    "[ref]$null, [ref]$errors); "
                    "if ($errors -and $errors.Count -gt 0) { "
                    "  $errors | ForEach-Object { Write-Error $_.ToString() }; exit 1 "
                    "}"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, f"{target.name} does not parse:\n{result.stderr}"


def test_wayseam_wgc_readback_never_exceeds_the_mapped_staging_surface():
    # Regression: three AccessViolationException crashes on 2026-08-25 in
    # WayseamWgcCapture.UpdatePixels (Marshal.Copy past the mapped surface
    # after a resize). The readback must be clamped to the staging texture.
    source = WGC_CS.read_text(encoding="utf-8")
    for marker in (
        "int mappedWidth = (int)Math.Min(state.StagingWidth, (uint)width);",
        "int mappedHeight = (int)Math.Min(state.StagingHeight, (uint)height);",
        "if (data.RowPitch < (uint)mappedWidth * 4u)",
        "right = Math.Min(right, mappedWidth);",
        "bottom = Math.Min(bottom, mappedHeight);",
        "box.Right = (uint)Math.Min((uint)right, state.StagingWidth);",
    ):
        assert marker in source


def test_agent_ps1_exposes_shared_memory_ring_route(agent_source: str):
    # ADR 0004: /wayseam/shm assigns per-HWND ring slots; frames then flow
    # through IVSHMEM with no HTTP on the hot path. Slot geometry constants
    # must match src/wayseam/wayseam/shm.py.
    unauthorized = agent_source.index("error = 'unauthorized'")
    assert agent_source.index("$path -eq '/wayseam/shm'") > unauthorized
    for marker in (
        "ShmStart",
        "ShmStop",
        "ShmStatus",
        "slot_size = 22020096",
        "slot0_offset = 1048576",
        "invalid_shm_request",
        "shm_assign_failed",
    ):
        assert marker in agent_source


def test_wayseam_wgc_ring_writer_uses_seqlock_and_wsd1_blobs():
    source = WGC_CS.read_text(encoding="utf-8")
    for marker in (
        "RingSlotCount = 3",
        "RingSlotSize = 21L * 1024 * 1024",
        "RingSlot0Offset = 1048576",
        'System.Text.Encoding.ASCII.GetBytes("WSRING1\\0")',
        "seq + 1); // odd: writing",
        "seq + 2); // even: published",
        "Thread.MemoryBarrier();",
        "RefreshPixels(session, 12)",
        "EncodeDelta(state, session, state.Sequence)",
    ):
        assert marker in source


def test_agent_geometry_uses_dwm_extended_frame_bounds(agent_source: str):
    # The host maps input/cursor against the CAPTURED rectangle. WGC captures
    # the DWM extended frame bounds; GetWindowRect can differ by the invisible
    # border (seen after a live DPI change), which deadlocked pointer mapping.
    assert "GetVisibleRect" in agent_source
    assert "DwmGetWindowAttribute(h, 9, out r" in agent_source
    assert "GetVisibleRect($rootHwnd" in agent_source
    assert "GetVisibleRect($hwnd" in agent_source
    assert "Inp.GetVisibleRect(hwnd, out rect)" in WGC_CS.read_text(encoding="utf-8")
