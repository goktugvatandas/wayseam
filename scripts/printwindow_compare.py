#!/usr/bin/env python3
# ruff: noqa: E501
"""Compare two PrintWindow flag modes without exporting application pixels."""

from __future__ import annotations

import argparse
import base64
import json
import os
import time
from pathlib import Path, PureWindowsPath

from wayseam_guest_capture_profile import presenter_pid

from wayseam.config import Config
from wayseam.guest.agent import AgentClient

DIFF_NATIVE = r'''
Add-Type -ReferencedAssemblies System.Drawing -TypeDefinition @"
using System;
using System.Drawing;
using System.Drawing.Imaging;
using System.Runtime.InteropServices;
public static class WayseamProfileDiff {
  public sealed class Result {
    public long different, pixels;
    public int left, top, right, bottom, maxDelta;
    public double firstLuma, secondLuma;
  }
  public static Result Compare(Bitmap first, Bitmap second) {
    int width = first.Width, height = first.Height, stride = width * 4;
    Rectangle area = new Rectangle(0, 0, width, height);
    BitmapData aData = first.LockBits(area, ImageLockMode.ReadOnly, PixelFormat.Format32bppArgb);
    BitmapData bData = second.LockBits(area, ImageLockMode.ReadOnly, PixelFormat.Format32bppArgb);
    byte[] a = new byte[stride * height], b = new byte[stride * height];
    try {
      for (int y = 0; y < height; y++) {
        Marshal.Copy(IntPtr.Add(aData.Scan0, y * aData.Stride), a, y * stride, stride);
        Marshal.Copy(IntPtr.Add(bData.Scan0, y * bData.Stride), b, y * stride, stride);
      }
    } finally {
      first.UnlockBits(aData); second.UnlockBits(bData);
    }
    Result result = new Result {
      pixels = (long)width * height, left = width, top = height, right = -1, bottom = -1
    };
    long firstSum = 0, secondSum = 0;
    for (int y = 0; y < height; y++) for (int x = 0; x < width; x++) {
      int p = y * stride + x * 4;
      firstSum += a[p] + a[p + 1] + a[p + 2];
      secondSum += b[p] + b[p + 1] + b[p + 2];
      int delta = Math.Max(Math.Abs(a[p] - b[p]),
        Math.Max(Math.Abs(a[p + 1] - b[p + 1]), Math.Abs(a[p + 2] - b[p + 2])));
      if (delta == 0 && a[p + 3] == b[p + 3]) continue;
      result.different++;
      result.maxDelta = Math.Max(result.maxDelta, delta);
      result.left = Math.Min(result.left, x); result.right = Math.Max(result.right, x);
      result.top = Math.Min(result.top, y); result.bottom = Math.Max(result.bottom, y);
    }
    double channels = result.pixels * 3.0;
    result.firstLuma = firstSum / channels;
    result.secondLuma = secondSum / channels;
    return result;
  }
}
"@
'''


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--process", default="Affinity.exe")
    parser.add_argument("--first", type=int, default=0)
    parser.add_argument("--second", type=int, default=2)
    parser.add_argument("--pause-presenter", action="store_true")
    parser.add_argument("--write-images", type=Path)
    args = parser.parse_args()
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
    compare = rf'''
$hwnd = [IntPtr]{window.hwnd}
$rect = [WayseamNativeCapture+RECT]::new()
if (-not [WayseamNativeCapture]::GetWindowRect($hwnd, [ref]$rect)) {{ throw 'rect failed' }}
$width = $rect.Right - $rect.Left; $height = $rect.Bottom - $rect.Top
$first = [Drawing.Bitmap]::new($width, $height, [Drawing.Imaging.PixelFormat]::Format32bppArgb)
$second = [Drawing.Bitmap]::new($width, $height, [Drawing.Imaging.PixelFormat]::Format32bppArgb)
try {{
  $graphics = [Drawing.Graphics]::FromImage($first); $dc = $graphics.GetHdc()
  try {{ $firstOk = [WayseamNativeCapture]::PrintWindow($hwnd, $dc, {args.first}) }}
  finally {{ $graphics.ReleaseHdc($dc); $graphics.Dispose() }}
  $graphics = [Drawing.Graphics]::FromImage($second); $dc = $graphics.GetHdc()
  try {{ $secondOk = [WayseamNativeCapture]::PrintWindow($hwnd, $dc, {args.second}) }}
  finally {{ $graphics.ReleaseHdc($dc); $graphics.Dispose() }}
  if (-not $firstOk -or -not $secondOk) {{ throw 'capture failed' }}
  $comparison = [WayseamProfileDiff]::Compare($first, $second)
  $firstPng = ''; $secondPng = ''
  if ({'$true' if args.write_images else '$false'}) {{
    $memory = [IO.MemoryStream]::new()
    try {{ $first.Save($memory, [Drawing.Imaging.ImageFormat]::Png); $firstPng = [Convert]::ToBase64String($memory.ToArray()) }}
    finally {{ $memory.Dispose() }}
    $memory = [IO.MemoryStream]::new()
    try {{ $second.Save($memory, [Drawing.Imaging.ImageFormat]::Png); $secondPng = [Convert]::ToBase64String($memory.ToArray()) }}
    finally {{ $memory.Dispose() }}
  }}
  [pscustomobject]@{{ comparison = $comparison; first_png = $firstPng; second_png = $secondPng }} |
    ConvertTo-Json -Compress -Depth 4
}} finally {{ $first.Dispose(); $second.Dispose() }}
'''

    pid = presenter_pid(window.hwnd) if args.pause_presenter else None
    if args.pause_presenter and pid is None:
        raise SystemExit("presenter process not found")
    try:
        if pid is not None:
            os.kill(pid, 19)
            time.sleep(0.25)
        result = client.exec(native + "\n" + DIFF_NATIVE + "\n" + compare, timeout=60)
    finally:
        if pid is not None:
            os.kill(pid, 18)
    if not result.ok:
        raise SystemExit(f"guest comparison failed rc={result.rc}")
    payload = json.loads(result.stdout)
    comparison = payload["comparison"]
    if args.write_images:
        args.write_images.mkdir(parents=True, exist_ok=True)
        (args.write_images / "printwindow-first.png").write_bytes(
            base64.b64decode(payload["first_png"], validate=True)
        )
        (args.write_images / "printwindow-second.png").write_bytes(
            base64.b64decode(payload["second_png"], validate=True)
        )
    print(json.dumps({
        "frame_size": [window.width, window.height],
        "different_pixels": int(comparison["different"]),
        "different_ratio": round(int(comparison["different"]) / int(comparison["pixels"]), 6),
        "bounds": [
            int(comparison["left"]), int(comparison["top"]),
            int(comparison["right"]), int(comparison["bottom"]),
        ],
        "max_channel_delta": int(comparison["maxDelta"]),
        "average_luma": [round(float(comparison["firstLuma"]), 1), round(float(comparison["secondLuma"]), 1)],
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
