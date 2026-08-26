# SPDX-License-Identifier: MIT

from __future__ import annotations

import base64
import hashlib
import io

from wayseam.present.audio import (
    _WEBSOCKET_GUID,
    _read_server_frame,
    audio_player_command,
    websocket_handshake,
)


class _Socket:
    def __init__(self, response: bytes) -> None:
        self.response = io.BytesIO(response)
        self.sent = bytearray()

    def sendall(self, payload: bytes) -> None:
        self.sent.extend(payload)

    def makefile(self, _mode: str):
        return self.response


def test_websocket_handshake_validates_dockur_audio_upgrade() -> None:
    key = "dGhlIHNhbXBsZSBub25jZQ=="
    accept = base64.b64encode(
        hashlib.sha1((key + _WEBSOCKET_GUID).encode("ascii")).digest()
    ).decode("ascii")
    sock = _Socket(
        (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        ).encode("ascii")
    )

    stream = websocket_handshake(sock, host="127.0.0.1", port=8006, key=key)

    assert stream is sock.response
    request = bytes(sock.sent)
    assert request.startswith(b"GET /audio HTTP/1.1\r\n")
    assert b"Host: 127.0.0.1:8006\r\n" in request
    assert f"Sec-WebSocket-Key: {key}\r\n".encode() in request


def test_read_server_frame_accepts_unmasked_binary_pcm() -> None:
    pcm = b"\x01\x02\x03\x04" * 40
    frame = bytes([0x82, 126]) + len(pcm).to_bytes(2, "big") + pcm

    fin, opcode, payload = _read_server_frame(io.BytesIO(frame))

    assert fin is True
    assert opcode == 2
    assert payload == pcm


def test_audio_player_uses_raw_dockur_pcm_format() -> None:
    assert audio_player_command("/usr/bin/pw-cat") == [
        "/usr/bin/pw-cat",
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
