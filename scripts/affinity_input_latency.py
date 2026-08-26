#!/usr/bin/env python3
"""Measure Affinity mouse-motion to changed host frame without clicking."""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from pathlib import Path, PureWindowsPath

from wayseam.config import Config
from wayseam.guest.agent import AgentClient
from wayseam.present.window import InputSender, LatestFramePump, PointerEvent


def wayseam_host_pids() -> list[int]:
    result: list[int] = []
    for path in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            command = path.read_bytes()
        except OSError:
            continue
        if (
            b"wayseam.present.window" in command
            or b"wayseam.present.watch" in command
        ):
            result.append(int(path.parent.name))
    return result


def region_changed(
    previous: bytes,
    current: bytes,
    *,
    stride: int,
    left: int,
    top: int,
    right: int,
    bottom: int,
) -> bool:
    changed = 0
    for y in range(top, bottom):
        start = y * stride + left * 4
        end = y * stride + right * 4
        old_row = previous[start:end]
        new_row = current[start:end]
        if old_row == new_row:
            continue
        changed += sum(a != b for a, b in zip(old_row, new_row, strict=True))
        if changed >= 32:
            return True
    return False


def wait_for_frame(pump: LatestFramePump, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        frame = pump.take_latest()
        if frame is not None:
            return frame
        time.sleep(0.002)
    raise RuntimeError("Affinity did not produce a frame")


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=10)
    parser.add_argument("--max-median-ms", type=float, default=75.0)
    parser.add_argument("--direct-input", action="store_true")
    args = parser.parse_args()
    if not (4 <= args.samples <= 30):
        parser.error("--samples must be between 4 and 30")

    client = AgentClient(Config.load())
    windows = [
        window
        for window in client.top_level_windows()
        if PureWindowsPath(window.process_path).name.casefold() == "affinity.exe"
        and window.owner == 0
    ]
    if len(windows) != 1:
        raise SystemExit(f"expected one Affinity root, found {len(windows)}")
    window = windows[0]
    paused = wayseam_host_pids()
    pump = LatestFramePump(client, window.hwnd, max_fps=60)
    sender = InputSender(client, window.hwnd)
    timings: list[float] = []
    wire_sizes: list[int] = []
    try:
        for pid in paused:
            os.kill(pid, 19)
        time.sleep(0.25)
        pump.start()
        sender.start()
        current = wait_for_frame(pump)

        # Affinity's rendered close-button hover is deterministic, harmless,
        # and still traverses its actual WPF renderer and production transport.
        away = (max(100, window.width - 240), 100)
        hover = (window.width - 20, 15)
        region = {
            "left": max(0, window.width - 220),
            "top": 0,
            "right": window.width,
            "bottom": min(100, window.height),
        }
        if args.direct_input:
            client.window_pointer_input(window.hwnd, x=away[0], y=away[1], action="move")
        else:
            sender.submit(PointerEvent(*away, "move"))
        settle_deadline = time.monotonic() + 1.0
        while time.monotonic() < settle_deadline:
            frame = pump.take_latest()
            if frame is not None:
                current = frame
            time.sleep(0.002)

        for index in range(args.samples):
            position = hover if index % 2 == 0 else away
            baseline = current.surface.pixels
            started = time.perf_counter()
            if args.direct_input:
                client.window_pointer_input(
                    window.hwnd, x=position[0], y=position[1], action="move",
                )
            else:
                sender.submit(PointerEvent(*position, "move"))
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                frame = pump.take_latest()
                if frame is None:
                    time.sleep(0.001)
                    continue
                current = frame
                if region_changed(
                    baseline,
                    frame.surface.pixels,
                    stride=frame.surface.stride,
                    **region,
                ):
                    timings.append((time.perf_counter() - started) * 1000)
                    wire_sizes.append(frame.surface.wire_size)
                    break
            else:
                raise RuntimeError("Affinity hover change did not reach the host")
            time.sleep(0.025)
    finally:
        sender.stop()
        pump.stop()
        for pid in paused:
            try:
                os.kill(pid, 18)
            except ProcessLookupError:
                pass

    median = statistics.median(timings)
    passed = median <= args.max_median_ms
    print(json.dumps({
        "surface_size": [window.width, window.height],
        "samples": len(timings),
        "median_ms": round(median, 1),
        "p95_ms": round(percentile(timings, 0.95), 1),
        "min_ms": round(min(timings), 1),
        "max_ms": round(max(timings), 1),
        "values_ms": [round(value, 1) for value in timings],
        "wire_bytes": wire_sizes,
        "limit_ms": args.max_median_ms,
        "passed": passed,
    }))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
