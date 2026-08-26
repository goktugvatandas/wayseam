#!/usr/bin/env python3
"""Live regression gate for Wayseam tiling, input, cursor, and popup alpha."""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import time

import gi

from wayseam.config import Config
from wayseam.guest.agent import AgentClient

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf  # noqa: E402


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


def decode_corner_pixels(png: bytes) -> tuple[bool, list[tuple[int, ...]]]:
    loader = GdkPixbuf.PixbufLoader.new_with_type("png")
    loader.write(png)
    loader.close()
    pixbuf = loader.get_pixbuf()
    assert pixbuf is not None
    width = pixbuf.get_width()
    height = pixbuf.get_height()
    channels = pixbuf.get_n_channels()
    stride = pixbuf.get_rowstride()
    pixels = pixbuf.get_pixels()
    corners = []
    for x, y in ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)):
        start = y * stride + x * channels
        corners.append(tuple(pixels[start:start + channels]))
    return pixbuf.get_has_alpha(), corners


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hwnd", required=True, type=lambda value: int(value, 0))
    parser.add_argument("--pointer-x", type=int, default=400)
    parser.add_argument("--pointer-y", type=int, default=250)
    args = parser.parse_args()

    failures: list[str] = []
    clients = json.loads(subprocess.check_output(["rtk", "hyprctl", "clients", "-j"]))
    wayseam = [client for client in clients if client.get("class") == "org.wayseam.Window"]
    floating = [client.get("title", "") for client in wayseam if client.get("floating")]
    if floating:
        failures.append(f"not tileable: floating={floating}")

    client = AgentClient(Config.load())
    root, owned = client.owned_windows(args.hwnd)
    if root["left"] != 0 or root["top"] != 0:
        failures.append(
            f"pointer origin mismatch: guest_root=({root['left']},{root['top']})"
        )
    if not owned:
        failures.append("popup alpha: no owned popup is currently open")
    else:
        has_alpha, corners = decode_corner_pixels(
            client.window_frame(owned[0].hwnd, popup_alpha=True)
        )
        opaque_black = [
            pixel for pixel in corners
            if pixel[:3] == (0, 0, 0) and (len(pixel) < 4 or pixel[3] == 255)
        ]
        if not has_alpha or opaque_black:
            failures.append(
                f"popup alpha lost: has_alpha={has_alpha} corners={corners}"
            )

    timings = []
    for _ in range(30):
        started = time.perf_counter()
        client.window_pointer_input(
            args.hwnd,
            x=min(args.pointer_x, root["width"] - 1),
            y=min(args.pointer_y, root["height"] - 1),
            action="move",
        )
        timings.append((time.perf_counter() - started) * 1000)
    p50 = statistics.median(timings)
    p95 = percentile(timings, 0.95)
    if p95 > 25:
        failures.append(f"pointer latency: p50={p50:.1f}ms p95={p95:.1f}ms")

    cursor = client.window_cursor(args.hwnd)
    if not cursor.visible:
        failures.append("guest cursor is not visible")
    elif not cursor.png:
        failures.append("guest cursor shape has no PNG")

    print(json.dumps({
        "wayseam_windows": len(wayseam),
        "floating": floating,
        "pointer_p50_ms": round(p50, 1),
        "pointer_p95_ms": round(p95, 1),
        "cursor": {
            "visible": cursor.visible,
            "shape": hex(cursor.shape),
            "hotspot": [cursor.hot_x, cursor.hot_y],
            "size": [cursor.width, cursor.height],
            "png_bytes": len(cursor.png),
        },
        "failures": failures,
    }, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
