#!/usr/bin/env python3
"""Enumerate Windows display modes without changing the guest display."""

from __future__ import annotations

import base64
import json
import uuid
from pathlib import Path

from wayseam.config import Config
from wayseam.guest.agent import AgentClient


def main() -> int:
    client = AgentClient(Config.load())
    source = Path(__file__).with_suffix(".cs").read_bytes()
    nonce = uuid.uuid4().hex
    guest_source = rf"C:\OEM\agent-runs\display-{nonce}.cs"
    guest_library = rf"C:\OEM\agent-runs\display-{nonce}.dll"
    encoded = base64.b64encode(source).decode("ascii")
    script = rf'''
$ErrorActionPreference = 'Stop'
[IO.File]::WriteAllBytes('{guest_source}', [Convert]::FromBase64String('{encoded}'))
& 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe' /nologo /target:library `
  "/out:{guest_library}" '{guest_source}'
if ($LASTEXITCODE -ne 0) {{ throw 'display probe compile failed' }}
Add-Type -Path '{guest_library}'
[WayseamDisplayProbe]::Enumerate() | ConvertTo-Json -Compress
'''
    try:
        result = client.exec(script, timeout=30)
        if not result.ok:
            raise SystemExit((result.stdout + result.stderr).strip()[:800])
        modes = json.loads(result.stdout)
    finally:
        client.exec(
            rf"Remove-Item -LiteralPath '{guest_source}','{guest_library}' "
            "-Force -ErrorAction SilentlyContinue",
            timeout=15,
        )
    if isinstance(modes, dict):
        modes = [modes]
    current = [mode for mode in modes if mode["Current"]]
    relevant = [
        mode for mode in modes
        if mode["Width"] == 3840 and mode["Height"] == 2160
    ]
    print(json.dumps({"current": current, "modes_3840x2160": relevant}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
