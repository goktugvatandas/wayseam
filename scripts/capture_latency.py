#!/usr/bin/env python3
"""Live pass/fail gate for Wayseam per-window capture latency."""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
import uuid
from pathlib import PureWindowsPath

from wayseam_guest_capture_profile import presenter_pid

from wayseam.config import Config
from wayseam.guest.agent import AgentClient


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--process", default="Affinity.exe")
    parser.add_argument("--samples", type=int, default=10)
    parser.add_argument("--max-median-ms", type=float, default=60.0)
    parser.add_argument("--temporary-size", metavar="WIDTHxHEIGHT")
    parser.add_argument("--pause-presenter", action="store_true")
    args = parser.parse_args()
    if not (3 <= args.samples <= 100):
        parser.error("--samples must be between 3 and 100")

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

    original_size = windows[0].width, windows[0].height
    if args.temporary_size:
        width, separator, height = args.temporary_size.partition("x")
        if not separator or not width.isdigit() or not height.isdigit():
            parser.error("--temporary-size must be WIDTHxHEIGHT")
        client.window_resize(
            windows[0].hwnd,
            width=int(width),
            height=int(height),
        )

    stream_id = uuid.uuid4().hex
    sequence = 0
    pixels: bytes | None = None
    timings: list[float] = []
    changed = 0
    frame = None
    pid = presenter_pid(windows[0].hwnd) if args.pause_presenter else None
    if args.pause_presenter and pid is None:
        raise SystemExit("presenter process not found")
    try:
        if pid is not None:
            os.kill(pid, 19)
            time.sleep(0.25)
        for _ in range(args.samples + 1):
            started = time.perf_counter()
            frame = client.window_frame_bgra(
                windows[0].hwnd,
                stream_id=stream_id,
                base_sequence=sequence,
                previous_pixels=pixels,
            )
            elapsed = (time.perf_counter() - started) * 1000
            sequence = frame.server_sequence
            pixels = frame.pixels
            if timings:
                changed += int(frame.changed)
            timings.append(elapsed)
    finally:
        if pid is not None:
            os.kill(pid, 18)
        if args.temporary_size:
            client.window_resize(
                windows[0].hwnd,
                width=original_size[0],
                height=original_size[1],
            )
    timings = timings[1:]  # discard the full-frame warmup
    assert frame is not None
    median = statistics.median(timings)
    p95 = percentile(timings, 0.95)
    passed = median <= args.max_median_ms
    print(json.dumps({
        "frame_size": [frame.width, frame.height],
        "samples": len(timings),
        "median_ms": round(median, 1),
        "p95_ms": round(p95, 1),
        "max_ms": round(max(timings), 1),
        "changed_frames": changed,
        "limit_ms": args.max_median_ms,
        "passed": passed,
    }))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
