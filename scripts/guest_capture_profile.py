#!/usr/bin/env python3
# ruff: noqa: E501
"""Profile guest-side allocation, PrintWindow, and delta encoding separately."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path, PureWindowsPath

from wayseam.config import Config
from wayseam.guest.agent import AgentClient


def presenter_pid(hwnd: int) -> int | None:
    marker = hex(hwnd).encode("ascii")
    for path in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            args = [part for part in path.read_bytes().split(b"\0") if part]
        except OSError:
            continue
        if b"wayseam.present.window" not in args or b"--hwnd" not in args:
            continue
        index = args.index(b"--hwnd")
        if index + 1 < len(args) and args[index + 1].lower() == marker:
            return int(path.parent.name)
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--process", default="Affinity.exe")
    parser.add_argument("--samples", type=int, default=6)
    parser.add_argument("--pause-presenter", action="store_true")
    parser.add_argument("--print-window-flags", type=int, default=2)
    args = parser.parse_args()
    if not (2 <= args.samples <= 20):
        parser.error("--samples must be between 2 and 20")

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
        Path(__file__).resolve().parents[1]
        / "guest"
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
$samples = {args.samples + 1}
$sequence = 0
$firstHash = ''
$rows = [Collections.Generic.List[object]]::new()
for ($i = 0; $i -lt $samples; $i++) {{
  $total = [Diagnostics.Stopwatch]::StartNew()
  $setup = [Diagnostics.Stopwatch]::StartNew()
  $rect = [WayseamNativeCapture+RECT]::new()
  if (-not [WayseamNativeCapture]::GetWindowRect($hwnd, [ref]$rect)) {{ throw 'rect failed' }}
  $width = $rect.Right - $rect.Left
  $height = $rect.Bottom - $rect.Top
  $bitmap = [Drawing.Bitmap]::new($width, $height, [Drawing.Imaging.PixelFormat]::Format32bppArgb)
  $graphics = [Drawing.Graphics]::FromImage($bitmap)
  $dc = $graphics.GetHdc()
  $setup.Stop()
  $capture = [Diagnostics.Stopwatch]::StartNew()
  try {{ $ok = [WayseamNativeCapture]::PrintWindow($hwnd, $dc, {args.print_window_flags}) }}
  finally {{ $graphics.ReleaseHdc($dc) }}
  $capture.Stop()
  if (-not $ok) {{ throw 'capture failed' }}
  $encode = [Diagnostics.Stopwatch]::StartNew()
  $bytes = [WayseamNativeCapture]::EncodeDeltaFrame('profile', $bitmap, $sequence)
  $encode.Stop()
  $sequence = [BitConverter]::ToInt32($bytes, 16)
  if ($i -eq 0) {{
    $sha = [Security.Cryptography.SHA256]::Create()
    try {{ $firstHash = ([BitConverter]::ToString($sha.ComputeHash($bytes)) -replace '-', '').ToLowerInvariant() }}
    finally {{ $sha.Dispose() }}
  }}
  $graphics.Dispose()
  $bitmap.Dispose()
  $total.Stop()
  if ($i -gt 0) {{
    $rows.Add([pscustomobject]@{{
      setup_ms = [Math]::Round($setup.Elapsed.TotalMilliseconds, 1)
      capture_ms = [Math]::Round($capture.Elapsed.TotalMilliseconds, 1)
      encode_ms = [Math]::Round($encode.Elapsed.TotalMilliseconds, 1)
      total_ms = [Math]::Round($total.Elapsed.TotalMilliseconds, 1)
      wire_bytes = $bytes.Length
    }})
  }}
}}
[pscustomobject]@{{ width = $width; height = $height; first_hash = $firstHash; samples = $rows }} |
  ConvertTo-Json -Compress -Depth 4
'''

    pid = presenter_pid(window.hwnd) if args.pause_presenter else None
    if args.pause_presenter and pid is None:
        raise SystemExit("presenter process not found")
    try:
        if pid is not None:
            os.kill(pid, 19)  # SIGSTOP
            time.sleep(0.25)
        result = client.exec(native + "\n" + profile, timeout=60)
    finally:
        if pid is not None:
            os.kill(pid, 18)  # SIGCONT
    if not result.ok:
        raise SystemExit(f"guest profile failed rc={result.rc}")
    payload = json.loads(result.stdout)
    rows = payload["samples"]
    medians = {
        key: round(sorted(float(row[key]) for row in rows)[len(rows) // 2], 1)
        for key in ("setup_ms", "capture_ms", "encode_ms", "total_ms", "wire_bytes")
    }
    print(json.dumps({
        "frame_size": [int(payload["width"]), int(payload["height"])],
        "samples": len(rows),
        "first_hash": str(payload["first_hash"]),
        "median": medians,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
