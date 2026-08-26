#!/usr/bin/env python3
"""Profile the persistent guest capture+delta hot path from local source."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path, PureWindowsPath

from wayseam_guest_capture_profile import presenter_pid

from wayseam.config import Config
from wayseam.guest.agent import AgentClient


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--process", default="Affinity.exe")
    parser.add_argument("--samples", type=int, default=12)
    parser.add_argument("--pause-presenter", action="store_true")
    args = parser.parse_args()
    if not (2 <= args.samples <= 30):
        parser.error("--samples must be between 2 and 30")

    client = AgentClient(Config.load())
    windows = [
        window
        for window in client.top_level_windows()
        if PureWindowsPath(window.process_path).name.casefold()
        == args.process.casefold()
        and window.owner == 0
    ]
    if len(windows) != 1:
        raise SystemExit(f"expected one root for process, found {len(windows)}")
    window = windows[0]

    source = (
        Path(__file__).resolve().parents[2]
        / "config"
        / "oem"
        / "agent"
        / "agent.ps1"
    ).read_text(encoding="utf-8")
    start = source.index("    Add-Type -AssemblyName System.Drawing")
    end_marker = '"@\n    $script:WayseamCaptureAvailable = $true'
    end = source.index(end_marker, start) + 2
    native = source[start:end]
    profile = rf'''
$hwnd = [IntPtr]{window.hwnd}
$base = 0
$rows = [Collections.Generic.List[object]]::new()
for ($i = 0; $i -lt {args.samples + 1}; $i++) {{
  $timer = [Diagnostics.Stopwatch]::StartNew()
  $capture = [WayseamNativeCapture]::CaptureDeltaFrame(
    'hotpath-profile', $hwnd, {window.width}, {window.height}, $base, $false
  )
  $timer.Stop()
  if (-not $capture.Ok) {{ throw 'capture failed' }}
  $base = [BitConverter]::ToInt32($capture.Bytes, 16)
  if ($i -gt 0) {{
    $rows.Add([pscustomobject]@{{
      total_ms = [Math]::Round($timer.Elapsed.TotalMilliseconds, 1)
      wire_bytes = $capture.Bytes.Length
    }})
  }}
}}
[pscustomobject]@{{ width = {window.width}; height = {window.height}; samples = $rows }} |
  ConvertTo-Json -Compress -Depth 4
'''

    pid = presenter_pid(window.hwnd) if args.pause_presenter else None
    if args.pause_presenter and pid is None:
        raise SystemExit("presenter process not found")
    try:
        if pid is not None:
            os.kill(pid, 19)
            time.sleep(0.25)
        result = client.exec(native + "\n" + profile, timeout=60)
    finally:
        if pid is not None:
            os.kill(pid, 18)
    if not result.ok:
        raise SystemExit(f"guest hot-path profile failed rc={result.rc}")
    payload = json.loads(result.stdout)
    rows = payload["samples"]
    timings = sorted(float(row["total_ms"]) for row in rows)
    print(json.dumps({
        "frame_size": [int(payload["width"]), int(payload["height"])],
        "samples": len(rows),
        "median_total_ms": timings[len(timings) // 2],
        "p95_total_ms": timings[min(len(timings) - 1, int(len(timings) * 0.95))],
        "median_wire_bytes": sorted(int(row["wire_bytes"]) for row in rows)[len(rows) // 2],
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
