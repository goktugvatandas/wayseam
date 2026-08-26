#!/usr/bin/env python3
"""Compile the agent's embedded Wayseam C# block inside the guest."""

from __future__ import annotations

from pathlib import Path

from wayseam.config import Config
from wayseam.guest.agent import AgentClient


def main() -> int:
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
    self_test = r'''
$bitmap = [Drawing.Bitmap]::new(4, 3, [Drawing.Imaging.PixelFormat]::Format32bppArgb)
$graphics = [Drawing.Graphics]::FromImage($bitmap)
try {
  $graphics.Clear([Drawing.Color]::Black)
  $full = [WayseamNativeCapture]::EncodeDeltaFrame('native-self-test', $bitmap, 0)
  if ($full.Length -ne 84 -or [BitConverter]::ToInt32($full, 16) -ne 1 -or
      [BitConverter]::ToInt32($full, 28) -ne 4 -or
      [BitConverter]::ToInt32($full, 32) -ne 3) { throw 'full delta failed' }
  $same = [WayseamNativeCapture]::EncodeDeltaFrame('native-self-test', $bitmap, 1)
  if ($same.Length -ne 36 -or [BitConverter]::ToInt32($same, 16) -ne 1 -or
      [BitConverter]::ToInt32($same, 28) -ne 0 -or
      [BitConverter]::ToInt32($same, 32) -ne 0) { throw 'empty delta failed' }
  $bitmap.SetPixel(2, 1, [Drawing.Color]::Red)
  $changed = [WayseamNativeCapture]::EncodeDeltaFrame('native-self-test', $bitmap, 1)
  if ($changed.Length -ne 40 -or [BitConverter]::ToInt32($changed, 16) -ne 2 -or
      [BitConverter]::ToInt32($changed, 20) -ne 2 -or
      [BitConverter]::ToInt32($changed, 24) -ne 1 -or
      [BitConverter]::ToInt32($changed, 28) -ne 1 -or
      [BitConverter]::ToInt32($changed, 32) -ne 1 -or
      $changed[36] -ne 0 -or $changed[37] -ne 0 -or
      $changed[38] -ne 255 -or $changed[39] -ne 255) { throw 'changed delta failed' }
  Write-Output wayseam-native-ok
} finally {
  $graphics.Dispose()
  $bitmap.Dispose()
}
'''
    compile_script = source[start:end] + "\n" + self_test
    result = AgentClient(Config.load()).exec(compile_script, timeout=45)
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.stderr.strip():
        print(result.stderr.strip())
    return 0 if result.ok and "wayseam-native-ok" in result.stdout else 1


if __name__ == "__main__":
    raise SystemExit(main())
