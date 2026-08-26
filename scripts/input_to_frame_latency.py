#!/usr/bin/env python3
"""Measure production pointer-submit to changed-frame latency end to end."""

from __future__ import annotations

import argparse
import base64
import json
import os
import statistics
import time
import uuid
from pathlib import Path

from wayseam.config import Config
from wayseam.guest.agent import AgentClient
from wayseam.present.window import InputSender, LatestFramePump, PointerEvent


def wayseam_host_pids() -> list[int]:
    """Return only live Wayseam watcher/presenter processes."""
    result: list[int] = []
    for path in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            command = path.read_bytes()
        except OSError:
            continue
        if not (
            b"wayseam.present.window" in command
            or b"wayseam.present.watch" in command
        ):
            continue
        result.append(int(path.parent.name))
    return result


def marker_matches(frame, *, red: bool) -> bool:
    """Inspect one private probe pixel without exporting captured content."""
    x = y = 120
    offset = y * frame.stride + x * 4
    blue, green, red_channel, alpha = frame.pixels[offset:offset + 4]
    if red:
        return red_channel >= 240 and blue <= 15 and green <= 15 and alpha >= 240
    return blue >= 240 and red_channel <= 15 and green <= 15 and alpha >= 240


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


def wait_for_probe(client: AgentClient, title: str, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        matches = [window for window in client.top_level_windows() if window.title == title]
        if len(matches) == 1:
            return matches[0]
        time.sleep(0.05)
    raise RuntimeError("guest latency probe window did not appear")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=10)
    parser.add_argument("--max-median-ms", type=float, default=75.0)
    args = parser.parse_args()
    if not (4 <= args.samples <= 30):
        parser.error("--samples must be between 4 and 30")

    client = AgentClient(Config.load())
    title = f"Wayseam Input Latency Probe {uuid.uuid4().hex}"
    guest_script = rf'''
Add-Type -AssemblyName PresentationFramework,PresentationCore,WindowsBase
$window = [Windows.Window]::new()
$window.Title = '{title}'
$window.Width = 2110
$window.Height = 2110
$window.Left = 0
$window.Top = 0
$window.Topmost = $true
$window.WindowStyle = [Windows.WindowStyle]::None
$window.ResizeMode = [Windows.ResizeMode]::NoResize
$window.Background = [Windows.Media.Brushes]::Black
$canvas = [Windows.Controls.Canvas]::new()
$canvas.Background = [Windows.Media.Brushes]::Black
$marker = [Windows.Controls.Border]::new()
$marker.Width = 64
$marker.Height = 64
$marker.Background = [Windows.Media.Brushes]::Blue
[Windows.Controls.Canvas]::SetLeft($marker, 100)
[Windows.Controls.Canvas]::SetTop($marker, 100)
[void]$canvas.Children.Add($marker)
$window.Content = $canvas
$window.Add_MouseMove({{
  param($sender, $event)
  $point = $event.GetPosition($sender)
  if ($point.X -lt ($sender.ActualWidth / 2)) {{
    $marker.Background = [Windows.Media.Brushes]::Blue
  }} else {{
    $marker.Background = [Windows.Media.Brushes]::Red
  }}
}})
[void]$window.ShowDialog()
'''
    encoded = base64.b64encode(guest_script.encode("utf-16-le")).decode("ascii")
    launch = rf'''
$process = Start-Process powershell.exe -PassThru -WindowStyle Hidden -ArgumentList @(
  '-NoProfile', '-STA', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', '{encoded}'
)
Write-Output $process.Id
'''

    paused = wayseam_host_pids()
    probe = None
    pump = None
    sender = None
    guest_pid = 0
    timings: list[float] = []
    try:
        for pid in paused:
            os.kill(pid, 19)  # SIGSTOP
        time.sleep(0.25)
        result = client.exec(launch, timeout=15)
        if not result.ok:
            raise RuntimeError("failed to launch guest latency probe")
        guest_pid = int(result.stdout.strip())
        probe = wait_for_probe(client, title)
        pump = LatestFramePump(client, probe.hwnd, max_fps=30)
        sender = InputSender(client, probe.hwnd)
        pump.start()
        sender.start()

        # Establish a known blue starting state and a decoded base frame.
        sender.submit(PointerEvent(200, 300, "move"))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            frame = pump.take_latest()
            if frame is not None and marker_matches(frame.surface, red=False):
                break
            time.sleep(0.002)
        else:
            raise RuntimeError("probe did not establish its initial frame")

        for index in range(args.samples):
            red = index % 2 == 0
            x = probe.width - 200 if red else 200
            started = time.perf_counter()
            sender.submit(PointerEvent(x, 300, "move"))
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                frame = pump.take_latest()
                if frame is not None and marker_matches(frame.surface, red=red):
                    timings.append((time.perf_counter() - started) * 1000)
                    break
                time.sleep(0.001)
            else:
                raise RuntimeError("changed probe pixels did not reach the host")
            time.sleep(0.025)
    finally:
        if sender is not None:
            sender.stop()
        if pump is not None:
            pump.stop()
        if probe is not None:
            try:
                client.window_close(probe.hwnd)
            except Exception:
                pass
        if guest_pid > 0:
            # The normal WM_CLOSE above is authoritative. This bounded check
            # only reports cleanup failure; it never kills an unrelated PID.
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if not any(window.pid == guest_pid for window in client.top_level_windows()):
                    break
                time.sleep(0.05)
        for pid in paused:
            try:
                os.kill(pid, 18)  # SIGCONT
            except ProcessLookupError:
                pass

    median = statistics.median(timings)
    passed = median <= args.max_median_ms
    print(json.dumps({
        "surface_size": [2110, 2110],
        "samples": len(timings),
        "median_ms": round(median, 1),
        "p95_ms": round(percentile(timings, 0.95), 1),
        "min_ms": round(min(timings), 1),
        "max_ms": round(max(timings), 1),
        "limit_ms": args.max_median_ms,
        "passed": passed,
    }))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
