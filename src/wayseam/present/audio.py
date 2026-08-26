# SPDX-License-Identifier: MIT
"""Play Dockur's loopback-only guest PCM stream through PipeWire.

Dockur's ``AUDIO=Y`` mode exposes signed 16-bit, 48 kHz stereo PCM as
binary WebSocket messages at the existing noVNC ``/audio`` endpoint.  This
module intentionally implements only the small RFC 6455 client subset needed
for that local stream, avoiding a new host package or Python dependency.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import BinaryIO

from wayseam.paths import runtime_dir

_WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_MAX_HEADER_BYTES = 16 * 1024
_MAX_FRAME_BYTES = 4 * 1024 * 1024


class AudioBridgeError(RuntimeError):
    """Dockur audio transport or local playback failed."""


def audio_player_command(pw_cat: str) -> list[str]:
    """Return the exact PipeWire command for Dockur's raw PCM format."""
    return [
        pw_cat,
        "--playback",
        "--raw",
        "--rate",
        "48000",
        "--channels",
        "2",
        "--format",
        "s16",
        "--latency",
        "100ms",
        "-",
    ]


def websocket_handshake(
    sock: socket.socket,
    *,
    host: str,
    port: int,
    key: str | None = None,
) -> BinaryIO:
    """Upgrade one loopback HTTP connection to Dockur's audio WebSocket."""
    if key is None:
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
    request = (
        "GET /audio HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    ).encode("ascii")
    sock.sendall(request)
    stream = sock.makefile("rb")
    status = stream.readline(_MAX_HEADER_BYTES + 1)
    if len(status) > _MAX_HEADER_BYTES or not status.startswith(b"HTTP/1.1 101 "):
        raise AudioBridgeError("Dockur audio endpoint refused WebSocket upgrade")
    header_bytes = len(status)
    headers: dict[str, str] = {}
    while True:
        line = stream.readline(_MAX_HEADER_BYTES + 1)
        header_bytes += len(line)
        if not line or header_bytes > _MAX_HEADER_BYTES:
            raise AudioBridgeError("Dockur audio WebSocket returned invalid headers")
        if line == b"\r\n":
            break
        try:
            name, value = line.decode("ascii").split(":", 1)
        except (UnicodeDecodeError, ValueError) as exc:
            raise AudioBridgeError("Dockur audio WebSocket returned invalid headers") from exc
        headers[name.strip().lower()] = value.strip()
    expected = base64.b64encode(
        hashlib.sha1((key + _WEBSOCKET_GUID).encode("ascii")).digest()
    ).decode("ascii")
    if headers.get("sec-websocket-accept") != expected:
        raise AudioBridgeError("Dockur audio WebSocket handshake was not authentic")
    if headers.get("upgrade", "").lower() != "websocket":
        raise AudioBridgeError("Dockur audio endpoint returned an invalid upgrade")
    return stream


def _read_exact(stream: BinaryIO, count: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < count:
        chunk = stream.read(count - len(chunks))
        if not chunk:
            raise EOFError("Dockur audio WebSocket closed")
        chunks.extend(chunk)
    return bytes(chunks)


def _read_server_frame(stream: BinaryIO) -> tuple[bool, int, bytes]:
    """Read one bounded, unmasked server-to-client RFC 6455 frame."""
    first, second = _read_exact(stream, 2)
    if first & 0x70:
        raise AudioBridgeError("Dockur audio WebSocket used unsupported RSV bits")
    fin = bool(first & 0x80)
    opcode = first & 0x0F
    if second & 0x80:
        raise AudioBridgeError("Dockur audio WebSocket sent a masked server frame")
    length = second & 0x7F
    if length == 126:
        length = int.from_bytes(_read_exact(stream, 2), "big")
    elif length == 127:
        length = int.from_bytes(_read_exact(stream, 8), "big")
    if length > _MAX_FRAME_BYTES:
        raise AudioBridgeError("Dockur audio WebSocket frame is oversized")
    return fin, opcode, _read_exact(stream, length)


def _send_control_frame(sock: socket.socket, opcode: int, payload: bytes) -> None:
    if len(payload) > 125:
        raise AudioBridgeError("WebSocket control frame is oversized")
    mask = secrets.token_bytes(4)
    encoded = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    sock.sendall(bytes([0x80 | opcode, 0x80 | len(payload)]) + mask + encoded)


def _stream_pcm(sock: socket.socket, stream: BinaryIO, output: BinaryIO) -> None:
    fragmented = bytearray()
    fragment_opcode: int | None = None
    while True:
        fin, opcode, payload = _read_server_frame(stream)
        if opcode == 0x8:
            return
        if opcode == 0x9:
            _send_control_frame(sock, 0xA, payload)
            continue
        if opcode == 0xA:
            continue
        if opcode == 0x2:
            if fragment_opcode is not None:
                raise AudioBridgeError("Dockur audio WebSocket interleaved messages")
            if fin:
                output.write(payload)
                continue
            fragment_opcode = opcode
            fragmented.extend(payload)
        elif opcode == 0x0 and fragment_opcode == 0x2:
            fragmented.extend(payload)
            if len(fragmented) > _MAX_FRAME_BYTES:
                raise AudioBridgeError("Dockur audio WebSocket message is oversized")
            if fin:
                output.write(fragmented)
                fragmented.clear()
                fragment_opcode = None
        elif opcode == 0x1:
            # The endpoint may send non-audio status text; it is not PCM.
            fragment_opcode = 0x1 if not fin else None
            fragmented.clear()
        elif opcode == 0x0 and fragment_opcode == 0x1:
            if fin:
                fragment_opcode = None
        else:
            raise AudioBridgeError("Dockur audio WebSocket sent an unsupported frame")


def play_once(host: str, port: int, pw_cat: str) -> None:
    """Connect once and play until the VM or player closes the stream."""
    with socket.create_connection((host, port), timeout=5) as sock:
        sock.settimeout(10)
        stream = websocket_handshake(sock, host=host, port=port)
        player = subprocess.Popen(
            audio_player_command(pw_cat),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        try:
            if player.stdin is None:
                raise AudioBridgeError("PipeWire player did not expose stdin")
            _stream_pcm(sock, stream, player.stdin)
        finally:
            stream.close()
            if player.stdin is not None:
                try:
                    player.stdin.close()
                except OSError:
                    pass
            try:
                player.wait(timeout=2)
            except subprocess.TimeoutExpired:
                player.terminate()
                try:
                    player.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    player.kill()
                    player.wait(timeout=2)


def _audio_process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    return b"wayseam.present.audio" in cmdline


def ensure_audio_bridge() -> bool:
    """Start the singleton bridge if needed; never block application launch."""
    pw_cat = shutil.which("pw-cat")
    if pw_cat is None:
        return False
    directory = runtime_dir()
    directory.mkdir(parents=True, exist_ok=True)
    pid_path = directory / "wayseam-audio.pid"
    start_lock_path = directory / "wayseam-audio-start.lock"
    with start_lock_path.open("a+b") as start_lock:
        fcntl.flock(start_lock.fileno(), fcntl.LOCK_EX)
        try:
            pid = int(pid_path.read_text(encoding="ascii").strip())
        except (FileNotFoundError, OSError, ValueError):
            pid = 0
        if _audio_process_alive(pid):
            return True
        source_root = str(Path(__file__).resolve().parents[2])
        env = {
            key: value
            for key, value in os.environ.items()
            if key in {
                "HOME",
                "LANG",
                "LC_ALL",
                "PATH",
                "PIPEWIRE_REMOTE",
                "PYTHONPATH",
                "XDG_RUNTIME_DIR",
            }
        }
        inherited = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = source_root + (os.pathsep + inherited if inherited else "")
        log_path = directory / "wayseam-audio.log"
        with log_path.open("ab", buffering=0) as log_stream:
            process = subprocess.Popen(
                [sys.executable, "-m", "wayseam.present.audio", "--pw-cat", pw_cat],
                stdin=subprocess.DEVNULL,
                stdout=log_stream,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,
            )
        pid_path.write_text(f"{process.pid}\n", encoding="ascii")
        time.sleep(0.05)
        return process.poll() is None


def run_bridge(host: str, port: int, pw_cat: str) -> int:
    directory = runtime_dir()
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / "wayseam-audio.lock"
    with lock_path.open("a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        stopping = False

        def stop(_signum: int, _frame: object) -> None:
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        while not stopping:
            try:
                play_once(host, port, pw_cat)
            except (AudioBridgeError, EOFError, OSError, BrokenPipeError):
                pass
            if not stopping:
                time.sleep(2)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Play the Wayseam guest audio stream")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8006)
    parser.add_argument("--pw-cat", default=shutil.which("pw-cat") or "pw-cat")
    args = parser.parse_args(argv)
    if args.host not in {"127.0.0.1", "::1", "localhost"}:
        parser.error("audio bridge only accepts a loopback host")
    if not (1 <= args.port <= 65535):
        parser.error("--port must be between 1 and 65535")
    return run_bridge(args.host, args.port, args.pw_cat)


if __name__ == "__main__":
    raise SystemExit(main())
