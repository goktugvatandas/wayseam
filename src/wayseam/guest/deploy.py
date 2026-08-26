# SPDX-License-Identifier: MIT
"""Push the bundled guest agent into a running guest and restart it.

The agent serves the very ``/exec`` used to replace it, so the restart is
scheduled a few seconds out through a one-shot scheduled task (ported from
Wayseam ``core.guest_sync``; see upstream/PROVENANCE).
"""

from __future__ import annotations

import base64
import hashlib
import re
import time

from wayseam.config import Config
from wayseam.guest.agent import AgentClient
from wayseam.paths import oem_dir

# Guest-side hidden launcher installed by install.bat (path is a guest
# contract; it keeps its historical name until the OEM payload is renamed).
_LAUNCHER_VBS = r"C:\Users\Public\wayseam\launchers\hidden-launcher.vbs"
_RESTART_AGENT_SCRIPT = (
    "Get-CimInstance Win32_Process -Filter \"Name='powershell.exe'\" |\n"
    "  Where-Object { $_.CommandLine -like '*agent.ps1*' -and "
    "$_.CommandLine -notlike '*restart-agent.ps1*' } |\n"
    "  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }\n"
    "Start-Sleep -Seconds 2\n"
    f"$wrap = '{_LAUNCHER_VBS}'\n"
    "if (Test-Path -LiteralPath $wrap) {\n"
    "  Start-Process wscript.exe -ArgumentList "
    '("`"$wrap`"",\'"powershell.exe"\',\'"-NoProfile"\',\'"-ExecutionPolicy"\','
    "'\"Bypass\"','\"-File\"','\"C:\\OEM\\agent.ps1\"')\n"
    "} else {\n"
    "  Start-Process powershell.exe -WindowStyle Hidden -ArgumentList "
    "'-NoProfile','-ExecutionPolicy','Bypass','-File','C:\\OEM\\agent.ps1'\n"
    "}\n"
)


def restart_agent_script() -> str:
    """Return the ``/exec`` payload that schedules an agent restart (~5 s out)."""
    b64 = base64.b64encode(_RESTART_AGENT_SCRIPT.encode("utf-8")).decode("ascii")
    return (
        f"$s = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{b64}')); "
        r"Set-Content -Path 'C:\OEM\restart-agent.ps1' -Value $s -Encoding UTF8; "
        f"$wrap = '{_LAUNCHER_VBS}'; "
        "if (Test-Path -LiteralPath $wrap) { "
        "$exe = 'wscript.exe'; "
        '$arg = \'"\' + $wrap + \'" "powershell.exe" "-NoProfile" '
        '"-ExecutionPolicy" "Bypass" "-File" "C:\\OEM\\restart-agent.ps1"\' '
        "} else { "
        "$exe = 'powershell.exe'; "
        "$arg = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden "
        "-File C:\\OEM\\restart-agent.ps1' "
        "}; "
        "$act = New-ScheduledTaskAction -Execute $exe -Argument $arg; "
        "$trg = New-ScheduledTaskTrigger -Once -At (Get-Date).AddSeconds(5); "
        "Register-ScheduledTask -TaskName 'WayseamAgentRestart' -Action $act "
        "-Trigger $trg -Force | Out-Null"
    )


def bundled_agent_version() -> str:
    """The version string baked into the bundled ``agent.ps1``."""
    text = (oem_dir() / "agent" / "agent.ps1").read_text(encoding="utf-8", errors="replace")
    match = re.search(r"\$script:AgentVersion\s*=\s*'([^']+)'", text)
    if not match:
        raise RuntimeError("bundled agent.ps1 has no AgentVersion")
    return match.group(1)


def deploy_agent(cfg: Config, *, wait_seconds: float = 45.0) -> int:
    """Deliver agent.ps1 + wayseam_wgc.cs, restart the agent, wait for the new version."""
    version = bundled_agent_version()
    agent_source = (oem_dir() / "agent" / "agent.ps1").read_bytes()
    wgc_source = (oem_dir() / "agent" / "wayseam_wgc.cs").read_bytes()
    agent_digest = hashlib.sha256(agent_source).hexdigest()
    wgc_digest = hashlib.sha256(wgc_source).hexdigest()
    agent_encoded = base64.b64encode(agent_source).decode("ascii")
    wgc_encoded = base64.b64encode(wgc_source).decode("ascii")
    deploy = rf'''
$agentTarget = 'C:\OEM\agent.ps1'
$agentBackup = 'C:\OEM\agent.ps1.pre-{version}'
$agentStage = "$agentTarget.wayseam-new"
$wgcTarget = 'C:\OEM\wayseam_wgc.cs'
$wgcBackup = 'C:\OEM\wayseam_wgc.cs.pre-{version}'
$wgcStage = "$wgcTarget.wayseam-new"
if (-not (Test-Path -LiteralPath $agentTarget)) {{ throw 'agent target missing' }}
[IO.File]::WriteAllBytes($agentStage, [Convert]::FromBase64String('{agent_encoded}'))
[IO.File]::WriteAllBytes($wgcStage, [Convert]::FromBase64String('{wgc_encoded}'))
$actualAgent = (Get-FileHash -LiteralPath $agentStage -Algorithm SHA256).Hash.ToLowerInvariant()
$actualWgc = (Get-FileHash -LiteralPath $wgcStage -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualAgent -ne '{agent_digest}' -or $actualWgc -ne '{wgc_digest}') {{
  Remove-Item -LiteralPath $agentStage,$wgcStage -Force -ErrorAction SilentlyContinue
  throw 'agent delivery hash mismatch'
}}
if (-not (Test-Path -LiteralPath $agentBackup)) {{
  Copy-Item -LiteralPath $agentTarget -Destination $agentBackup
}}
if ((Test-Path -LiteralPath $wgcTarget) -and -not (Test-Path -LiteralPath $wgcBackup)) {{
  Copy-Item -LiteralPath $wgcTarget -Destination $wgcBackup
}}
Move-Item -LiteralPath $wgcStage -Destination $wgcTarget -Force
Move-Item -LiteralPath $agentStage -Destination $agentTarget -Force
Write-Output "$actualAgent $actualWgc"
'''
    client = AgentClient(cfg)
    result = client.exec(deploy, timeout=30)
    delivered = result.stdout.strip().lower().split()
    if not result.ok or delivered != [agent_digest, wgc_digest]:
        print("agent delivery failed")
        return 1
    restart = client.exec(restart_agent_script(), timeout=30)
    if not restart.ok:
        print("agent restart scheduling failed")
        return 1
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        time.sleep(1)
        try:
            health = client.health()
        except Exception:  # noqa: BLE001 - agent is restarting
            continue
        if health.get("version") == version:
            print(f"agent {version} healthy")
            return 0
    print("agent did not return with the expected version")
    return 1
