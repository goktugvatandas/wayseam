# SPDX-License-Identifier: MIT
"""Host-side HTTP client for the guest agent (agent-v2).

Complements ``config/oem/agent/agent.ps1`` running inside the Windows VM.
The guest binds an HTTP listener on ``127.0.0.1:8765``; both forwarding
chain legs (QEMU hostfwd via dockur ``USER_PORTS``, plus the compose
``ports:`` mapping) make that listener reachable from the host on the
same loopback port.

Phase 1 implements only ``GET /health`` (no auth) — the readiness
signal that gates everything downstream. ``health()`` responding is the
single, definitive proof that ``install.bat`` finished, ``rdprrap``
activated, and the agent could bind its listener.

Later phases will add ``/exec``, ``/events``, ``/apply``, ``/discover``.
The Phase 2+ surface area is sketched as helpers/exception types in
this module so callers don't churn between phases.

See ``docs/design/AGENT_V2_DESIGN.md`` for the full design.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import socket
import struct
from dataclasses import dataclass
from typing import Any
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from wayseam.config import Config
from wayseam.token import token_path

log = logging.getLogger(__name__)


# Host-side single source of truth for the agent port. Everything Python that
# talks about the guest agent listener (compose port mapping, urlacl strings
# we push into the guest, /health probes) should derive from this constant
# rather than re-literal 8765. The guest side -- agent.ps1, install.bat,
# agent-keepalive.ps1, agent-respawn.ps1 -- carries its own literal and is
# documented as the paired second SoT (PowerShell can't import a Python
# constant). Changing the port means editing here AND those PS1/BAT files.
AGENT_PORT = 8765
_EXEC_RESPONSE_GRACE = 5.0


class AgentError(RuntimeError):
    """Base class for all AgentClient failures."""


class AgentUnavailableError(AgentError):
    """Agent is unreachable: connection refused, timeout, 5xx, no token, etc.

    The host should treat this as "still booting" or "guest agent not yet
    up" and avoid firing speculative FreeRDP probes (anti-goal #3).
    """


class AgentAuthError(AgentError):
    """Agent rejected the request with 401/403 — token mismatch or missing.

    Phase 1's ``/health`` is unauthenticated, so this is reserved for
    Phase 2+ endpoints. Defined now so callers don't need to change
    their except-clauses between phases.
    """


class AgentTimeoutError(AgentError):
    """Server accepted the request but didn't finish before the deadline.

    Distinct from ``AgentUnavailableError``'s connect-timeout case: the
    listener was up and replied with headers but the work itself
    exceeded the per-request budget.
    """


class AgentWindowGoneError(AgentError):
    """The requested Wayseam HWND no longer exists in the guest."""


class AgentBusyError(AgentError):
    """The guest resource (for example the clipboard) is momentarily locked."""


@dataclass(frozen=True)
class ExecResult:
    """Outcome of a guest-side script execution via ``/exec``.

    Fields mirror the agent's JSON response (``rc``/``stdout``/``stderr``).
    Transport-level failures (channel down, auth, timeout) raise the
    matching ``Agent*Error`` instead of returning here.
    """

    rc: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.rc == 0


@dataclass(frozen=True)
class WayseamWindow:
    """A visible guest window owned by a Wayseam root HWND."""

    hwnd: int
    owner: int
    title: str
    class_name: str
    left: int
    top: int
    width: int
    height: int


@dataclass(frozen=True)
class WayseamTopLevel:
    """A visible guest root window and its executable identity."""

    hwnd: int
    pid: int
    process_path: str
    title: str
    class_name: str
    left: int
    top: int
    width: int
    height: int
    owner: int = 0


@dataclass(frozen=True)
class WayseamFrame:
    """Decoded lossless BGRA pixels from one independent guest HWND."""

    width: int
    height: int
    stride: int
    pixels: bytes | bytearray
    wire_digest: bytes
    server_sequence: int = 0
    changed: bool = True
    wire_size: int = 0
    # Damage carried by this update. ``None`` means the whole surface must be
    # (re)presented from ``pixels``; otherwise ``(x, y, width, height)`` with
    # ``damage_pixels`` holding exactly that rectangle's tightly packed BGRA
    # rows, so a presenter can upload only the changed region.
    damage: tuple[int, int, int, int] | None = None
    damage_pixels: bytes = b""


_WAYSEAM_RLE_HEADER = struct.Struct("<4sIII")


def decode_wayseam_rle(raw: bytes) -> WayseamFrame:
    """Decode the bounded lossless pixel RLE used by the live surface path."""
    if len(raw) < _WAYSEAM_RLE_HEADER.size:
        raise AgentError("/wayseam/frame returned a truncated RLE header")
    magic, width, height, stride = _WAYSEAM_RLE_HEADER.unpack_from(raw)
    if magic != b"WSR1":
        raise AgentError("/wayseam/frame returned an invalid RLE magic")
    if not (1 <= width <= 8192 and 1 <= height <= 8192 and stride == width * 4):
        raise AgentError("/wayseam/frame returned invalid RLE dimensions")
    output_size = stride * height
    if output_size > 128 * 1024 * 1024:
        raise AgentError("/wayseam/frame returned an oversized RLE frame")

    encoded = memoryview(raw)[_WAYSEAM_RLE_HEADER.size:]
    pixels = bytearray(output_size)
    source = 0
    target = 0
    while source < len(encoded) and target < output_size:
        tag = encoded[source]
        source += 1
        count = (tag & 0x7F) + 1
        byte_count = count * 4
        if target + byte_count > output_size:
            raise AgentError("/wayseam/frame RLE expands past its declared dimensions")
        if tag & 0x80:
            if source + 4 > len(encoded):
                raise AgentError("/wayseam/frame returned a truncated RLE run")
            pixels[target:target + byte_count] = bytes(encoded[source:source + 4]) * count
            source += 4
        else:
            if source + byte_count > len(encoded):
                raise AgentError("/wayseam/frame returned a truncated RLE literal")
            pixels[target:target + byte_count] = encoded[source:source + byte_count]
            source += byte_count
        target += byte_count
    if target != output_size or source != len(encoded):
        raise AgentError("/wayseam/frame returned an incomplete RLE frame")
    return WayseamFrame(
        width=width,
        height=height,
        stride=stride,
        pixels=bytes(pixels),
        wire_digest=hashlib.blake2s(raw, digest_size=16).digest(),
    )


_WAYSEAM_DELTA_HEADER = struct.Struct("<4sIIIIIIII")


def decode_wayseam_delta(
    raw: bytes,
    previous: bytes | bytearray | None,
    *,
    in_place: bool = False,
) -> WayseamFrame:
    """Apply one bounded BGRA rectangle update to the previous HWND frame.

    With ``in_place`` and a ``bytearray`` canvas of matching size, the damaged
    rows are written straight into ``previous`` and the returned frame shares
    it; a hover-sized delta then costs its own few kilobytes instead of a
    full-surface copy. Callers that keep the canvas across frames must treat
    it as mutable.
    """
    if len(raw) < _WAYSEAM_DELTA_HEADER.size:
        raise AgentError("/wayseam/frame returned a truncated delta header")
    magic, width, height, stride, sequence, x, y, rect_width, rect_height = (
        _WAYSEAM_DELTA_HEADER.unpack_from(raw)
    )
    if magic != b"WSD1":
        raise AgentError("/wayseam/frame returned an invalid delta magic")
    if not (1 <= width <= 8192 and 1 <= height <= 8192 and stride == width * 4):
        raise AgentError("/wayseam/frame returned invalid delta dimensions")
    output_size = stride * height
    if output_size > 128 * 1024 * 1024:
        raise AgentError("/wayseam/frame returned an oversized delta frame")
    # The header carries the per-stream sequence, so hashing it alone gives a
    # unique, O(1) identity; hashing a full 17 MB frame cost ~20 ms per frame.
    digest = hashlib.blake2s(
        raw[: _WAYSEAM_DELTA_HEADER.size], digest_size=16,
    ).digest()
    if rect_width == 0 or rect_height == 0:
        if rect_width != 0 or rect_height != 0 or previous is None:
            raise AgentError("/wayseam/frame returned an invalid empty delta")
        if len(previous) != output_size or len(raw) != _WAYSEAM_DELTA_HEADER.size:
            raise AgentError("/wayseam/frame returned an inconsistent empty delta")
        return WayseamFrame(
            width, height, stride, previous, digest, sequence, False, len(raw),
        )
    if x + rect_width > width or y + rect_height > height:
        raise AgentError("/wayseam/frame returned an out-of-bounds delta")
    payload = memoryview(raw)[_WAYSEAM_DELTA_HEADER.size:]
    row_size = rect_width * 4
    if len(payload) != row_size * rect_height:
        raise AgentError("/wayseam/frame returned a truncated delta payload")
    full = x == 0 and y == 0 and rect_width == width and rect_height == height
    if previous is None and not full:
        raise AgentError("/wayseam/frame returned a partial delta without a base frame")
    if previous is not None and len(previous) != output_size and not full:
        raise AgentError("/wayseam/frame base dimensions changed without a full frame")
    if full:
        return WayseamFrame(
            width, height, stride, bytearray(payload), digest, sequence, True, len(raw),
        )
    if in_place and isinstance(previous, bytearray) and len(previous) == output_size:
        pixels = previous
    else:
        pixels = bytearray(previous)
    for row in range(rect_height):
        source = row * row_size
        target = (y + row) * stride + x * 4
        pixels[target:target + row_size] = payload[source:source + row_size]
    return WayseamFrame(
        width, height, stride, pixels, digest, sequence, True, len(raw),
        (x, y, rect_width, rect_height), bytes(payload),
    )


@dataclass(frozen=True)
class WayseamClipboard:
    """Guest text clipboard snapshot keyed by Windows' clipboard sequence."""

    sequence: int
    changed: bool
    text: str = ""
    truncated: bool = False


@dataclass(frozen=True)
class WayseamSessionState:
    """Connection state of the guest's interactive (agent) session."""

    session_id: int
    state: str
    station: str
    console: bool
    reconnected: bool = False


@dataclass(frozen=True)
class WayseamShmSlot:
    """Geometry of one assigned IVSHMEM frame-ring slot (ADR 0004)."""

    slot: int
    slot_count: int
    slot_size: int
    slot0_offset: int

    def offset(self) -> int:
        return self.slot0_offset + self.slot * self.slot_size


@dataclass(frozen=True)
class WayseamCursor:
    """Guest cursor image and hotspot position relative to a root HWND."""

    visible: bool
    shape: int
    x: int
    y: int
    hot_x: int
    hot_y: int
    width: int
    height: int
    png: bytes


class AgentClient:
    """HTTP client for the guest agent.

    The agent host follows the *same* address RDP uses — ``cfg.rdp.ip`` — so
    it works on every backend without special-casing (#426). For
    podman/docker that's the default ``127.0.0.1`` (the container publishes
    ``8765`` to host loopback); for the ``manual`` backend pointed at a VM
    that isn't on loopback (e.g. a VMware guest at ``LTSC11P.local``) it's the
    VM's own address, where the agent actually listens — instead of the old
    hard-coded loopback, which left agent-backed features falling back to
    FreeRDP-only even though the agent was reachable.
    """

    # Loopback fallback, kept as a named constant for callers/tests that
    # reference the canonical default. The live base URL is derived per
    # instance from cfg.rdp.ip (see _default_base_url).
    DEFAULT_BASE_URL = f"http://127.0.0.1:{AGENT_PORT}"
    HEALTH_TIMEOUT = 5.0
    FRAME_TIMEOUT = 3.0

    def __init__(
        self,
        cfg: Config,
        *,
        base_url: str | None = None,
        token: str | None = None,
        default_timeout: float = 30.0,
    ) -> None:
        self.cfg = cfg
        self.base_url = (base_url or self._default_base_url(cfg)).rstrip("/")
        self.default_timeout = default_timeout
        self._cached_token = token

    @staticmethod
    def _default_base_url(cfg: Config) -> str:
        """Agent base URL derived from ``cfg.rdp.ip`` (the VM address).

        Mirrors the RDP reachability check so the ``manual`` backend reaches
        the agent at the VM's address; podman/docker keep ``127.0.0.1`` since
        ``cfg.rdp.ip`` defaults to loopback there. The agent always listens on
        :data:`AGENT_PORT` — the host RDP port mapping doesn't apply to it.
        """
        host = (getattr(cfg.rdp, "ip", "") or "").strip() or "127.0.0.1"
        # Bracket a bare IPv6 literal so the URL stays valid.
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = getattr(cfg.pod, "agent_port", AGENT_PORT)
        return f"http://{host}:{port}"

    def _token(self) -> str:
        """Return the bearer token, lazily loaded from the host token file.

        Raises ``AgentUnavailableError`` if the token file is missing or
        empty — without it the client cannot authenticate Phase 2+
        requests, so the agent is functionally unavailable.

        ``health()`` does not call this — Phase 1 ``/health`` is
        unauthenticated. Plumbed now for Phase 2.
        """
        if self._cached_token:
            return self._cached_token
        path = token_path()
        try:
            content = path.read_text(encoding="ascii").strip()
        except FileNotFoundError as e:
            raise AgentUnavailableError(f"agent token file missing: {path}") from e
        except OSError as e:
            raise AgentUnavailableError(f"cannot read agent token: {e}") from e
        if not content:
            raise AgentUnavailableError(f"agent token file is empty: {path}")
        self._cached_token = content
        return content

    def auth_ready(self) -> tuple[bool, str]:
        """Return whether authenticated endpoints can be used.

        This intentionally does not expose the token. It lets transport
        selection verify that /exec can authenticate after an unauthenticated
        /health succeeds.
        """
        try:
            self._token()
        except AgentUnavailableError as e:
            return False, str(e)
        return True, ""

    def _build_request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: bytes | None = None,
        with_auth: bool = True,
        extra_headers: dict[str, str] | None = None,
    ) -> urllib_request.Request:
        """Build a urllib Request for ``path`` against the configured base URL."""
        url = f"{self.base_url}{path}"
        headers: dict[str, str] = {"Accept": "application/json"}
        if with_auth:
            headers["Authorization"] = f"Bearer {self._token()}"
        if extra_headers:
            headers.update(extra_headers)
        return urllib_request.Request(url, data=body, headers=headers, method=method)

    def health(self) -> dict[str, Any]:
        """GET /health — the readiness signal. No auth, 2s timeout.

        Returns the parsed JSON status payload on 200. Raises
        ``AgentTimeoutError`` on timeout, ``AgentUnavailableError`` on
        connection-refused, 5xx, or non-JSON body. Callers should treat
        any exception as "agent not ready, do not fire FreeRDP probes"
        (anti-goal #3).
        """
        req = self._build_request("/health", method="GET", with_auth=False)
        try:
            with urllib_request.urlopen(req, timeout=self.HEALTH_TIMEOUT) as resp:
                status = resp.status
                raw = resp.read()
        except urllib_error.HTTPError as e:
            # 4xx (other than auth) and 5xx come back here.
            if e.code in (401, 403):
                raise AgentAuthError(f"/health returned {e.code}") from e
            raise AgentUnavailableError(f"/health returned HTTP {e.code}") from e
        except TimeoutError as e:
            raise AgentTimeoutError(f"/health timed out after {self.HEALTH_TIMEOUT}s") from e
        except urllib_error.URLError as e:
            if isinstance(e.reason, socket.timeout):
                raise AgentTimeoutError(f"/health timed out after {self.HEALTH_TIMEOUT}s") from e
            raise AgentUnavailableError(f"/health unreachable: {e.reason}") from e
        except OSError as e:
            raise AgentUnavailableError(f"/health socket error: {e}") from e

        if status >= 500:
            raise AgentUnavailableError(f"/health returned HTTP {status}")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise AgentUnavailableError(f"/health returned non-JSON body: {e}") from e

    def exec(self, script: str, *, timeout: int = 60) -> ExecResult:
        """POST /exec — run ``script`` as PowerShell on the guest.

        ``script`` is the raw PowerShell source; this method base64-encodes
        it before sending. ``timeout`` is the guest's per-call execution
        budget in seconds; urllib waits for that server timeout plus a
        private five-second cleanup/response grace period before giving up.
        A client-side socket timeout becomes ``AgentTimeoutError``.

        Returns ``ExecResult`` even when the script's rc is non-zero —
        that's a script-level outcome, not a transport-level error.

        Raises:
            AgentAuthError: 401/403 from the agent (token mismatch).
            AgentTimeoutError: client-side socket timeout while waiting.
            AgentUnavailableError: connect refused / 5xx / network error.
            AgentError: 200 with a body the client can't parse.
        """
        encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
        body = json.dumps({"script": encoded, "timeout_sec": timeout}).encode("utf-8")
        req = self._build_request(
            "/exec",
            method="POST",
            body=body,
            with_auth=True,
            extra_headers={"Content-Type": "application/json"},
        )
        urlopen_timeout = float(timeout) + _EXEC_RESPONSE_GRACE
        try:
            with urllib_request.urlopen(req, timeout=urlopen_timeout) as resp:
                status = resp.status
                raw = resp.read()
        except urllib_error.HTTPError as e:
            if e.code in (401, 403):
                raise AgentAuthError(f"/exec returned {e.code}") from e
            raise AgentUnavailableError(f"/exec returned HTTP {e.code}") from e
        except TimeoutError as e:
            raise AgentTimeoutError(f"/exec timed out after {urlopen_timeout}s") from e
        except urllib_error.URLError as e:
            # urllib wraps socket.timeout as URLError(reason=socket.timeout) on
            # some Python versions — disambiguate before falling through.
            if isinstance(e.reason, socket.timeout):
                raise AgentTimeoutError(f"/exec timed out after {urlopen_timeout}s") from e
            raise AgentUnavailableError(f"/exec unreachable: {e.reason}") from e
        except OSError as e:
            raise AgentUnavailableError(f"/exec socket error: {e}") from e

        if status >= 500:
            raise AgentUnavailableError(f"/exec returned HTTP {status}")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise AgentError(f"/exec returned non-JSON body: {e}") from e

        # PowerShell's Start-Process -PassThru / WaitForExit can leave
        # $proc.ExitCode null even after a clean exit (kernalix7 hit this on
        # 2026-04-30). The agent has been patched to coerce that to 0, but
        # older agents already baked into existing pods still emit rc=null —
        # treat it as success rather than failing the whole apply, since
        # WaitForExit returning true means the process did terminate.
        rc_raw = payload.get("rc")
        if rc_raw is None:
            rc = 0
        else:
            try:
                rc = int(rc_raw)
            except (TypeError, ValueError) as e:
                raise AgentError(f"/exec response has non-integer rc: {rc_raw!r}") from e
        return ExecResult(
            rc=rc,
            stdout=str(payload.get("stdout", "")),
            stderr=str(payload.get("stderr", "")),
        )

    def window_frame(self, hwnd: int, *, popup_alpha: bool = False) -> bytes:
        """Return one lossless PNG frame rendered by the target guest HWND.

        The guest asks the window to render off-screen; this is an independent
        per-window surface, not a crop of the Windows desktop framebuffer.
        """
        if not isinstance(hwnd, int) or isinstance(hwnd, bool) or hwnd <= 0:
            raise ValueError("hwnd must be a positive integer")
        query_values = {"hwnd": hex(hwnd)}
        if popup_alpha:
            query_values["alpha"] = "border"
        query = urllib_parse.urlencode(query_values)
        req = self._build_request(
            f"/wayseam/frame?{query}", method="GET", with_auth=True,
            extra_headers={"Accept": "image/png"},
        )
        try:
            with urllib_request.urlopen(req, timeout=self.FRAME_TIMEOUT) as resp:
                status = resp.status
                raw = resp.read()
        except urllib_error.HTTPError as e:
            if e.code in (401, 403):
                raise AgentAuthError(f"/wayseam/frame returned {e.code}") from e
            if e.code == 410:
                raise AgentWindowGoneError("/wayseam/frame target window is closed") from e
            if 400 <= e.code < 500:
                raise AgentError(f"/wayseam/frame rejected HWND: HTTP {e.code}") from e
            raise AgentUnavailableError(f"/wayseam/frame returned HTTP {e.code}") from e
        except TimeoutError as e:
            raise AgentTimeoutError(
                f"/wayseam/frame timed out after {self.FRAME_TIMEOUT}s"
            ) from e
        except urllib_error.URLError as e:
            if isinstance(e.reason, socket.timeout):
                raise AgentTimeoutError(
                    f"/wayseam/frame timed out after {self.FRAME_TIMEOUT}s"
                ) from e
            raise AgentUnavailableError(f"/wayseam/frame unreachable: {e.reason}") from e
        except OSError as e:
            raise AgentUnavailableError(f"/wayseam/frame socket error: {e}") from e

        if status != 200 or not raw.startswith(b"\x89PNG\r\n\x1a\n"):
            raise AgentError("/wayseam/frame returned a non-PNG body")
        return raw

    def window_frame_bgra(
        self,
        hwnd: int,
        *,
        stream_id: str,
        base_sequence: int,
        previous_pixels: bytes | bytearray | None,
        popup_alpha: bool = False,
        in_place: bool = False,
    ) -> WayseamFrame:
        """Return one lossless BGRA rectangle update for the target HWND.

        ``in_place`` lets the decoder update a caller-owned ``bytearray``
        canvas instead of copying the whole surface per update.
        """
        if not isinstance(hwnd, int) or isinstance(hwnd, bool) or hwnd <= 0:
            raise ValueError("hwnd must be a positive integer")
        if not re.fullmatch(r"[0-9a-f]{32}", stream_id):
            raise ValueError("stream_id must be 32 lowercase hexadecimal characters")
        if not isinstance(base_sequence, int) or base_sequence < 0:
            raise ValueError("base_sequence must be a non-negative integer")
        query_values = {
            "hwnd": hex(hwnd),
            "encoding": "delta",
            "stream": stream_id,
            "base": str(base_sequence),
        }
        if popup_alpha:
            query_values["alpha"] = "border"
        query = urllib_parse.urlencode(query_values)
        req = self._build_request(
            f"/wayseam/frame?{query}", method="GET", with_auth=True,
            extra_headers={"Accept": "application/x-wayseam-bgra-delta"},
        )
        try:
            with urllib_request.urlopen(req, timeout=self.FRAME_TIMEOUT) as resp:
                status = resp.status
                raw = resp.read()
        except urllib_error.HTTPError as e:
            if e.code in (401, 403):
                raise AgentAuthError(f"/wayseam/frame returned {e.code}") from e
            if e.code == 410:
                raise AgentWindowGoneError("/wayseam/frame target window is closed") from e
            if 400 <= e.code < 500:
                raise AgentError(f"/wayseam/frame rejected HWND: HTTP {e.code}") from e
            raise AgentUnavailableError(f"/wayseam/frame returned HTTP {e.code}") from e
        except TimeoutError as e:
            raise AgentTimeoutError(
                f"/wayseam/frame timed out after {self.FRAME_TIMEOUT}s"
            ) from e
        except urllib_error.URLError as e:
            if isinstance(e.reason, socket.timeout):
                raise AgentTimeoutError(
                    f"/wayseam/frame timed out after {self.FRAME_TIMEOUT}s"
                ) from e
            raise AgentUnavailableError(f"/wayseam/frame unreachable: {e.reason}") from e
        except OSError as e:
            raise AgentUnavailableError(f"/wayseam/frame socket error: {e}") from e

        if status != 200:
            raise AgentError(f"/wayseam/frame returned HTTP {status}")
        return decode_wayseam_delta(raw, previous_pixels, in_place=in_place)

    def owned_windows(self, root_hwnd: int) -> tuple[dict[str, int], list[WayseamWindow]]:
        """Return geometry and visible owned HWNDs for one application root."""
        if root_hwnd <= 0:
            raise ValueError("root_hwnd must be positive")
        path = f"/wayseam/windows?{urllib_parse.urlencode({'root': hex(root_hwnd)})}"
        req = self._build_request(path, method="GET")
        try:
            with urllib_request.urlopen(req, timeout=self.FRAME_TIMEOUT) as resp:
                raw = resp.read()
        except urllib_error.HTTPError as e:
            if e.code in (401, 403):
                raise AgentAuthError(f"/wayseam/windows returned {e.code}") from e
            if 400 <= e.code < 500:
                raise AgentError(f"/wayseam/windows rejected root: HTTP {e.code}") from e
            raise AgentUnavailableError(f"/wayseam/windows returned HTTP {e.code}") from e
        except (TimeoutError, urllib_error.URLError, OSError) as e:
            raise AgentUnavailableError(f"/wayseam/windows unreachable: {e}") from e
        try:
            payload = json.loads(raw.decode("utf-8"))
            root = {
                key: int(payload[key])
                for key in ("left", "top", "width", "height")
            }
            windows = [
                WayseamWindow(
                    hwnd=int(item["hwnd"]),
                    owner=int(item["owner"]),
                    title=str(item.get("title", "")),
                    class_name=str(item.get("class_name", "")),
                    left=int(item["left"]),
                    top=int(item["top"]),
                    width=int(item["width"]),
                    height=int(item["height"]),
                )
                for item in payload.get("windows", [])
            ]
        except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AgentError(f"/wayseam/windows returned invalid data: {exc}") from exc
        return root, windows

    def top_level_windows(self) -> list[WayseamTopLevel]:
        """Return bounded visible guest roots with executable identity."""
        req = self._build_request("/wayseam/top-level", method="GET")
        try:
            with urllib_request.urlopen(req, timeout=self.FRAME_TIMEOUT) as resp:
                status = resp.status
                raw = resp.read()
        except urllib_error.HTTPError as e:
            if e.code in (401, 403):
                raise AgentAuthError(f"/wayseam/top-level returned {e.code}") from e
            if 400 <= e.code < 500:
                raise AgentError(
                    f"/wayseam/top-level rejected request: HTTP {e.code}"
                ) from e
            raise AgentUnavailableError(
                f"/wayseam/top-level returned HTTP {e.code}"
            ) from e
        except (TimeoutError, urllib_error.URLError, OSError) as e:
            raise AgentUnavailableError(f"/wayseam/top-level unreachable: {e}") from e
        if status != 200 or len(raw) > 4 * 1024 * 1024:
            raise AgentError("/wayseam/top-level returned an invalid response")
        try:
            payload = json.loads(raw.decode("utf-8"))
            items = payload.get("windows", [])
            if not isinstance(items, list) or len(items) > 256:
                raise ValueError("invalid window count")
            windows = [
                WayseamTopLevel(
                    hwnd=int(item["hwnd"]),
                    pid=int(item["pid"]),
                    process_path=str(item["process_path"]),
                    title=str(item.get("title", "")),
                    class_name=str(item.get("class_name", "")),
                    left=int(item["left"]),
                    top=int(item["top"]),
                    width=int(item["width"]),
                    height=int(item["height"]),
                    owner=int(item.get("owner", 0)),
                )
                for item in items
            ]
        except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AgentError(f"/wayseam/top-level returned invalid data: {exc}") from exc
        for window in windows:
            if (
                window.hwnd <= 0
                or window.owner < 0
                or window.pid <= 0
                or not window.process_path
                or len(window.process_path) > 32_768
                or len(window.title) > 512
                or len(window.class_name) > 256
                or not (64 <= window.width <= 32_768)
                or not (64 <= window.height <= 32_768)
                or abs(window.left) > 1_000_000
                or abs(window.top) > 1_000_000
            ):
                raise AgentError("/wayseam/top-level returned invalid window metadata")
        return windows

    def window_cursor(self, root_hwnd: int) -> WayseamCursor:
        """Return the live Windows cursor relative to a guest root HWND."""
        if not isinstance(root_hwnd, int) or isinstance(root_hwnd, bool) or root_hwnd <= 0:
            raise ValueError("root_hwnd must be a positive integer")
        path = f"/wayseam/cursor?{urllib_parse.urlencode({'root': hex(root_hwnd)})}"
        req = self._build_request(path, method="GET", with_auth=True)
        try:
            with urllib_request.urlopen(req, timeout=self.FRAME_TIMEOUT) as resp:
                status = resp.status
                raw = resp.read()
        except urllib_error.HTTPError as e:
            if e.code in (401, 403):
                raise AgentAuthError(f"/wayseam/cursor returned {e.code}") from e
            if 400 <= e.code < 500:
                raise AgentError(f"/wayseam/cursor rejected root: HTTP {e.code}") from e
            raise AgentUnavailableError(f"/wayseam/cursor returned HTTP {e.code}") from e
        except (TimeoutError, urllib_error.URLError, OSError) as e:
            raise AgentUnavailableError(f"/wayseam/cursor unreachable: {e}") from e
        if status != 200:
            raise AgentError(f"/wayseam/cursor returned HTTP {status}")
        try:
            payload = json.loads(raw.decode("utf-8"))
            visible = payload["visible"] is True
            png = base64.b64decode(payload.get("png", ""), validate=True)
            cursor = WayseamCursor(
                visible=visible,
                shape=int(str(payload.get("shape", "0")), 0),
                x=int(payload.get("x", 0)),
                y=int(payload.get("y", 0)),
                hot_x=int(payload.get("hot_x", 0)),
                hot_y=int(payload.get("hot_y", 0)),
                width=int(payload.get("width", 0)),
                height=int(payload.get("height", 0)),
                png=png,
            )
        except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AgentError(f"/wayseam/cursor returned invalid data: {exc}") from exc
        if visible:
            if not (1 <= cursor.width <= 256 and 1 <= cursor.height <= 256):
                raise AgentError("/wayseam/cursor returned invalid dimensions")
            if len(cursor.png) > 1_048_576 or not cursor.png.startswith(b"\x89PNG\r\n\x1a\n"):
                raise AgentError("/wayseam/cursor returned invalid PNG data")
            if not (0 <= cursor.hot_x < cursor.width and 0 <= cursor.hot_y < cursor.height):
                raise AgentError("/wayseam/cursor returned invalid hotspot")
        return cursor

    WHEEL_DELTA_LIMIT = 12000  # 100 notches of WHEEL_DELTA (120) per request

    def window_pointer_input(
        self,
        hwnd: int,
        *,
        x: int,
        y: int,
        action: str,
        button: int = 0,
        delta_x: int = 0,
        delta_y: int = 0,
    ) -> None:
        """Route one bounded pointer event to a guest HWND.

        ``wheel`` events carry raw Windows ``WHEEL_DELTA`` units (+120 is one
        notch up / right) and are delivered to the HWND under the pointer.
        """
        if not isinstance(hwnd, int) or isinstance(hwnd, bool) or hwnd <= 0:
            raise ValueError("hwnd must be a positive integer")
        if not isinstance(x, int) or not isinstance(y, int) or x < 0 or y < 0:
            raise ValueError("pointer coordinates must be non-negative integers")
        if action not in {"move", "down", "up", "wheel"}:
            raise ValueError("unsupported pointer action")
        if action in {"down", "up"} and button not in {1, 2, 3}:
            raise ValueError("button must be 1, 2, or 3 for click events")
        event: dict[str, object] = {
            "hwnd": hex(hwnd), "x": x, "y": y, "action": action, "button": button,
        }
        if action == "wheel":
            for delta in (delta_x, delta_y):
                if not isinstance(delta, int) or isinstance(delta, bool):
                    raise ValueError("wheel deltas must be integers")
                if abs(delta) > self.WHEEL_DELTA_LIMIT:
                    raise ValueError("wheel deltas exceed the supported range")
            if delta_x == 0 and delta_y == 0:
                raise ValueError("wheel events require a non-zero delta")
            event["delta_x"] = delta_x
            event["delta_y"] = delta_y
        payload = json.dumps(event).encode("utf-8")
        req = self._build_request(
            "/wayseam/input", method="POST", body=payload, with_auth=True,
            extra_headers={"Content-Type": "application/json"},
        )
        try:
            with urllib_request.urlopen(req, timeout=self.FRAME_TIMEOUT) as resp:
                status = resp.status
                raw = resp.read()
        except urllib_error.HTTPError as e:
            if e.code in (401, 403):
                raise AgentAuthError(f"/wayseam/input returned {e.code}") from e
            if 400 <= e.code < 500:
                raise AgentError(f"/wayseam/input rejected event: HTTP {e.code}") from e
            raise AgentUnavailableError(f"/wayseam/input returned HTTP {e.code}") from e
        except TimeoutError as e:
            raise AgentTimeoutError(
                f"/wayseam/input timed out after {self.FRAME_TIMEOUT}s"
            ) from e
        except urllib_error.URLError as e:
            raise AgentUnavailableError(f"/wayseam/input unreachable: {e.reason}") from e
        except OSError as e:
            raise AgentUnavailableError(f"/wayseam/input socket error: {e}") from e
        if status != 200:
            raise AgentError(f"/wayseam/input returned HTTP {status}")
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise AgentError("/wayseam/input returned a non-JSON body") from e
        if result.get("ok") is not True:
            raise AgentError("/wayseam/input did not acknowledge the event")

    def window_keyboard_input(
        self,
        hwnd: int,
        *,
        action: str,
        virtual_key: int = 0,
        unicode: int = 0,
        extended: bool = False,
    ) -> None:
        """Route one bounded keyboard event to a guest HWND family."""
        if not isinstance(hwnd, int) or isinstance(hwnd, bool) or hwnd <= 0:
            raise ValueError("hwnd must be a positive integer")
        if action not in {"down", "up", "press"}:
            raise ValueError("unsupported keyboard action")
        if not isinstance(virtual_key, int) or isinstance(virtual_key, bool):
            raise ValueError("virtual_key must be an integer")
        if not isinstance(unicode, int) or isinstance(unicode, bool):
            raise ValueError("unicode must be an integer")
        if not isinstance(extended, bool):
            raise ValueError("extended must be a boolean")
        if action in {"down", "up"}:
            if not (1 <= virtual_key <= 0xFF) or unicode != 0:
                raise ValueError("virtual-key events require one bounded virtual_key")
        elif (
            virtual_key != 0
            or not (1 <= unicode <= 0x10FFFF)
            or 0xD800 <= unicode <= 0xDFFF
        ):
            raise ValueError("unicode press requires one Unicode scalar")
        payload = json.dumps({
            "hwnd": hex(hwnd),
            "action": action,
            "virtual_key": virtual_key,
            "unicode": unicode,
            "extended": extended,
        }).encode("utf-8")
        req = self._build_request(
            "/wayseam/keyboard",
            method="POST",
            body=payload,
            with_auth=True,
            extra_headers={"Content-Type": "application/json"},
        )
        try:
            with urllib_request.urlopen(req, timeout=self.FRAME_TIMEOUT) as resp:
                status = resp.status
                raw = resp.read()
        except urllib_error.HTTPError as e:
            if e.code in (401, 403):
                raise AgentAuthError(f"/wayseam/keyboard returned {e.code}") from e
            if 400 <= e.code < 500:
                raise AgentError(f"/wayseam/keyboard rejected event: HTTP {e.code}") from e
            raise AgentUnavailableError(f"/wayseam/keyboard returned HTTP {e.code}") from e
        except TimeoutError as e:
            raise AgentTimeoutError(
                f"/wayseam/keyboard timed out after {self.FRAME_TIMEOUT}s"
            ) from e
        except urllib_error.URLError as e:
            raise AgentUnavailableError(f"/wayseam/keyboard unreachable: {e.reason}") from e
        except OSError as e:
            raise AgentUnavailableError(f"/wayseam/keyboard socket error: {e}") from e
        if status != 200:
            raise AgentError(f"/wayseam/keyboard returned HTTP {status}")
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise AgentError("/wayseam/keyboard returned a non-JSON body") from e
        if result.get("ok") is not True:
            raise AgentError("/wayseam/keyboard did not acknowledge the event")

    def window_close(self, hwnd: int) -> None:
        """Request a normal Windows close for one presented root HWND."""
        if not isinstance(hwnd, int) or isinstance(hwnd, bool) or hwnd <= 0:
            raise ValueError("hwnd must be a positive integer")
        payload = json.dumps({"hwnd": hex(hwnd)}).encode("utf-8")
        req = self._build_request(
            "/wayseam/close", method="POST", body=payload, with_auth=True,
            extra_headers={"Content-Type": "application/json"},
        )
        try:
            with urllib_request.urlopen(req, timeout=self.FRAME_TIMEOUT) as resp:
                status = resp.status
                raw = resp.read()
        except urllib_error.HTTPError as e:
            if e.code in (401, 403):
                raise AgentAuthError(f"/wayseam/close returned {e.code}") from e
            if 400 <= e.code < 500:
                raise AgentError(f"/wayseam/close rejected HWND: HTTP {e.code}") from e
            raise AgentUnavailableError(f"/wayseam/close returned HTTP {e.code}") from e
        except TimeoutError as e:
            raise AgentTimeoutError(
                f"/wayseam/close timed out after {self.FRAME_TIMEOUT}s"
            ) from e
        except urllib_error.URLError as e:
            raise AgentUnavailableError(f"/wayseam/close unreachable: {e.reason}") from e
        except OSError as e:
            raise AgentUnavailableError(f"/wayseam/close socket error: {e}") from e
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise AgentError("/wayseam/close returned a non-JSON body") from e
        if status != 200 or result.get("ok") is not True:
            raise AgentError("/wayseam/close did not acknowledge the request")

    def window_shell_visibility(self, *, visible: bool) -> int:
        """Show or hide Windows shell bars for the active presentation mode."""
        if not isinstance(visible, bool):
            raise ValueError("visible must be a boolean")
        payload = json.dumps({"visible": visible}).encode("utf-8")
        req = self._build_request(
            "/wayseam/shell",
            method="POST",
            body=payload,
            with_auth=True,
            extra_headers={"Content-Type": "application/json"},
        )
        try:
            with urllib_request.urlopen(req, timeout=self.FRAME_TIMEOUT) as resp:
                status = resp.status
                raw = resp.read()
        except urllib_error.HTTPError as e:
            if e.code in (401, 403):
                raise AgentAuthError(f"/wayseam/shell returned {e.code}") from e
            if 400 <= e.code < 500:
                raise AgentError(f"/wayseam/shell rejected mode: HTTP {e.code}") from e
            raise AgentUnavailableError(f"/wayseam/shell returned HTTP {e.code}") from e
        except TimeoutError as e:
            raise AgentTimeoutError(
                f"/wayseam/shell timed out after {self.FRAME_TIMEOUT}s"
            ) from e
        except urllib_error.URLError as e:
            raise AgentUnavailableError(f"/wayseam/shell unreachable: {e.reason}") from e
        except OSError as e:
            raise AgentUnavailableError(f"/wayseam/shell socket error: {e}") from e
        if status != 200:
            raise AgentError(f"/wayseam/shell returned HTTP {status}")
        try:
            result = json.loads(raw.decode("utf-8"))
            windows = int(result["windows"])
        except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as e:
            raise AgentError("/wayseam/shell returned an invalid response") from e
        if result.get("ok") is not True or not (0 <= windows <= 16):
            raise AgentError("/wayseam/shell did not acknowledge the mode")
        return windows

    def window_resize(
        self,
        hwnd: int,
        *,
        width: int,
        height: int,
        origin: tuple[int, int] | None = None,
    ) -> tuple[int, int]:
        """Resize a guest HWND to match its compositor-managed host surface.

        ``origin`` mirrors the host tile position onto the guest desktop so
        concurrently presented windows never overlap (Windows routes wheel
        input by z-order at the cursor, so overlap cross-scrolls tiles).
        """
        if not isinstance(hwnd, int) or isinstance(hwnd, bool) or hwnd <= 0:
            raise ValueError("hwnd must be a positive integer")
        if not (160 <= width <= 8192 and 120 <= height <= 8192):
            raise ValueError("window dimensions are outside the supported range")
        if width * height > 33_554_432:
            raise ValueError("window pixel count exceeds the supported range")
        body: dict[str, object] = {
            "hwnd": hex(hwnd),
            "width": width,
            "height": height,
        }
        if origin is not None:
            ox, oy = int(origin[0]), int(origin[1])
            if not (0 <= ox <= 16384 and 0 <= oy <= 16384):
                raise ValueError("window origin is outside the supported range")
            body["x"] = ox
            body["y"] = oy
        payload = json.dumps(body).encode("utf-8")
        req = self._build_request(
            "/wayseam/resize",
            method="POST",
            body=payload,
            with_auth=True,
            extra_headers={"Content-Type": "application/json"},
        )
        try:
            with urllib_request.urlopen(req, timeout=self.FRAME_TIMEOUT) as resp:
                status = resp.status
                raw = resp.read()
        except urllib_error.HTTPError as e:
            if e.code in (401, 403):
                raise AgentAuthError(f"/wayseam/resize returned {e.code}") from e
            if 400 <= e.code < 500:
                raise AgentError(f"/wayseam/resize rejected dimensions: HTTP {e.code}") from e
            raise AgentUnavailableError(f"/wayseam/resize returned HTTP {e.code}") from e
        except TimeoutError as e:
            raise AgentTimeoutError(
                f"/wayseam/resize timed out after {self.FRAME_TIMEOUT}s"
            ) from e
        except urllib_error.URLError as e:
            raise AgentUnavailableError(f"/wayseam/resize unreachable: {e.reason}") from e
        except OSError as e:
            raise AgentUnavailableError(f"/wayseam/resize socket error: {e}") from e
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise AgentError("/wayseam/resize returned a non-JSON body") from e
        if status != 200 or result.get("ok") is not True:
            raise AgentError("/wayseam/resize did not acknowledge the request")
        try:
            actual = int(result["width"]), int(result["height"])
        except (KeyError, TypeError, ValueError) as e:
            raise AgentError("/wayseam/resize returned invalid dimensions") from e
        if not (1 <= actual[0] <= 8192 and 1 <= actual[1] <= 8192):
            raise AgentError("/wayseam/resize returned invalid dimensions")
        return actual

    def _wayseam_json(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        query: dict[str, str] | None = None,
        max_bytes: int = 8 * 1024 * 1024,
    ) -> dict[str, Any]:
        """POST/GET one authenticated JSON route and require ``ok: true``."""
        target = f"{path}?{urllib_parse.urlencode(query)}" if query else path
        body: bytes | None = None
        headers: dict[str, str] = {}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = self._build_request(
            target, method=method, body=body, with_auth=True, extra_headers=headers,
        )
        try:
            with urllib_request.urlopen(req, timeout=self.FRAME_TIMEOUT) as resp:
                status = resp.status
                raw = resp.read(max_bytes + 1)
        except urllib_error.HTTPError as e:
            if e.code in (401, 403):
                raise AgentAuthError(f"{path} returned {e.code}") from e
            if e.code == 423:
                raise AgentBusyError(f"{path} is busy") from e
            if 400 <= e.code < 500:
                raise AgentError(f"{path} rejected request: HTTP {e.code}") from e
            raise AgentUnavailableError(f"{path} returned HTTP {e.code}") from e
        except TimeoutError as e:
            raise AgentTimeoutError(
                f"{path} timed out after {self.FRAME_TIMEOUT}s"
            ) from e
        except urllib_error.URLError as e:
            raise AgentUnavailableError(f"{path} unreachable: {e.reason}") from e
        except OSError as e:
            raise AgentUnavailableError(f"{path} socket error: {e}") from e
        if status != 200 or len(raw) > max_bytes:
            raise AgentError(f"{path} returned an invalid response")
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise AgentError(f"{path} returned a non-JSON body") from e
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise AgentError(f"{path} did not acknowledge the request")
        return result

    CLIPBOARD_MAX_CHARS = 1_048_576

    def clipboard_get(self, *, since: int | None = None) -> WayseamClipboard:
        """Read the guest text clipboard unless it is unchanged since ``since``."""
        query: dict[str, str] = {}
        if since is not None:
            if not isinstance(since, int) or isinstance(since, bool) or since < 0:
                raise ValueError("since must be a non-negative clipboard sequence")
            query["since"] = str(since)
        result = self._wayseam_json("/wayseam/clipboard", query=query or None)
        try:
            sequence = int(result["sequence"])
            changed = result.get("changed") is True
            text = str(result.get("text", "")) if changed else ""
        except (KeyError, TypeError, ValueError) as e:
            raise AgentError("/wayseam/clipboard returned invalid data") from e
        if sequence < 0 or len(text) > self.CLIPBOARD_MAX_CHARS:
            raise AgentError("/wayseam/clipboard returned invalid data")
        return WayseamClipboard(
            sequence=sequence,
            changed=changed,
            text=text,
            truncated=result.get("truncated") is True,
        )

    def clipboard_set(self, text: str) -> int:
        """Replace the guest text clipboard and return its new sequence."""
        if not isinstance(text, str):
            raise ValueError("clipboard text must be a string")
        if len(text) > self.CLIPBOARD_MAX_CHARS:
            raise ValueError("clipboard text exceeds the supported size")
        try:
            text.encode("utf-8")
        except UnicodeEncodeError as e:
            raise ValueError("clipboard text must be valid Unicode") from e
        result = self._wayseam_json(
            "/wayseam/clipboard", method="POST", payload={"text": text},
        )
        try:
            sequence = int(result["sequence"])
        except (KeyError, TypeError, ValueError) as e:
            raise AgentError("/wayseam/clipboard returned invalid data") from e
        return sequence

    def shm_assign(self, hwnd: int) -> WayseamShmSlot:
        """Start push-mode frame streaming for ``hwnd`` into an IVSHMEM slot.

        The guest's WGC worker then publishes WSD1 frame blobs into the ring
        (ADR 0004) with no HTTP on the hot path; the reply carries the slot
        geometry the host reader needs.
        """
        if not isinstance(hwnd, int) or isinstance(hwnd, bool) or hwnd <= 0:
            raise ValueError("hwnd must be a positive integer")
        result = self._wayseam_json(
            "/wayseam/shm", method="POST",
            payload={"action": "assign", "hwnd": hex(hwnd)},
        )
        try:
            slot = WayseamShmSlot(
                slot=int(result["slot"]),
                slot_count=int(result["slot_count"]),
                slot_size=int(result["slot_size"]),
                slot0_offset=int(result["slot0_offset"]),
            )
        except (KeyError, TypeError, ValueError) as e:
            raise AgentError("/wayseam/shm returned invalid slot data") from e
        if not (
            0 <= slot.slot < slot.slot_count <= 16
            and 1 <= slot.slot_size <= 256 * 1024 * 1024
            and slot.slot0_offset >= 0
        ):
            raise AgentError("/wayseam/shm returned invalid slot geometry")
        return slot

    def shm_release(self, hwnd: int) -> None:
        """Stop the guest-side ring stream for ``hwnd`` (idempotent)."""
        if not isinstance(hwnd, int) or isinstance(hwnd, bool) or hwnd <= 0:
            raise ValueError("hwnd must be a positive integer")
        self._wayseam_json(
            "/wayseam/shm", method="POST",
            payload={"action": "release", "hwnd": hex(hwnd)},
        )

    def display_status(self) -> dict[str, Any]:
        """GET /wayseam/display — Parsec keepalive, displays, DPI (agent >= 0.2.36)."""
        req = self._build_request("/wayseam/display", with_auth=True)
        try:
            with urllib_request.urlopen(req, timeout=self.HEALTH_TIMEOUT) as resp:
                raw = resp.read()
        except urllib_error.HTTPError as e:
            if e.code in (401, 403):
                raise AgentAuthError(f"/wayseam/display returned {e.code}") from e
            raise AgentUnavailableError(f"/wayseam/display returned HTTP {e.code}") from e
        except (TimeoutError, urllib_error.URLError, OSError) as e:
            raise AgentUnavailableError(f"/wayseam/display unreachable: {e}") from e
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise AgentError("/wayseam/display returned a non-JSON body") from e

    def shm_status(self) -> str:
        """Return the guest's one-line ring/stream status (diagnostics)."""
        result = self._wayseam_json("/wayseam/shm")
        return str(result.get("status", ""))[:2048]

    _SESSION_STATES = frozenset({"active", "connected", "disconnected", "unknown"})

    def _session_state_from(self, result: dict[str, Any]) -> WayseamSessionState:
        try:
            session_id = int(result["session_id"])
            state = str(result.get("state", "unknown"))
            station = str(result.get("station", ""))
        except (KeyError, TypeError, ValueError) as e:
            raise AgentError("/wayseam/session returned invalid data") from e
        if session_id < 0 or len(station) > 256:
            raise AgentError("/wayseam/session returned invalid data")
        if state not in self._SESSION_STATES:
            state = "unknown"
        return WayseamSessionState(
            session_id=session_id,
            state=state,
            station=station,
            console=result.get("console") is True,
            reconnected=result.get("reconnected") is True,
        )

    def session_state(self) -> WayseamSessionState:
        """Report whether the guest's interactive session is on the console."""
        return self._session_state_from(self._wayseam_json("/wayseam/session"))

    def session_reconnect_console(self) -> WayseamSessionState:
        """Reattach the interactive session to the console after Desktop Mode.

        Closing the desktop RDP client leaves the Windows session
        disconnected, where DWM stops composing and Wayseam capture/input
        fail. The guest runs ``tscon <id> /dest:console`` and waits for the
        session to report ``active`` again.
        """
        return self._session_state_from(
            self._wayseam_json(
                "/wayseam/session", method="POST", payload={"action": "console"},
            )
        )
