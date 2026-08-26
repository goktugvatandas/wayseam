#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""IVSHMEM transport spike: verify guest<->host shared memory and measure RTT.

Guest side: compiles wayseam_ivshmem_probe.cs via the agent's /exec and maps
the IVSHMEM BAR. Host side: mmaps the QEMU memory-backend file (which lives in
Omarchy's /shared bind mount) and exchanges a counter ping-pong with the guest
to measure round-trip latency without any HTTP in the loop.

Read-only with respect to the VM; writes only inside the dedicated 64 MiB
shared region and never touches Windows state.
"""
from __future__ import annotations

import argparse
import base64
import mmap
import statistics
import struct
import time
from pathlib import Path

from wayseam.config import Config
from wayseam.guest.agent import AgentClient

BACKING = Path.home() / "Windows" / "wayseam-ivshmem.bin"
MAGIC = b"WAYSEAM-IVSHMEM-TEST-1"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, default=5, help="guest echo loop duration")
    parser.add_argument("--pings", type=int, default=200)
    args = parser.parse_args()

    source = (Path(__file__).with_name("wayseam_ivshmem_probe.cs")).read_text(encoding="utf-8")
    b64 = base64.b64encode(source.encode("utf-8")).decode("ascii")
    client = AgentClient(Config.load())

    setup = (
        "$src=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + b64 + "'))\n"
        "Add-Type -TypeDefinition $src\n"
        "[WayseamIvshmem]::Open()\n"
        "[WayseamIvshmem]::WritePattern()\n"
    )
    result = client.exec(setup, timeout=60)
    print("guest setup:", result.stdout.strip()[-300:], result.stderr.strip()[:200])
    if "ok size=" not in result.stdout:
        print("guest could not map IVSHMEM; aborting")
        return 1

    with BACKING.open("r+b") as handle:
        memory = mmap.mmap(handle.fileno(), 0)
        try:
            head = memory[: len(MAGIC)]
            print("host sees magic:", head == MAGIC, head[:24])
            if head != MAGIC:
                print("shared memory is not coherent between guest and host; aborting")
                return 1

            # Start the guest echo loop in the background via /exec, then ping.
            echo = (
                "$src=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + b64 + "'))\n"
                "Add-Type -TypeDefinition $src\n"
                "[WayseamIvshmem]::Open()\n"
                f"[WayseamIvshmem]::PingLoop({args.seconds})\n"
                "[WayseamIvshmem]::Close()\n"
            )
            import threading

            echo_result: dict = {}

            def run_echo() -> None:
                echo_result["result"] = client.exec(echo, timeout=args.seconds + 60)

            thread = threading.Thread(target=run_echo, daemon=True)
            thread.start()
            time.sleep(1.5)  # let the guest loop start

            rtts = []
            deadline = time.monotonic() + args.seconds - 1.0
            counter = int(time.time())
            while len(rtts) < args.pings and time.monotonic() < deadline:
                counter += 1
                start = time.perf_counter_ns()
                memory[128:136] = struct.pack("<q", counter)
                while time.monotonic() < deadline:
                    if struct.unpack("<q", memory[192:200])[0] == counter:
                        rtts.append((time.perf_counter_ns() - start) / 1000.0)
                        break
                time.sleep(0.002)
            thread.join(timeout=args.seconds + 60)
            if rtts:
                rtts.sort()
                print(
                    f"IVSHMEM ping-pong over {len(rtts)} rounds (host write -> guest poll -> "
                    f"guest write -> host poll):"
                )
                print(
                    f"  median={statistics.median(rtts):.1f} us  "
                    f"p95={rtts[int(len(rtts) * 0.95) - 1]:.1f} us  "
                    f"min={rtts[0]:.1f} us  max={rtts[-1]:.1f} us"
                )
            else:
                print("no echo responses observed")
            guest = echo_result.get("result")
            if guest is not None:
                print("guest echo loop:", guest.stdout.strip()[-200:])
        finally:
            memory.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
