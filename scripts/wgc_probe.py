#!/usr/bin/env python3
# ruff: noqa: E501
"""Compile and time the experimental per-HWND Windows Graphics Capture path."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import time
import uuid
from pathlib import Path, PureWindowsPath

from wayseam.config import Config
from wayseam.guest.agent import AgentClient


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--process", default="Affinity.exe")
    parser.add_argument("--latency-samples", type=int, default=0)
    parser.add_argument("--isolate", action="store_true")
    parser.add_argument("--production-helper", action="store_true")
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
        raise SystemExit(f"expected one process root, found {len(windows)}")
    source = (
        Path(__file__).resolve().parents[2]
        / "config"
        / "oem"
        / "agent"
        / "wayseam_wgc.cs"
        if args.production_helper
        else Path(__file__).with_name("wayseam_wgc_probe.cs")
    ).read_bytes()
    nonce = uuid.uuid4().hex
    guest_source = rf"C:\OEM\agent-runs\wayseam-wgc-{nonce}.cs"
    guest_library = rf"C:\OEM\agent-runs\wayseam-wgc-{nonce}.dll"
    encoded = base64.b64encode(source).decode("ascii")
    paused: list[int] = []
    script = rf'''
$ErrorActionPreference = 'Stop'
$source = '{guest_source}'
$library = '{guest_library}'
[IO.File]::WriteAllBytes($source, [Convert]::FromBase64String('{encoded}'))
$compiler = 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe'
& $compiler /nologo /target:library "/out:$library" `
  '/r:C:\Windows\Microsoft.NET\Framework64\v4.0.30319\System.Runtime.WindowsRuntime.dll' `
  '/r:C:\Windows\Microsoft.NET\Framework64\v4.0.30319\System.Runtime.InteropServices.WindowsRuntime.dll' `
  '/r:C:\Windows\Microsoft.NET\assembly\GAC_MSIL\System.Runtime\v4.0_4.0.0.0__b03f5f7f11d50a3a\System.Runtime.dll' `
  '/r:C:\Windows\Microsoft.NET\Framework64\v4.0.30319\System.Drawing.dll' `
  '/r:C:\Windows\System32\WinMetadata\Windows.Foundation.winmd' `
  '/r:C:\Windows\System32\WinMetadata\Windows.Graphics.winmd' `
  $source
if ($LASTEXITCODE -ne 0) {{ throw "WGC probe compile failed: $LASTEXITCODE" }}
Add-Type -Path $library
$result = if ({'$true' if args.production_helper else '$false'}) {{
  $timer = [Diagnostics.Stopwatch]::StartNew()
  $capture = [WayseamWgcCapture]::CaptureDeltaFrame(
    'production-probe', [IntPtr]{windows[0].hwnd}, 0, 50
  )
  $timer.Stop()
  if (-not $capture.Ok) {{ throw 'production WGC capture failed' }}
  $firstMs = $timer.Elapsed.TotalMilliseconds
  $sequence = [BitConverter]::ToInt32($capture.Bytes, 16)
  $steadyMs = @()
  $steadyBytes = @()
  for ($index = 0; $index -lt 6; $index++) {{
    $timer.Restart()
    $second = [WayseamWgcCapture]::CaptureDeltaFrame(
      'production-probe', [IntPtr]{windows[0].hwnd}, $sequence, 20
    )
    $timer.Stop()
    if (-not $second.Ok) {{ throw 'persistent WGC capture failed' }}
    $sequence = [BitConverter]::ToInt32($second.Bytes, 16)
    $steadyMs += $timer.Elapsed.TotalMilliseconds
    $steadyBytes += $second.Bytes.Length
  }}
  @{{
    Width = $capture.Width
    Height = $capture.Height
    ByteCount = $capture.Bytes.Length
    Magic = [Text.Encoding]::ASCII.GetString($capture.Bytes, 0, 4)
    FirstMs = $firstMs
    SteadyMs = $steadyMs
    SteadyByteCount = $steadyBytes
  }}
}} elseif ({args.latency_samples} -gt 0) {{
  [WayseamWgcProbe]::MeasureHoverLatency(
    [IntPtr]{windows[0].hwnd}, {args.latency_samples}
  )
}} else {{
  [WayseamWgcProbe]::CaptureOnce([IntPtr]{windows[0].hwnd})
}}
$result |
  ConvertTo-Json -Compress
'''
    try:
        if args.isolate:
            for path in Path("/proc").glob("[0-9]*/cmdline"):
                try:
                    command = path.read_bytes()
                except OSError:
                    continue
                if (
                    b"wayseam.present.window" in command
                    or b"wayseam.present.watch" in command
                ):
                    pid = int(path.parent.name)
                    os.kill(pid, 19)
                    paused.append(pid)
            time.sleep(0.25)
        result = client.exec(script, timeout=45)
        if not result.ok:
            detail = (result.stdout + " " + result.stderr).strip()
            detail = detail.replace("\r", " ").replace("\n", " ")
            raise SystemExit(f"guest WGC probe failed: {detail[:800]}")
        payload = json.loads(result.stdout)
    finally:
        try:
            client.exec(
                rf"Remove-Item -LiteralPath '{guest_source}','{guest_library}' "
                "-Force -ErrorAction SilentlyContinue",
                timeout=15,
            )
        finally:
            for pid in paused:
                try:
                    os.kill(pid, 18)
                except ProcessLookupError:
                    pass
    if args.production_helper:
        print(json.dumps({
            "frame_size": [int(payload["Width"]), int(payload["Height"])],
            "delta_bytes": int(payload["ByteCount"]),
            "magic": str(payload["Magic"]),
            "cold_ms": round(float(payload["FirstMs"]), 1),
            "steady_ms": [round(float(value), 1) for value in payload["SteadyMs"]],
            "steady_delta_bytes": [int(value) for value in payload["SteadyByteCount"]],
        }))
        return 0

    if args.latency_samples:
        total = sorted(float(value) for value in payload["TotalMs"])
        readback = sorted(float(value) for value in payload["ReadbackMs"])
        print(json.dumps({
            "frame_size": [int(payload["Width"]), int(payload["Height"])],
            "samples": len(total),
            "median_input_to_pixels_ms": round(total[len(total) // 2], 1),
            "p95_input_to_pixels_ms": round(
                total[min(len(total) - 1, int(len(total) * 0.95))], 1,
            ),
            "median_readback_ms": round(readback[len(readback) // 2], 1),
        }))
        return 0

    digest = str(payload["Sha256"])
    printwindow = client.window_frame_bgra(
        windows[0].hwnd,
        stream_id=uuid.uuid4().hex,
        base_sequence=0,
        previous_pixels=None,
    )
    printwindow_digest = hashlib.sha256(printwindow.pixels).hexdigest()
    print(json.dumps({
        "frame_size": [int(payload["Width"]), int(payload["Height"])],
        "first_frame_ms": int(payload["FirstFrameMs"]),
        "readback_ms": round(float(payload["ReadbackMs"]), 1),
        "digest_valid": len(digest) == 64,
        "matches_printwindow": digest == printwindow_digest,
        "different_ratio": round(
            int(payload["DifferentPixels"])
            / (int(payload["Width"]) * int(payload["Height"])),
            6,
        ),
        "max_channel_delta": int(payload["MaxChannelDelta"]),
        "average_luma": [
            round(float(payload["WgcLuma"]), 1),
            round(float(payload["PrintWindowLuma"]), 1),
        ],
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
