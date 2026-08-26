# SPDX-License-Identifier: MIT
"""Tests for the host-side AgentClient (Phase 1: /health, Phase 2: /exec)."""

from __future__ import annotations

import base64
import io
import json
import struct
from urllib import error as urllib_error

import pytest

from wayseam.config import Config
from wayseam.guest.agent import (
    AGENT_PORT,
    AgentAuthError,
    AgentBusyError,
    AgentClient,
    AgentError,
    AgentTimeoutError,
    AgentUnavailableError,
    AgentWindowGoneError,
    ExecResult,
    WayseamClipboard,
    WayseamFrame,
    WayseamSessionState,
    WayseamShmSlot,
    WayseamTopLevel,
    decode_wayseam_delta,
    decode_wayseam_rle,
)


class _FakeResponse:
    """Minimal context-manager stand-in for urllib's HTTPResponse."""

    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = body
        self.status = status

    def read(self, size: int = -1) -> bytes:
        return self._body if size < 0 else self._body[:size]

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *a: object) -> None:
        pass


@pytest.fixture
def cfg() -> Config:
    return Config()


@pytest.fixture
def client(cfg: Config) -> AgentClient:
    return AgentClient(cfg)


def _patch_urlopen(monkeypatch, fake):
    monkeypatch.setattr("wayseam.guest.agent.urllib_request.urlopen", fake)


def _raise_http_error(req, code: int, body: bytes):
    raise urllib_error.HTTPError(req.full_url, code, "error", {}, io.BytesIO(body))


class TestHealth:
    def test_happy_path_returns_parsed_json(self, monkeypatch, client):
        body = json.dumps({"ok": True, "version": "0.2.2"}).encode("utf-8")
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["timeout"] = timeout
            captured["headers"] = dict(req.header_items())
            return _FakeResponse(body, status=200)

        _patch_urlopen(monkeypatch, fake_urlopen)

        result = client.health()

        assert result == {"ok": True, "version": "0.2.2"}
        assert captured["url"] == "http://127.0.0.1:8765/health"
        assert captured["method"] == "GET"
        assert captured["timeout"] == 5.0
        # No Authorization header on /health.
        header_keys = {k.lower() for k in captured["headers"]}
        assert "authorization" not in header_keys

    def test_no_token_succeeds(self, monkeypatch, tmp_path, cfg):
        """/health must work even when ~/.config/wayseam/agent_token.txt is absent."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        client = AgentClient(cfg)
        body = b'{"ok": true}'

        def fake_urlopen(req, timeout=None):
            return _FakeResponse(body, status=200)

        _patch_urlopen(monkeypatch, fake_urlopen)

        result = client.health()
        assert result == {"ok": True}

    def test_connection_refused_raises_unavailable(self, monkeypatch, client):
        def fake_urlopen(req, timeout=None):
            raise urllib_error.URLError(ConnectionRefusedError("connection refused"))

        _patch_urlopen(monkeypatch, fake_urlopen)

        with pytest.raises(AgentUnavailableError, match="unreachable"):
            client.health()

    def test_500_raises_unavailable(self, monkeypatch, client):
        def fake_urlopen(req, timeout=None):
            raise urllib_error.HTTPError(
                req.full_url, 500, "Internal Server Error", hdrs=None, fp=io.BytesIO(b"")
            )

        _patch_urlopen(monkeypatch, fake_urlopen)

        with pytest.raises(AgentUnavailableError, match="500"):
            client.health()

    def test_503_raises_unavailable(self, monkeypatch, client):
        def fake_urlopen(req, timeout=None):
            raise urllib_error.HTTPError(
                req.full_url, 503, "Service Unavailable", hdrs=None, fp=io.BytesIO(b"")
            )

        _patch_urlopen(monkeypatch, fake_urlopen)

        with pytest.raises(AgentUnavailableError, match="503"):
            client.health()

    def test_timeout_raises_timeout(self, monkeypatch, client):
        def fake_urlopen(req, timeout=None):
            raise TimeoutError("timed out")

        _patch_urlopen(monkeypatch, fake_urlopen)

        with pytest.raises(AgentTimeoutError):
            client.health()

    def test_urlerror_socket_timeout_raises_timeout(self, monkeypatch, client):
        def fake_urlopen(req, timeout=None):
            raise urllib_error.URLError(TimeoutError("timed out"))

        _patch_urlopen(monkeypatch, fake_urlopen)

        with pytest.raises(AgentTimeoutError):
            client.health()

    def test_non_json_raises_unavailable(self, monkeypatch, client):
        def fake_urlopen(req, timeout=None):
            return _FakeResponse(b"<html>not json</html>", status=200)

        _patch_urlopen(monkeypatch, fake_urlopen)

        with pytest.raises(AgentUnavailableError, match="non-JSON"):
            client.health()

    def test_401_raises_auth_error(self, monkeypatch, client):
        """/health is unauthenticated, but if a misconfigured server returns
        401 anyway, surface it as AgentAuthError so callers can disambiguate."""

        def fake_urlopen(req, timeout=None):
            raise urllib_error.HTTPError(
                req.full_url, 401, "Unauthorized", hdrs=None, fp=io.BytesIO(b"")
            )

        _patch_urlopen(monkeypatch, fake_urlopen)

        with pytest.raises(AgentAuthError):
            client.health()


class TestTokenLazyLoad:
    def test_token_missing_raises_unavailable(self, monkeypatch, tmp_path, cfg):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        client = AgentClient(cfg)

        with pytest.raises(AgentUnavailableError, match="missing"):
            client._token()

    def test_token_empty_raises_unavailable(self, monkeypatch, tmp_path, cfg):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        path = tmp_path / "wayseam" / "agent_token.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")

        client = AgentClient(cfg)

        with pytest.raises(AgentUnavailableError, match="empty"):
            client._token()

    def test_auth_ready_reports_missing_token(self, monkeypatch, tmp_path, cfg):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        client = AgentClient(cfg)

        ok, detail = client.auth_ready()

        assert ok is False
        assert "missing" in detail

    def test_auth_ready_ok_when_token_exists(self, monkeypatch, tmp_path, cfg):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        path = tmp_path / "wayseam" / "agent_token.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("deadbeef" * 8)
        client = AgentClient(cfg)

        ok, detail = client.auth_ready()

        assert ok is True
        assert detail == ""

    def test_token_loaded_and_cached(self, monkeypatch, tmp_path, cfg):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        path = tmp_path / "wayseam" / "agent_token.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("deadbeef" * 8)

        client = AgentClient(cfg)
        first = client._token()
        # Delete file — cache should still serve.
        path.unlink()
        second = client._token()

        assert first == second == "deadbeef" * 8


class TestExec:
    @pytest.fixture
    def authed_client(self, cfg: Config) -> AgentClient:
        """Client with a pre-cached token so _token() never touches disk."""
        return AgentClient(cfg, token="cafebabe" * 8)

    def test_exec_happy_path(self, monkeypatch, authed_client):
        body = json.dumps({"rc": 0, "stdout": "ok\n", "stderr": ""}).encode("utf-8")
        captured: dict = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["timeout"] = timeout
            captured["headers"] = dict(req.header_items())
            captured["body"] = req.data
            return _FakeResponse(body, status=200)

        _patch_urlopen(monkeypatch, fake_urlopen)

        result = authed_client.exec("Write-Output ok", timeout=15)

        assert isinstance(result, ExecResult)
        assert result.rc == 0
        assert result.stdout == "ok\n"
        assert result.stderr == ""
        assert result.ok is True
        assert captured["url"] == "http://127.0.0.1:8765/exec"
        assert captured["method"] == "POST"
        assert captured["timeout"] == 20.0  # timeout + five-second response grace
        # Authorization header present.
        header_keys = {k.lower(): v for k, v in captured["headers"].items()}
        assert header_keys["authorization"] == "Bearer " + "cafebabe" * 8
        # Body is JSON with base64-encoded script.
        sent = json.loads(captured["body"].decode("utf-8"))
        assert sent["timeout_sec"] == 15
        assert base64.b64decode(sent["script"]).decode("utf-8") == "Write-Output ok"


class TestWayseamFrame:
    @pytest.fixture
    def authed_client(self, cfg: Config) -> AgentClient:
        return AgentClient(cfg, token="cafebabe" * 8)

    def test_window_frame_returns_lossless_png(self, monkeypatch, authed_client):
        png = b"\x89PNG\r\n\x1a\n" + b"frame"
        captured: dict = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["timeout"] = timeout
            captured["headers"] = dict(req.header_items())
            return _FakeResponse(png, status=200)

        _patch_urlopen(monkeypatch, fake_urlopen)

        assert authed_client.window_frame(0x60436) == png
        assert captured["url"] == "http://127.0.0.1:8765/wayseam/frame?hwnd=0x60436"
        assert captured["method"] == "GET"
        assert captured["timeout"] == 3.0
        headers = {k.lower(): v for k, v in captured["headers"].items()}
        assert headers["authorization"] == "Bearer " + "cafebabe" * 8
        assert headers["accept"] == "image/png"

    def test_window_frame_bgra_returns_lossless_pixels(self, monkeypatch, authed_client):
        pixels = bytes.fromhex("102030ff102030ff506070ff")
        body = (
            struct.pack("<4sIIIIIIII", b"WSD1", 3, 1, 12, 1, 0, 0, 3, 1)
            + pixels
        )
        captured: dict = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["headers"] = dict(req.header_items())
            return _FakeResponse(body, status=200)

        _patch_urlopen(monkeypatch, fake_urlopen)

        frame = authed_client.window_frame_bgra(
            0x60436,
            stream_id="a" * 32,
            base_sequence=0,
            previous_pixels=None,
        )

        assert frame == WayseamFrame(
            width=3,
            height=1,
            stride=12,
            pixels=pixels,
            wire_digest=frame.wire_digest,
            server_sequence=1,
            changed=True,
            wire_size=len(body),
        )
        assert isinstance(frame.pixels, bytearray)
        assert captured["url"] == (
            "http://127.0.0.1:8765/wayseam/frame?hwnd=0x60436&encoding=delta"
            "&stream=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa&base=0"
        )
        headers = {key.lower(): value for key, value in captured["headers"].items()}
        assert headers["accept"] == "application/x-wayseam-bgra-delta"

    def test_window_frame_bgra_accepts_full_frame_after_resize(
        self, monkeypatch, authed_client,
    ):
        pixels = bytes.fromhex("102030ff506070ff")
        body = (
            struct.pack("<4sIIIIIIII", b"WSD1", 2, 1, 8, 8, 0, 0, 2, 1)
            + pixels
        )
        _patch_urlopen(
            monkeypatch,
            lambda req, timeout=None: _FakeResponse(body, status=200),
        )

        frame = authed_client.window_frame_bgra(
            0x60436,
            stream_id="a" * 32,
            base_sequence=7,
            previous_pixels=b"old-size-frame",
        )

        assert frame.width == 2
        assert frame.height == 1
        assert frame.pixels == pixels
        assert frame.server_sequence == 8

    @pytest.mark.parametrize(
        "body",
        [
            b"WSR1",
            struct.pack("<4sIII", b"BAD!", 1, 1, 4) + b"\x80\0\0\0\0",
            struct.pack("<4sIII", b"WSR1", 1, 1, 8) + b"\x80\0\0\0\0",
            struct.pack("<4sIII", b"WSR1", 1, 1, 4) + b"\x80\0\0",
        ],
    )
    def test_wayseam_rle_rejects_malformed_frames(self, body):
        with pytest.raises(AgentError, match="RLE"):
            decode_wayseam_rle(body)

    @pytest.mark.parametrize("hwnd", [0, -1, True, "0x60436"])
    def test_window_frame_rejects_invalid_hwnd(self, authed_client, hwnd):
        with pytest.raises(ValueError, match="positive integer"):
            authed_client.window_frame(hwnd)

    def test_window_frame_rejects_non_png(self, monkeypatch, authed_client):
        _patch_urlopen(
            monkeypatch,
            lambda req, timeout=None: _FakeResponse(b'{"error":"capture_failed"}', status=200),
        )

        with pytest.raises(AgentError, match="non-PNG"):
            authed_client.window_frame(1)

    def test_window_frame_reports_closed_hwnd(self, monkeypatch, authed_client):
        def fake_urlopen(req, timeout=None):
            raise urllib_error.HTTPError(
                req.full_url,
                410,
                "Gone",
                {},
                io.BytesIO(b'{"error":"window_gone"}'),
            )

        _patch_urlopen(monkeypatch, fake_urlopen)

        with pytest.raises(AgentWindowGoneError):
            authed_client.window_frame_bgra(
                0x60436,
                stream_id="a" * 32,
                base_sequence=0,
                previous_pixels=None,
            )

    def test_popup_frame_requests_bounded_alpha_repair(self, monkeypatch, authed_client):
        captured: dict = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            return _FakeResponse(b"\x89PNG\r\n\x1a\nframe", status=200)

        _patch_urlopen(monkeypatch, fake_urlopen)
        authed_client.window_frame(0x130398, popup_alpha=True)

        assert captured["url"] == (
            "http://127.0.0.1:8765/wayseam/frame?hwnd=0x130398&alpha=border"
        )

    def test_owned_windows_returns_root_and_transient_geometry(
        self, monkeypatch, authed_client,
    ):
        payload = json.dumps({
            "root": "0x60436",
            "left": 541,
            "top": 166,
            "width": 925,
            "height": 500,
            "windows": [{
                "hwnd": 0x130398,
                "owner": 0x60436,
                "title": "PopupHost",
                "class_name": "Microsoft.UI.Content.PopupWindowSiteBridge",
                "left": 543,
                "top": 231,
                "width": 284,
                "height": 429,
            }],
        }).encode()
        captured: dict = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["timeout"] = timeout
            return _FakeResponse(payload, status=200)

        _patch_urlopen(monkeypatch, fake_urlopen)
        root, windows = authed_client.owned_windows(0x60436)

        assert root == {"left": 541, "top": 166, "width": 925, "height": 500}
        assert len(windows) == 1
        assert windows[0].hwnd == 0x130398
        assert windows[0].owner == 0x60436
        assert windows[0].left - root["left"] == 2
        assert windows[0].top - root["top"] == 65
        assert captured == {
            "url": "http://127.0.0.1:8765/wayseam/windows?root=0x60436",
            "timeout": 3.0,
        }

    def test_top_level_windows_returns_process_identity_without_shell_metadata(
        self, monkeypatch, authed_client,
    ):
        payload = json.dumps({
            "windows": [{
                "hwnd": 0xD03DE,
                "owner": 0xA11CE,
                "pid": 4400,
                "process_path": r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                "title": "Browser",
                "class_name": "Chrome_WidgetWin_1",
                "left": 100,
                "top": 120,
                "width": 1200,
                "height": 800,
            }],
        }).encode("utf-8")
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["timeout"] = timeout
            return _FakeResponse(payload, status=200)

        _patch_urlopen(monkeypatch, fake_urlopen)

        windows = authed_client.top_level_windows()

        assert windows == [
            WayseamTopLevel(
                hwnd=0xD03DE,
                pid=4400,
                process_path=(
                    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
                ),
                title="Browser",
                class_name="Chrome_WidgetWin_1",
                left=100,
                top=120,
                width=1200,
                height=800,
                owner=0xA11CE,
            )
        ]
        assert captured == {
            "url": "http://127.0.0.1:8765/wayseam/top-level",
            "timeout": 3.0,
        }

    def test_window_cursor_returns_png_shape_and_hotspot(self, monkeypatch, authed_client):
        png = b"\x89PNG\r\n\x1a\n" + b"cursor"
        payload = json.dumps({
            "visible": True,
            "shape": "0xabc",
            "x": 320,
            "y": 480,
            "hot_x": 7,
            "hot_y": 8,
            "width": 32,
            "height": 32,
            "png": base64.b64encode(png).decode("ascii"),
        }).encode("utf-8")
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["timeout"] = timeout
            return _FakeResponse(payload, status=200)

        _patch_urlopen(monkeypatch, fake_urlopen)

        cursor = authed_client.window_cursor(0x60436)

        assert cursor.visible is True
        assert cursor.shape == 0xABC
        assert (cursor.x, cursor.y) == (320, 480)
        assert (cursor.hot_x, cursor.hot_y) == (7, 8)
        assert cursor.png == png
        assert captured == {
            "url": "http://127.0.0.1:8765/wayseam/cursor?root=0x60436",
            "timeout": 3.0,
        }

    def test_window_cursor_rejects_non_png_shape(self, monkeypatch, authed_client):
        payload = json.dumps({
            "visible": True,
            "shape": "0xabc",
            "x": 1,
            "y": 1,
            "hot_x": 0,
            "hot_y": 0,
            "width": 32,
            "height": 32,
            "png": base64.b64encode(b"not a png").decode("ascii"),
        }).encode("utf-8")
        _patch_urlopen(
            monkeypatch,
            lambda req, timeout=None: _FakeResponse(payload, status=200),
        )

        with pytest.raises(AgentError, match="invalid PNG"):
            authed_client.window_cursor(0x60436)

    def test_pointer_input_posts_bounded_event(self, monkeypatch, authed_client):
        captured: dict = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _FakeResponse(b'{"ok":true}', status=200)

        _patch_urlopen(monkeypatch, fake_urlopen)
        authed_client.window_pointer_input(0x60436, x=20, y=30, action="down", button=1)

        assert captured == {
            "url": "http://127.0.0.1:8765/wayseam/input",
            "method": "POST",
            "body": {"hwnd": "0x60436", "x": 20, "y": 30, "action": "down", "button": 1},
        }

    def test_pointer_input_posts_bounded_wheel_deltas(self, monkeypatch, authed_client):
        captured: dict = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _FakeResponse(b'{"ok":true}', status=200)

        _patch_urlopen(monkeypatch, fake_urlopen)
        authed_client.window_pointer_input(
            0x60436, x=20, y=30, action="wheel", delta_y=-240, delta_x=120,
        )

        assert captured == {
            "url": "http://127.0.0.1:8765/wayseam/input",
            "body": {
                "hwnd": "0x60436", "x": 20, "y": 30, "action": "wheel",
                "button": 0, "delta_x": 120, "delta_y": -240,
            },
        }

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"delta_y": 0}, "non-zero"),
            ({"delta_y": 12001}, "range"),
            ({"delta_x": -12001}, "range"),
            ({"delta_y": 1.5}, "integers"),
            ({"delta_y": True}, "integers"),
        ],
    )
    def test_pointer_input_rejects_unbounded_wheel(self, authed_client, kwargs, message):
        with pytest.raises(ValueError, match=message):
            authed_client.window_pointer_input(1, x=0, y=0, action="wheel", **kwargs)

    def test_clipboard_get_skips_unchanged_sequence(self, monkeypatch, authed_client):
        captured: dict = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            return _FakeResponse(b'{"ok":true,"sequence":41,"changed":false}', status=200)

        _patch_urlopen(monkeypatch, fake_urlopen)
        clip = authed_client.clipboard_get(since=41)

        assert captured == {
            "url": "http://127.0.0.1:8765/wayseam/clipboard?since=41",
            "method": "GET",
        }
        assert clip.sequence == 41 and clip.changed is False and clip.text == ""

    def test_clipboard_get_returns_text_when_changed(self, monkeypatch, authed_client):
        body = json.dumps(
            {"ok": True, "sequence": 42, "changed": True, "text": "h\u00e9llo", "truncated": False}
        ).encode("utf-8")
        _patch_urlopen(monkeypatch, lambda req, timeout=None: _FakeResponse(body, status=200))

        clip = authed_client.clipboard_get()

        assert clip == WayseamClipboard(sequence=42, changed=True, text="héllo")

    def test_clipboard_get_reports_busy_guest(self, monkeypatch, authed_client):
        _patch_urlopen(
            monkeypatch,
            lambda req, timeout=None: _raise_http_error(req, 423, b'{"error":"clipboard_busy"}'),
        )
        with pytest.raises(AgentBusyError):
            authed_client.clipboard_get()

    def test_clipboard_set_posts_text_and_returns_sequence(self, monkeypatch, authed_client):
        captured: dict = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _FakeResponse(b'{"ok":true,"sequence":43}', status=200)

        _patch_urlopen(monkeypatch, fake_urlopen)
        assert authed_client.clipboard_set("copied\nfrom linux") == 43
        assert captured == {
            "url": "http://127.0.0.1:8765/wayseam/clipboard",
            "body": {"text": "copied\nfrom linux"},
        }

    @pytest.mark.parametrize(
        "text", [b"bytes", "x" * (1_048_576 + 1), "lone\udc80surrogate"],
    )
    def test_clipboard_set_rejects_invalid_text(self, authed_client, text):
        with pytest.raises(ValueError):
            authed_client.clipboard_set(text)

    def test_session_state_and_console_reconnect(self, monkeypatch, authed_client):
        captured: list = []

        def fake_urlopen(req, timeout=None):
            captured.append((req.get_method(), req.data))
            payload = {
                "ok": True, "session_id": 1, "state": "active", "state_code": 0,
                "station": "Console", "console": True,
                "reconnected": req.get_method() == "POST",
            }
            return _FakeResponse(json.dumps(payload).encode("utf-8"), status=200)

        _patch_urlopen(monkeypatch, fake_urlopen)
        state = authed_client.session_state()
        reconnected = authed_client.session_reconnect_console()

        assert state == WayseamSessionState(1, "active", "Console", True, False)
        assert reconnected.reconnected is True
        assert captured == [("GET", None), ("POST", b'{"action": "console"}')]

    def test_session_state_normalizes_unknown_states(self, monkeypatch, authed_client):
        body = b'{"ok":true,"session_id":2,"state":"shadow","station":"rdp-tcp#3","console":false}'
        _patch_urlopen(monkeypatch, lambda req, timeout=None: _FakeResponse(body, status=200))

        assert authed_client.session_state() == WayseamSessionState(
            2, "unknown", "rdp-tcp#3", False, False,
        )

    def test_window_resize_posts_bounded_dimensions(self, monkeypatch, authed_client):
        captured: dict = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _FakeResponse(
                b'{"ok":true,"width":941,"height":1048,"canvas_changed":true}',
                status=200,
            )

        _patch_urlopen(monkeypatch, fake_urlopen)
        result = authed_client.window_resize(0x60436, width=941, height=1048)

        assert captured == {
            "url": "http://127.0.0.1:8765/wayseam/resize",
            "body": {"hwnd": "0x60436", "width": 941, "height": 1048},
        }
        assert result == (941, 1048)

    def test_keyboard_input_posts_bounded_virtual_key(self, monkeypatch, authed_client):
        captured: dict = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _FakeResponse(b'{"ok":true}', status=200)

        _patch_urlopen(monkeypatch, fake_urlopen)
        authed_client.window_keyboard_input(
            0x60436,
            action="down",
            virtual_key=0x4B,
            extended=False,
        )

        assert captured == {
            "url": "http://127.0.0.1:8765/wayseam/keyboard",
            "method": "POST",
            "body": {
                "hwnd": "0x60436",
                "action": "down",
                "virtual_key": 0x4B,
                "unicode": 0,
                "extended": False,
            },
        }

    def test_keyboard_input_posts_unicode_scalar(self, monkeypatch, authed_client):
        captured: dict = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _FakeResponse(b'{"ok":true}', status=200)

        _patch_urlopen(monkeypatch, fake_urlopen)
        authed_client.window_keyboard_input(0x60436, action="press", unicode=0xE7)

        assert captured["body"] == {
            "hwnd": "0x60436",
            "action": "press",
            "virtual_key": 0,
            "unicode": 0xE7,
            "extended": False,
        }

    def test_shell_visibility_posts_bounded_mode(self, monkeypatch, authed_client):
        captured: dict = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _FakeResponse(b'{"ok":true,"windows":1}', status=200)

        _patch_urlopen(monkeypatch, fake_urlopen)
        assert authed_client.window_shell_visibility(visible=False) == 1

        assert captured == {
            "url": "http://127.0.0.1:8765/wayseam/shell",
            "body": {"visible": False},
        }

    def test_window_close_posts_exact_hwnd(self, monkeypatch, authed_client):
        captured: dict = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _FakeResponse(b'{"ok":true}', status=200)

        _patch_urlopen(monkeypatch, fake_urlopen)
        authed_client.window_close(0x60436)

        assert captured == {
            "url": "http://127.0.0.1:8765/wayseam/close",
            "body": {"hwnd": "0x60436"},
        }

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"action": "repeat", "virtual_key": 0x41},
            {"action": "down", "virtual_key": 0},
            {"action": "up", "virtual_key": 0x100},
            {"action": "down", "virtual_key": 0x41, "unicode": 0x41},
            {"action": "press", "unicode": 0},
            {"action": "press", "unicode": 0xD800},
            {"action": "press", "unicode": 0x110000},
        ],
    )
    def test_keyboard_input_rejects_invalid_event(self, authed_client, kwargs):
        with pytest.raises(ValueError):
            authed_client.window_keyboard_input(1, **kwargs)

    @pytest.mark.parametrize(
        ("width", "height"),
        [(159, 500), (500, 119), (8193, 500), (500, 8193)],
    )
    def test_window_resize_rejects_unsafe_dimensions(
        self, authed_client, width, height,
    ):
        with pytest.raises(ValueError, match="dimensions"):
            authed_client.window_resize(1, width=width, height=height)

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"x": -1, "y": 0, "action": "move"}, "coordinates"),
            ({"x": 0, "y": 0, "action": "drag"}, "action"),
            ({"x": 0, "y": 0, "action": "down", "button": 0}, "button"),
        ],
    )
    def test_pointer_input_rejects_invalid_event(self, authed_client, kwargs, message):
        with pytest.raises(ValueError, match=message):
            authed_client.window_pointer_input(1, **kwargs)

    def test_exec_deadline_allows_response_grace_at_minimum_timeout(
        self, monkeypatch, authed_client
    ):
        # Given: a successful guest response and the minimum valid timeout.
        body = json.dumps({"rc": 0, "stdout": "", "stderr": ""}).encode("utf-8")
        captured: dict = {}

        def fake_urlopen(req, timeout=None):
            captured["timeout"] = timeout
            captured["body"] = req.data
            return _FakeResponse(body, status=200)

        _patch_urlopen(monkeypatch, fake_urlopen)

        # When: the client executes the script.
        authed_client.exec("Write-Output ok", timeout=1)

        # Then: the guest gets the requested timeout and the client keeps five seconds of grace.
        sent = json.loads(captured["body"].decode("utf-8"))
        assert sent["timeout_sec"] == 1
        assert captured["timeout"] == 6.0

    def test_exec_token_rejected(self, monkeypatch, authed_client):
        def fake_urlopen(req, timeout=None):
            raise urllib_error.HTTPError(
                req.full_url, 401, "Unauthorized", hdrs=None, fp=io.BytesIO(b"")
            )

        _patch_urlopen(monkeypatch, fake_urlopen)

        with pytest.raises(AgentAuthError, match="401"):
            authed_client.exec("Write-Output ok")

    def test_exec_timeout(self, monkeypatch, authed_client):
        def fake_urlopen(req, timeout=None):
            raise TimeoutError("timed out")

        _patch_urlopen(monkeypatch, fake_urlopen)

        with pytest.raises(AgentTimeoutError):
            authed_client.exec("Start-Sleep 90", timeout=5)

    def test_exec_connection_refused(self, monkeypatch, authed_client):
        def fake_urlopen(req, timeout=None):
            raise urllib_error.URLError(ConnectionRefusedError("connection refused"))

        _patch_urlopen(monkeypatch, fake_urlopen)

        with pytest.raises(AgentUnavailableError, match="unreachable"):
            authed_client.exec("Write-Output ok")

    def test_exec_500(self, monkeypatch, authed_client):
        def fake_urlopen(req, timeout=None):
            raise urllib_error.HTTPError(
                req.full_url, 500, "Internal Server Error", hdrs=None, fp=io.BytesIO(b"")
            )

        _patch_urlopen(monkeypatch, fake_urlopen)

        with pytest.raises(AgentUnavailableError, match="500"):
            authed_client.exec("Write-Output ok")

    def test_exec_non_json_response(self, monkeypatch, authed_client):
        def fake_urlopen(req, timeout=None):
            return _FakeResponse(b"<html>boom</html>", status=200)

        _patch_urlopen(monkeypatch, fake_urlopen)

        with pytest.raises(AgentError, match="non-JSON"):
            authed_client.exec("Write-Output ok")




def test_agent_base_url_follows_rdp_ip_manual_backend():
    # #426: manual backend pointed at a VM not on loopback — the agent host
    # must follow cfg.rdp.ip, the same address RDP uses.
    cfg = Config()
    cfg.pod.backend = "manual"
    cfg.rdp.ip = "LTSC11P.local"
    client = AgentClient(cfg)
    assert client.base_url == f"http://LTSC11P.local:{AGENT_PORT}"


def test_agent_base_url_defaults_to_loopback():
    # podman/docker: cfg.rdp.ip defaults to 127.0.0.1 (port forwarded to host
    # loopback), so the agent host stays loopback — unchanged behaviour.
    client = AgentClient(Config())
    assert client.base_url == f"http://127.0.0.1:{AGENT_PORT}"


def test_agent_base_url_brackets_ipv6_literal():
    cfg = Config()
    cfg.rdp.ip = "fe80::1"
    client = AgentClient(cfg)
    assert client.base_url == f"http://[fe80::1]:{AGENT_PORT}"


def test_agent_explicit_base_url_overrides_cfg():
    cfg = Config()
    cfg.rdp.ip = "LTSC11P.local"
    client = AgentClient(cfg, base_url="http://10.0.0.5:9999")
    assert client.base_url == "http://10.0.0.5:9999"


class TestWayseamDeltaInPlace:
    def _delta(self, width, height, x, y, rect_width, rect_height, sequence=1):
        header = struct.pack(
            "<4sIIIIIIII", b"WSD1", width, height, width * 4, sequence,
            x, y, rect_width, rect_height,
        )
        return header + bytes(range(1, 5)) * (rect_width * rect_height)

    def test_partial_delta_updates_caller_canvas_and_reports_damage(self):
        canvas = bytearray(4 * 4 * 4)
        raw = self._delta(4, 4, 1, 2, 2, 1, sequence=5)

        frame = decode_wayseam_delta(raw, canvas, in_place=True)

        assert frame.pixels is canvas
        assert frame.damage == (1, 2, 2, 1)
        assert frame.damage_pixels == bytes(range(1, 5)) * 2
        assert canvas[(2 * 4 + 1) * 4:(2 * 4 + 3) * 4] == bytes(range(1, 5)) * 2
        assert frame.changed and frame.server_sequence == 5

    def test_partial_delta_without_in_place_leaves_previous_untouched(self):
        previous = bytes(4 * 4 * 4)
        frame = decode_wayseam_delta(self._delta(4, 4, 0, 0, 1, 1), previous)
        assert previous == bytes(64)
        assert frame.pixels[:4] == bytes(range(1, 5))
        assert frame.damage == (0, 0, 1, 1)

    def test_full_delta_returns_fresh_canvas_without_damage_rect(self):
        stale = bytearray(4 * 4 * 4)
        frame = decode_wayseam_delta(self._delta(4, 4, 0, 0, 4, 4), stale, in_place=True)
        assert frame.pixels is not stale
        assert frame.damage is None and frame.damage_pixels == b""
        assert bytes(frame.pixels) == bytes(range(1, 5)) * 16

    def test_delta_digest_is_sequence_unique_without_hashing_pixels(self):
        first = decode_wayseam_delta(self._delta(4, 4, 0, 0, 4, 4, sequence=1), None)
        second = decode_wayseam_delta(self._delta(4, 4, 0, 0, 4, 4, sequence=2), None)
        assert first.wire_digest != second.wire_digest
        assert len(first.wire_digest) == 16


class TestWayseamShm:
    @pytest.fixture
    def authed_client(self, cfg: Config) -> AgentClient:
        """Client with a pre-cached token so _token() never touches disk."""
        return AgentClient(cfg, token="cafebabe" * 8)

    def test_shm_assign_returns_validated_slot_geometry(self, monkeypatch, authed_client):
        captured: dict = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _FakeResponse(
                b'{"ok":true,"slot":1,"slot_count":3,"slot_size":22020096,'
                b'"slot0_offset":1048576}',
                status=200,
            )

        _patch_urlopen(monkeypatch, fake_urlopen)
        slot = authed_client.shm_assign(0x603A4)

        assert captured == {
            "url": "http://127.0.0.1:8765/wayseam/shm",
            "body": {"action": "assign", "hwnd": "0x603a4"},
        }
        assert slot == WayseamShmSlot(1, 3, 22020096, 1048576)
        assert slot.offset() == 1048576 + 22020096

    def test_shm_assign_rejects_bogus_geometry(self, monkeypatch, authed_client):
        _patch_urlopen(
            monkeypatch,
            lambda req, timeout=None: _FakeResponse(
                b'{"ok":true,"slot":9,"slot_count":3,"slot_size":1,"slot0_offset":0}',
                status=200,
            ),
        )
        with pytest.raises(AgentError, match="slot geometry"):
            authed_client.shm_assign(1)

    def test_shm_release_posts_release_action(self, monkeypatch, authed_client):
        captured: dict = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _FakeResponse(b'{"ok":true,"detail":"stopped slot 1"}', status=200)

        _patch_urlopen(monkeypatch, fake_urlopen)
        authed_client.shm_release(0x603A4)
        assert captured["body"] == {"action": "release", "hwnd": "0x603a4"}

    def test_shm_status_returns_bounded_string(self, monkeypatch, authed_client):
        _patch_urlopen(
            monkeypatch,
            lambda req, timeout=None: _FakeResponse(
                b'{"ok":true,"status":"mapped=True slot0=hwnd:0x1"}', status=200,
            ),
        )
        assert authed_client.shm_status() == "mapped=True slot0=hwnd:0x1"
