# SPDX-License-Identifier: MIT
"""Present one guest HWND as an independent native Wayland window.

This first transport adapter consumes lossless HWND frames from the existing
authenticated guest agent. It deliberately never reads or crops the Windows
desktop framebuffer. The public seam is a latest-frame-only pump so a faster
producer can replace the PNG adapter without changing window lifecycle code.
"""

from __future__ import annotations

import argparse
import collections
import fcntl
import hashlib
import os
import signal
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from wayseam.config import Config
from wayseam.guest.agent import (
    AgentClient,
    AgentError,
    AgentUnavailableError,
    AgentWindowGoneError,
    WayseamCursor,
    WayseamFrame,
    WayseamWindow,
)
from wayseam.paths import runtime_dir
from wayseam.present.shm import (
    ShmFrameSource,
    ShmInputWriter,
    ShmNeedsBase,
    ShmTooLarge,
    default_ring_path,
)

Patch = tuple[tuple[int, int, int, int], bytes]


@dataclass(frozen=True)
class Frame:
    surface: WayseamFrame
    sequence: int
    captured_at: float
    # Damage accumulated since the previous ``take_latest``: every changed
    # rectangle with its pixels, so the presenter uploads only those. ``None``
    # means "present the whole surface" (first frame, size change, or too much
    # damage to be worth patching).
    damage: tuple[Patch, ...] | None = None


class LatestFramePump:
    """Fetch frames without ever allowing a latency-producing backlog."""

    # Beyond these bounds a full re-upload is cheaper than many patches.
    MAX_PATCHES = 48
    MAX_PATCH_FRACTION = 0.5

    # While streaming over shared memory, poll the seqlock at this cadence:
    # far cheaper than a frame fetch (a read of 8 bytes) and well under a
    # 60 fps frame period, so arrival-to-present delay stays ~1-2 ms.
    SHM_POLL_SECONDS = 0.002
    SHM_KEEPALIVE_SECONDS = 2.0

    def __init__(
        self,
        client: AgentClient,
        hwnd: int,
        *,
        max_fps: int = 30,
        popup_alpha: bool = False,
        shm: ShmFrameSource | None = None,
    ) -> None:
        self.client = client
        self.hwnd = hwnd
        self.max_fps = max(1, min(max_fps, 120))
        self.popup_alpha = popup_alpha
        self.shm = shm
        self.transport = "shm" if shm is not None else "http"
        self._lock = threading.Lock()
        self._latest: Frame | None = None
        self._pending_damage: list[Patch] | None = None
        self._pending_bytes = 0
        self._last_digest: bytes | None = None
        self._stream_id = uuid.uuid4().hex
        self._server_sequence = 0
        self._pixels: bytes | bytearray | None = None
        self._sequence = 0
        self._stop = threading.Event()
        self.window_gone = threading.Event()
        self._thread: threading.Thread | None = None
        self._frame_callback: Callable[[], None] | None = None
        self.error: str | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="wayseam-frame-pump", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        if self.shm is not None:
            try:
                self.shm.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                self.client.shm_release(self.hwnd)
            except Exception:  # noqa: BLE001 - the guest reaps stale slots anyway
                pass

    def set_frame_callback(self, callback: Callable[[], None] | None) -> None:
        """Wake the presentation loop as soon as a changed frame arrives."""
        self._frame_callback = callback

    def take_latest(self) -> Frame | None:
        with self._lock:
            frame = self._latest
            if frame is not None:
                damage = (
                    None if self._pending_damage is None
                    else tuple(self._pending_damage)
                )
                frame = Frame(frame.surface, frame.sequence, frame.captured_at, damage)
            self._latest = None
            self._pending_damage = []
            self._pending_bytes = 0
            return frame

    def _record_damage(self, surface: WayseamFrame) -> None:
        """Accumulate this update's damage for the next presentation."""
        if surface.damage is None or self._pending_damage is None:
            self._pending_damage = None
            return
        self._pending_damage.append((surface.damage, surface.damage_pixels))
        self._pending_bytes += len(surface.damage_pixels)
        if (
            len(self._pending_damage) > self.MAX_PATCHES
            or self._pending_bytes > len(surface.pixels) * self.MAX_PATCH_FRACTION
        ):
            self._pending_damage = None

    def _publish(self, surface: WayseamFrame) -> None:
        """Common publish path for both transports."""
        self._server_sequence = surface.server_sequence
        self._pixels = surface.pixels
        digest = surface.wire_digest
        if surface.changed and digest != self._last_digest:
            self._last_digest = digest
            self._sequence += 1
            frame = Frame(
                surface=surface,
                sequence=self._sequence,
                captured_at=time.monotonic(),
            )
            with self._lock:
                self._record_damage(surface)
                self._latest = frame
            callback = self._frame_callback
            if callback is not None:
                try:
                    callback()
                except Exception:
                    pass
        self.error = None

    def _http_full_frame(self) -> None:
        """Fetch one full frame over HTTP (shm too-large or recovery)."""
        surface = self.client.window_frame_bgra(
            self.hwnd,
            stream_id=uuid.uuid4().hex,
            base_sequence=0,
            previous_pixels=None,
            popup_alpha=self.popup_alpha,
        )
        self._publish(surface)

    def _run_shm(self) -> None:
        """Consume the guest's push-mode ring; HTTP never rides the hot path.

        The guest publishes only on change, so ``None`` polls are the idle
        norm. A periodic idempotent re-assign heals guest agent restarts; if
        the ring stops working entirely, drop to the HTTP loop so the window
        keeps living (slower beats frozen).
        """
        last_keepalive = time.monotonic()
        failures = 0
        while not self._stop.is_set():
            try:
                result = self.shm.poll(self._pixels)
            except Exception as exc:  # noqa: BLE001 - reader must never kill the pump
                self.error = f"{type(exc).__name__}: {exc}"
                result = None
                failures += 1
                if failures > 20:
                    self.transport = "http"
                    return
            if isinstance(result, (ShmTooLarge, ShmNeedsBase)):
                # Too large for the slot, or a delta we have no base for
                # (attached after the first full frame): one HTTP full frame
                # gives the canvas every later delta applies to.
                try:
                    self._http_full_frame()
                    self.error = None
                except AgentWindowGoneError:
                    self.window_gone.set()
                    return
                except Exception as exc:  # noqa: BLE001
                    self.error = f"{type(exc).__name__}: {exc}"
            elif result is not None:
                failures = 0
                self.error = None
                self._publish(result)
                continue  # drain bursts without sleeping
            now = time.monotonic()
            if now - last_keepalive >= self.SHM_KEEPALIVE_SECONDS:
                last_keepalive = now
                try:
                    self.client.shm_assign(self.hwnd)
                    if self.error and "shm" in self.error:
                        self.error = None  # the agent is back; stop showing the hiccup
                except AgentWindowGoneError:
                    self.window_gone.set()
                    return
                except AgentError as exc:
                    # In shm mode nothing else touches the HTTP frame route,
                    # so this keepalive is the only place a dead HWND shows
                    # up: the agent rejects assigns for windows that no
                    # longer exist. Retire the surface instead of error-
                    # looping with a stale last frame on screen.
                    if "rejected" in str(exc):
                        self.window_gone.set()
                        return
                    self.error = f"{type(exc).__name__}: {exc}"
                except Exception as exc:  # noqa: BLE001 - agent hiccups are survivable
                    self.error = f"{type(exc).__name__}: {exc}"
            self._stop.wait(self.SHM_POLL_SECONDS)

    SHM_UPGRADE_SECONDS = 5.0

    def set_shm_factory(self, factory) -> None:
        """Provide a callable that builds a fresh ShmFrameSource (or None)."""
        self._shm_factory = factory

    def _try_shm_upgrade(self) -> bool:
        factory = getattr(self, "_shm_factory", None)
        if factory is None:
            return False
        try:
            source = factory()
        except Exception:  # noqa: BLE001 - the ring may still be unavailable
            return False
        if source is None:
            return False
        self.shm = source
        self.transport = "shm"
        return True

    def _run(self) -> None:
        while True:
            if self.shm is not None:
                self._run_shm()
                if self._stop.is_set() or self.window_gone.is_set():
                    return
                # Ring became unusable: continue on HTTP with a fresh stream.
                self.shm = None
            self._run_http()
            if self._stop.is_set() or self.window_gone.is_set():
                return
            # _run_http only returns without stop when an upgrade succeeded.

    def _run_http(self) -> None:
        last_upgrade = time.monotonic()
        frame_period = 1.0 / self.max_fps
        while not self._stop.is_set():
            if time.monotonic() - last_upgrade >= self.SHM_UPGRADE_SECONDS:
                last_upgrade = time.monotonic()
                if self._try_shm_upgrade():
                    # A deploy or a startup race left this window on HTTP;
                    # the ring is back — switch over without a restart.
                    return
            started = time.monotonic()
            try:
                surface = self.client.window_frame_bgra(
                    self.hwnd,
                    stream_id=self._stream_id,
                    base_sequence=self._server_sequence,
                    previous_pixels=self._pixels,
                    popup_alpha=self.popup_alpha,
                    in_place=True,
                )
                self._server_sequence = surface.server_sequence
                self._pixels = surface.pixels
                digest = surface.wire_digest
                if surface.changed and digest != self._last_digest:
                    self._last_digest = digest
                    self._sequence += 1
                    frame = Frame(
                        surface=surface,
                        sequence=self._sequence,
                        captured_at=time.monotonic(),
                    )
                    with self._lock:
                        self._record_damage(surface)
                        self._latest = frame
                    callback = self._frame_callback
                    if callback is not None:
                        try:
                            callback()
                        except Exception:
                            pass
                self.error = None
            except AgentWindowGoneError:
                self.window_gone.set()
                break
            except Exception as exc:  # noqa: BLE001 - surfaced in the window title
                self.error = f"{type(exc).__name__}: {exc}"
                if self._stop.wait(0.25):
                    break
            remaining = frame_period - (time.monotonic() - started)
            if remaining > 0:
                self._stop.wait(remaining)


@dataclass(frozen=True)
class PointerEvent:
    x: int
    y: int
    action: str
    button: int = 0
    delta_x: int = 0
    delta_y: int = 0


WHEEL_DELTA = 120


class WheelAccumulator:
    """Turn GDK scroll deltas into whole Windows WHEEL_DELTA units.

    GDK reports one notch as ``±1`` for wheels and fractional values for
    touchpads; GDK's y axis grows downward while Windows' wheel grows upward.
    Fractional remainders are carried so slow touchpad scrolls still arrive.
    """

    def __init__(self, *, notch_pixels: float = 40.0) -> None:
        self.notch_pixels = max(0.01, notch_pixels)
        self._carry_x = 0.0
        self._carry_y = 0.0

    def consume(self, dx: float, dy: float, *, discrete: bool) -> tuple[int, int]:
        scale = WHEEL_DELTA if discrete else WHEEL_DELTA / self.notch_pixels
        self._carry_x += dx * scale
        self._carry_y += -dy * scale
        out_x = int(self._carry_x)
        out_y = int(self._carry_y)
        self._carry_x -= out_x
        self._carry_y -= out_y
        limit = 100 * WHEEL_DELTA
        return max(-limit, min(limit, out_x)), max(-limit, min(limit, out_y))

    def reset(self) -> None:
        self._carry_x = 0.0
        self._carry_y = 0.0


@dataclass(frozen=True)
class KeyboardEvent:
    action: str
    virtual_key: int = 0
    unicode: int = 0
    extended: bool = False


_SUPER_KEYVALS = {
    0xFFE7,  # Meta_L
    0xFFE8,  # Meta_R
    0xFFEB,  # Super_L
    0xFFEC,  # Super_R
    0xFFED,  # Hyper_L
    0xFFEE,  # Hyper_R
}

_SPECIAL_KEYVALS: dict[int, tuple[int, bool]] = {
    0xFF08: (0x08, False),  # BackSpace
    0xFF09: (0x09, False),  # Tab
    0xFE20: (0x09, False),  # ISO_Left_Tab
    0xFF0D: (0x0D, False),  # Return
    0xFF1B: (0x1B, False),  # Escape
    0xFF13: (0x13, False),  # Pause
    0xFF14: (0x91, False),  # Scroll_Lock
    0xFF50: (0x24, True),   # Home
    0xFF51: (0x25, True),   # Left
    0xFF52: (0x26, True),   # Up
    0xFF53: (0x27, True),   # Right
    0xFF54: (0x28, True),   # Down
    0xFF55: (0x21, True),   # Page_Up
    0xFF56: (0x22, True),   # Page_Down
    0xFF57: (0x23, True),   # End
    0xFF61: (0x2C, True),   # Print
    0xFF63: (0x2D, True),   # Insert
    0xFF67: (0x5D, True),   # Menu
    0xFF7F: (0x90, True),   # Num_Lock
    0xFFE1: (0x10, False),  # Shift_L
    0xFFE2: (0x10, False),  # Shift_R
    0xFFE3: (0x11, False),  # Control_L
    0xFFE4: (0x11, True),   # Control_R
    0xFFE5: (0x14, False),  # Caps_Lock
    0xFFE9: (0x12, False),  # Alt_L
    0xFFEA: (0x12, True),   # Alt_R
    0xFE03: (0x12, True),   # ISO_Level3_Shift / AltGr
    0xFFFF: (0x2E, True),   # Delete
    0xFF8D: (0x0D, True),   # KP_Enter
    0xFFAA: (0x6A, False),  # KP_Multiply
    0xFFAB: (0x6B, False),  # KP_Add
    0xFFAC: (0x6C, False),  # KP_Separator
    0xFFAD: (0x6D, False),  # KP_Subtract
    0xFFAE: (0x6E, False),  # KP_Decimal
    0xFFAF: (0x6F, True),   # KP_Divide
}

_PUNCTUATION_VKS = {
    ";": 0xBA, ":": 0xBA,
    "=": 0xBB, "+": 0xBB,
    ",": 0xBC, "<": 0xBC,
    "-": 0xBD, "_": 0xBD,
    ".": 0xBE, ">": 0xBE,
    "/": 0xBF, "?": 0xBF,
    "`": 0xC0, "~": 0xC0,
    "[": 0xDB, "{": 0xDB,
    "\\": 0xDC, "|": 0xDC,
    "]": 0xDD, "}": 0xDD,
    "'": 0xDE, '"': 0xDE,
}
_SHIFTED_DIGITS = {
    "!": 0x31, "@": 0x32, "#": 0x33, "$": 0x34, "%": 0x35,
    "^": 0x36, "&": 0x37, "*": 0x38, "(": 0x39, ")": 0x30,
}


def translate_keyboard_event(
    keyval: int,
    codepoint: int,
    *,
    pressed: bool,
) -> KeyboardEvent | None:
    """Translate one GDK keysym into a bounded Windows input event."""
    if keyval in _SUPER_KEYVALS:
        return None
    mapped = _SPECIAL_KEYVALS.get(keyval)
    if mapped is None and 0xFFBE <= keyval <= 0xFFD5:  # F1..F24
        mapped = (0x70 + keyval - 0xFFBE, False)
    if mapped is None and 0xFFB0 <= keyval <= 0xFFB9:  # KP_0..KP_9
        mapped = (0x60 + keyval - 0xFFB0, False)
    if mapped is None and 0x20 <= codepoint <= 0x7E:
        character = chr(codepoint)
        if character.isalpha():
            mapped = (ord(character.upper()), False)
        elif character.isdigit():
            mapped = (ord(character), False)
        elif character == " ":
            mapped = (0x20, False)
        elif character in _PUNCTUATION_VKS:
            mapped = (_PUNCTUATION_VKS[character], False)
        elif character in _SHIFTED_DIGITS:
            mapped = (_SHIFTED_DIGITS[character], False)
    if mapped is not None:
        return KeyboardEvent(
            action="down" if pressed else "up",
            virtual_key=mapped[0],
            extended=mapped[1],
        )
    if pressed and (
        1 <= codepoint <= 0x10FFFF
        and not 0xD800 <= codepoint <= 0xDFFF
    ):
        return KeyboardEvent(action="press", unicode=codepoint)
    return None


class KeyboardSender:
    """Serialize keyboard boundaries and release held guest keys on blur."""

    def __init__(
        self, client: AgentClient, hwnd: int, shm_input: ShmInputWriter | None = None
    ) -> None:
        self.client = client
        self.hwnd = hwnd
        self.shm_input = shm_input
        self._events: collections.deque[KeyboardEvent] = collections.deque()
        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="wayseam-keyboard", daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._condition:
            self._condition.notify_all()
        self._thread.join(timeout=1.0)

    def submit(self, event: KeyboardEvent) -> None:
        with self._condition:
            self._events.append(event)
            self._condition.notify()

    def release_all(self) -> None:
        self.submit(KeyboardEvent(action="release_all"))

    def _send(self, event: KeyboardEvent) -> None:
        if self.shm_input is not None:
            if event.action == "press":
                if self.shm_input.unicode(self.hwnd, event.unicode):
                    return
            elif self.shm_input.key(
                self.hwnd, event.virtual_key,
                down=event.action == "down", extended=event.extended,
            ):
                return
        self.client.window_keyboard_input(
            self.hwnd,
            action=event.action,
            virtual_key=event.virtual_key,
            unicode=event.unicode,
            extended=event.extended,
        )

    def _run(self) -> None:
        pressed: dict[int, bool] = {}
        while not self._stop.is_set():
            with self._condition:
                self._condition.wait_for(
                    lambda: bool(self._events) or self._stop.is_set(),
                    timeout=0.1,
                )
                if self._stop.is_set():
                    break
                if not self._events:
                    continue
                event = self._events.popleft()
            if event.action == "release_all":
                for virtual_key, extended in tuple(pressed.items()):
                    try:
                        self._send(KeyboardEvent("up", virtual_key, 0, extended))
                    except Exception:
                        pass
                pressed.clear()
                continue
            if event.action == "up" and event.virtual_key not in pressed:
                continue
            try:
                self._send(event)
            except Exception:
                continue
            if event.action == "down":
                pressed[event.virtual_key] = event.extended
            elif event.action == "up":
                pressed.pop(event.virtual_key, None)
        for virtual_key, extended in pressed.items():
            try:
                self._send(KeyboardEvent("up", virtual_key, 0, extended))
            except Exception:
                pass


def fitted_geometry(
    host_size: tuple[int, int],
    frame_size: tuple[int, int],
) -> tuple[float, float, float]:
    """Return ``(scale, offset_x, offset_y)`` for centered scale-down fitting.

    This is the single source of truth shared by the renderer and
    ``map_synced_pointer`` so pixels and pointer coordinates never disagree.
    """
    host_width, host_height = host_size
    frame_width, frame_height = frame_size
    if min(host_width, host_height, frame_width, frame_height) <= 0:
        return 1.0, 0.0, 0.0
    scale = min(1.0, host_width / frame_width, host_height / frame_height)
    return (
        scale,
        (host_width - frame_width * scale) / 2.0,
        (host_height - frame_height * scale) / 2.0,
    )


def make_surface_view_class(Gtk, Gdk, GLib, Graphene):
    """Build the damage-aware surface widget against the loaded GTK bindings.

    A ``Gtk.Picture`` needs a complete new texture per frame: one 17 MB copy
    into ``GLib.Bytes`` and one full GPU upload even when a 4 KB hover
    rectangle changed. This widget keeps the last full texture and stacks the
    small damage-rectangle textures on top in ``snapshot``, so the per-frame
    host cost is proportional to the damage. The pump bounds patch count and
    bytes, after which a fresh base texture is uploaded.
    """

    class SurfaceView(Gtk.Widget):
        def __init__(self) -> None:
            super().__init__()
            self._base = None
            self._patches: list[tuple[int, int, object]] = []
            self._cursor = None  # (texture, x, y, hot_x, hot_y) in frame pixels
            self.size = (0, 0)
            self.set_overflow(Gtk.Overflow.HIDDEN)

        def set_cursor_overlay(self, texture, x, y, hot_x, hot_y) -> None:
            """Draw the guest cursor in-frame so it shares the frame's timeline.

            Placing the cursor in the same content the ink lives in, with the
            hotspot put exactly on the pointer, keeps the visible pointer on
            the paint point for every cursor shape. The compositor's own
            handling of a client cursor's hotspot is unreliable here (it
            offsets differently per shape — a brush ring stays put but a
            pencil-tip cursor drifts), so we bypass it entirely.
            """
            nxt = None if texture is None else (texture, x, y, hot_x, hot_y)
            if nxt != self._cursor:
                self._cursor = nxt
                self.queue_draw()

        @property
        def patch_count(self) -> int:
            return len(self._patches)

        def set_full(self, texture) -> None:
            self._base = texture
            self._patches = []
            size = (texture.get_width(), texture.get_height())
            if size != self.size:
                self.size = size
                self.queue_resize()
            self.queue_draw()

        def add_patch(self, x: int, y: int, texture) -> None:
            if self._base is None:
                return
            self._patches.append((x, y, texture))
            self.queue_draw()

        def do_measure(self, orientation, _for_size):
            natural = self.size[0] if orientation == Gtk.Orientation.HORIZONTAL else self.size[1]
            return 0, natural, -1, -1

        def do_snapshot(self, snapshot) -> None:
            if self._base is None:
                return
            scale, offset_x, offset_y = fitted_geometry(
                (self.get_width(), self.get_height()), self.size,
            )
            snapshot.save()
            snapshot.translate(Graphene.Point().init(offset_x, offset_y))
            snapshot.scale(scale, scale)
            snapshot.append_texture(
                self._base, Graphene.Rect().init(0, 0, self.size[0], self.size[1]),
            )
            for x, y, texture in self._patches:
                snapshot.append_texture(
                    texture,
                    Graphene.Rect().init(x, y, texture.get_width(), texture.get_height()),
                )
            if self._cursor is not None:
                texture, cx, cy, hx, hy = self._cursor
                snapshot.append_texture(
                    texture,
                    Graphene.Rect().init(
                        cx - hx, cy - hy, texture.get_width(), texture.get_height(),
                    ),
                )
            snapshot.restore()

    class CursorLayer(Gtk.Widget):
        """Topmost overlay that draws the guest cursor above everything.

        Owned windows (Affinity confirmation dialogs, menus) are painted as
        separate picture overlays stacked above the main frame; a cursor
        composited into the frame itself disappears under them. This layer
        sits above every owned picture and shares the frame's fitted
        geometry so the hotspot still lands on the ink point.
        """

        def __init__(self, view) -> None:
            super().__init__()
            self._view = view
            self._cursor = None
            self.set_can_target(False)

        def set_cursor_overlay(self, texture, x, y, hot_x, hot_y) -> None:
            nxt = None if texture is None else (texture, x, y, hot_x, hot_y)
            if nxt != self._cursor:
                self._cursor = nxt
                self.queue_draw()

        def do_snapshot(self, snapshot) -> None:
            if self._cursor is None or self._view.size == (0, 0):
                return
            scale, offset_x, offset_y = fitted_geometry(
                (self.get_width(), self.get_height()), self._view.size,
            )
            texture, cx, cy, hx, hy = self._cursor
            snapshot.save()
            snapshot.translate(Graphene.Point().init(offset_x, offset_y))
            snapshot.scale(scale, scale)
            snapshot.append_texture(
                texture,
                Graphene.Rect().init(
                    cx - hx, cy - hy, texture.get_width(), texture.get_height(),
                ),
            )
            snapshot.restore()

    SurfaceView.CursorLayer = CursorLayer
    return SurfaceView


def map_synced_pointer(
    x: float,
    y: float,
    *,
    host_size: tuple[int, int],
    guest_size: tuple[int, int],
    frame_size: tuple[int, int],
) -> tuple[int, int] | None:
    """Map through GTK's centered ``SCALE_DOWN`` presentation geometry.

    The captured frame and the guest window rectangle may disagree by a few
    pixels of invisible border (observed after a live DPI change); a strict
    equality gate then drops every pointer event and strands the guest
    cursor. Render geometry (the frame) is authoritative; a small tolerance
    covers border skew while still pausing input across real resizes.
    """
    if (
        abs(guest_size[0] - frame_size[0]) > 16
        or abs(guest_size[1] - frame_size[1]) > 16
    ):
        return None
    guest_size = frame_size
    host_width, host_height = host_size
    guest_width, guest_height = guest_size
    if min(host_width, host_height, guest_width, guest_height) <= 0:
        return None
    scale, offset_x, offset_y = fitted_geometry(host_size, frame_size)
    rendered_width = guest_width * scale
    rendered_height = guest_height * scale
    if not (
        offset_x <= x < offset_x + rendered_width
        and offset_y <= y < offset_y + rendered_height
    ):
        return None
    guest_x = min(guest_width - 1, int((x - offset_x) / scale))
    guest_y = min(guest_height - 1, int((y - offset_y) / scale))
    return guest_x, guest_y


class InputSender:
    """Serialize guest input while coalescing only adjacent motion events."""

    def __init__(
        self, client: AgentClient, hwnd: int, shm_input: ShmInputWriter | None = None
    ) -> None:
        self.client = client
        self.hwnd = hwnd
        self.shm_input = shm_input
        self._events: collections.deque[PointerEvent] = collections.deque()
        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="wayseam-input", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._condition:
            self._condition.notify_all()
        self._thread.join(timeout=1.0)

    def submit(self, event: PointerEvent) -> None:
        with self._condition:
            if event.action == "move" and self._events and self._events[-1].action == "move":
                self._events[-1] = event
            elif (
                event.action == "wheel"
                and self._events
                and self._events[-1].action == "wheel"
            ):
                # Fold bursts of wheel ticks into one bounded delivery instead
                # of queueing a scroll backlog behind a slow guest round trip.
                last = self._events[-1]
                limit = 100 * WHEEL_DELTA
                self._events[-1] = PointerEvent(
                    event.x,
                    event.y,
                    "wheel",
                    0,
                    max(-limit, min(limit, last.delta_x + event.delta_x)),
                    max(-limit, min(limit, last.delta_y + event.delta_y)),
                )
            elif len(self._events) < 64:
                self._events.append(event)
            elif event.action != "move":
                # Preserve button boundaries by sacrificing the oldest motion
                # event when an extreme backlog reaches the hard bound.
                for index, queued in enumerate(self._events):
                    if queued.action == "move":
                        del self._events[index]
                        self._events.append(event)
                        break
            self._condition.notify()

    def _run(self) -> None:
        pressed: set[int] = set()
        last_position = (0, 0)
        while not self._stop.is_set():
            with self._condition:
                self._condition.wait_for(
                    lambda: bool(self._events) or self._stop.is_set(),
                    timeout=0.1,
                )
                if self._stop.is_set():
                    break
                if not self._events:
                    continue
                event = self._events.popleft()
            last_position = (event.x, event.y)
            try:
                if event.action == "wheel":
                    if event.delta_x or event.delta_y:
                        if not (
                            self.shm_input is not None
                            and self.shm_input.wheel(
                                self.hwnd, event.x, event.y, event.delta_x, event.delta_y,
                            )
                        ):
                            self.client.window_pointer_input(
                                self.hwnd, x=event.x, y=event.y, action="wheel",
                                delta_x=event.delta_x, delta_y=event.delta_y,
                            )
                    continue
                if event.action == "move":
                    if not (
                        self.shm_input is not None
                        and self.shm_input.move(self.hwnd, event.x, event.y)
                    ):
                        self.client.window_pointer_input(
                            self.hwnd, x=event.x, y=event.y, action="move", button=0,
                        )
                    continue
                # Clicks stay on HTTP: the guest route runs the managed-by-host
                # hit test. Flush queued ring motion first so the click lands
                # at the position the user sees.
                if self.shm_input is not None:
                    self.shm_input.flush()
                if event.action == "down" and event.button in pressed:
                    # A cancelled host gesture must never leave the guest's
                    # global mouse button held. Reset before the new press.
                    self.client.window_pointer_input(
                        self.hwnd,
                        x=event.x,
                        y=event.y,
                        action="up",
                        button=event.button,
                    )
                    pressed.remove(event.button)
                elif event.action == "up" and event.button not in pressed:
                    continue
                self.client.window_pointer_input(
                    self.hwnd, x=event.x, y=event.y,
                    action=event.action, button=event.button,
                )
                if event.action == "down":
                    pressed.add(event.button)
                elif event.action == "up":
                    pressed.remove(event.button)
            except Exception:  # noqa: BLE001 - input is best effort
                pass
        for button in pressed:
            try:
                self.client.window_pointer_input(
                    self.hwnd,
                    x=last_position[0],
                    y=last_position[1],
                    action="up",
                    button=button,
                )
            except Exception:
                pass


@dataclass(frozen=True)
class PointerRoute:
    """A guest input sender plus its host-to-guest coordinate mapper."""

    sender: InputSender
    mapper: Callable[[float, float], tuple[int, int] | None]


class PointerRouter:
    """Keep raw pointer sequences bound to the HWND pressed beneath them."""

    def __init__(
        self,
        resolver: Callable[[float, float], PointerRoute | None],
    ) -> None:
        self.resolver = resolver
        self._pressed: dict[int, tuple[PointerRoute, tuple[int, int]]] = {}

    def motion(self, x: float, y: float) -> None:
        if not self._pressed:
            route = self.resolver(x, y)
            if route is None:
                return
            mapped = route.mapper(x, y)
            if mapped is not None:
                route.sender.submit(PointerEvent(*mapped, "move"))
            return
        seen: set[int] = set()
        for _button, (route, last) in tuple(self._pressed.items()):
            sender_id = id(route.sender)
            if sender_id in seen:
                continue
            seen.add(sender_id)
            mapped = route.mapper(x, y)
            if mapped is None:
                mapped = last
            route.sender.submit(PointerEvent(*mapped, "move"))
            for active_button, (active_route, _active_last) in tuple(self._pressed.items()):
                if active_route.sender is route.sender:
                    self._pressed[active_button] = (active_route, mapped)

    def scroll(self, x: float, y: float, delta_x: int, delta_y: int) -> None:
        """Deliver a wheel step to the HWND under the pointer (or the drag target)."""
        if not (delta_x or delta_y):
            return
        if self._pressed:
            route, last = next(iter(self._pressed.values()))
            mapped = route.mapper(x, y) or last
        else:
            route = self.resolver(x, y)
            if route is None:
                return
            mapped = route.mapper(x, y)
            if mapped is None:
                return
        route.sender.submit(PointerEvent(*mapped, "wheel", 0, delta_x, delta_y))

    def button(self, x: float, y: float, button: int, *, pressed: bool) -> None:
        if pressed:
            route = self.resolver(x, y)
            if route is None:
                return
            mapped = route.mapper(x, y)
            if mapped is None:
                return
            previous = self._pressed.pop(button, None)
            if previous is not None:
                previous[0].sender.submit(PointerEvent(*previous[1], "up", button))
            route.sender.submit(PointerEvent(*mapped, "down", button))
            self._pressed[button] = (route, mapped)
            return
        active = self._pressed.pop(button, None)
        if active is None:
            return
        route, last = active
        mapped = route.mapper(x, y)
        if mapped is None:
            mapped = last
        route.sender.submit(PointerEvent(*mapped, "up", button))


@dataclass(frozen=True)
class CursorSample:
    cursor: WayseamCursor
    captured_at: float


class CursorPump:
    """Poll the OS cursor independently because HWND frames never contain it."""

    def __init__(self, client: AgentClient, root_hwnd: int, *, max_fps: int = 60) -> None:
        self.client = client
        self.root_hwnd = root_hwnd
        self.max_fps = max(1, min(max_fps, 120))
        self._lock = threading.Lock()
        self._latest: CursorSample | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="wayseam-cursor", daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)

    def take_latest(self) -> CursorSample | None:
        with self._lock:
            value = self._latest
            self._latest = None
            return value

    def _run(self) -> None:
        period = 1.0 / self.max_fps
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                value = CursorSample(
                    cursor=self.client.window_cursor(self.root_hwnd),
                    captured_at=time.monotonic(),
                )
                with self._lock:
                    self._latest = value
            except Exception:
                pass
            remaining = period - (time.monotonic() - started)
            if remaining > 0:
                self._stop.wait(remaining)


class ClipboardSync:
    """Mirror text between the host clipboard and the guest while focused.

    Windows has one clipboard per session, Wayland one per seat; nothing
    bridges them for Wayseam Windows. The presenter that holds keyboard focus
    pushes the host text into the guest on focus-in (so Ctrl+V pastes what was
    copied in a Linux app) and mirrors guest changes back while focused and
    once more on focus-out (so text copied inside the Windows app is available
    to Linux apps). Only ``text/plain`` travels; guest sequence numbers avoid
    re-reading unchanged clipboards and echoing our own writes.
    """

    def __init__(
        self,
        client: AgentClient,
        *,
        read_host: Callable[[Callable[[str | None], None]], None],
        write_host: Callable[[str], None],
    ) -> None:
        self.client = client
        self.read_host = read_host
        self.write_host = write_host
        self._lock = threading.Lock()
        self._guest_sequence: int | None = None
        self._last_host_text: str | None = None
        self._last_guest_text: str | None = None
        self.error: str | None = None

    def push_host(self) -> None:
        """Copy the host clipboard text into the guest if it changed."""
        self.read_host(self._push_host_text)

    def _push_host_text(self, text: str | None) -> None:
        if not text or text == self._last_host_text or text == self._last_guest_text:
            return
        if len(text) > AgentClient.CLIPBOARD_MAX_CHARS:
            return
        with self._lock:
            try:
                self._guest_sequence = self.client.clipboard_set(text)
                self._last_host_text = text
                self.error = None
            except Exception as exc:  # noqa: BLE001 - clipboard is best effort
                self.error = f"{type(exc).__name__}: {exc}"

    def pull_guest(self) -> bool:
        """Mirror a changed guest clipboard into the host; True when copied."""
        with self._lock:
            try:
                clip = self.client.clipboard_get(since=self._guest_sequence)
                self.error = None
            except Exception as exc:  # noqa: BLE001 - clipboard is best effort
                self.error = f"{type(exc).__name__}: {exc}"
                return False
            self._guest_sequence = clip.sequence
            if not clip.changed or not clip.text or clip.text == self._last_guest_text:
                return False
            self._last_guest_text = clip.text
            self._last_host_text = clip.text
        self.write_host(clip.text)
        return True


_HYPRLAND_SOCKET: list[Path | None] = [None]


def _hyprland_socket() -> Path:
    """Locate Hyprland's IPC socket, surviving an incomplete environment.

    Presenters are spawned by the watcher, whose own environment (systemd
    unit, agent context) may lack HYPRLAND_INSTANCE_SIGNATURE entirely; with
    only the env lookup, every compositor query silently failed in exactly
    those processes. Fall back to discovering the live socket on disk.
    """
    cached = _HYPRLAND_SOCKET[0]
    if cached is not None and cached.exists():
        return cached
    runtime = os.environ.get("XDG_RUNTIME_DIR", "") or f"/run/user/{os.getuid()}"
    signature = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "")
    candidates = []
    if signature:
        candidates.append(Path(runtime) / "hypr" / signature / ".socket.sock")
    hypr_dir = Path(runtime) / "hypr"
    try:
        discovered = sorted(
            hypr_dir.glob("*/.socket.sock"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        discovered = []
    candidates.extend(discovered)
    for candidate in candidates:
        if candidate.exists():
            _HYPRLAND_SOCKET[0] = candidate
            return candidate
    raise OSError("not running under Hyprland")


def _hyprland_query(request: str, *, timeout: float = 0.25) -> bytes:
    """Send one request on Hyprland's IPC socket and return the reply."""
    import socket

    path = _hyprland_socket()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(str(path))
        sock.sendall(request.encode("utf-8"))
        chunks: list[bytes] = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            if sum(len(c) for c in chunks) > 8 * 1024 * 1024:
                raise OSError("Hyprland reply too large")
    return b"".join(chunks)


class CompositorPointerLocator:
    """Find the real pointer position relative to this presenter's window.

    Wayland only tells a client where the pointer is through events. When a
    tile is resized from the keyboard, floated, or re-tiled while the mouse is
    still, Hyprland moves the surface under the pointer without sending any
    motion, so the client's last known surface-relative position is stale.
    Hyprland's IPC knows both the global cursor and the window origin, which
    is enough to compute the true surface-relative point; on other
    compositors the GDK-cached position is the best available fallback.
    """

    def __init__(
        self,
        *,
        pid: int | None = None,
        app_id: str = "",
        query: Callable[[str], bytes] = _hyprland_query,
    ) -> None:
        self.pid = pid or os.getpid()
        self.app_id = app_id
        self.query = query

    def locate_wayseam_window(self) -> tuple[str, int, float, float] | None:
        """Return ``(app_id, pid, x, y)`` of the Wayseam window under the cursor.

        Under focus-follows-mouse=0 the compositor routes every pointer event
        to the focused window, so a scroll made while hovering a different
        Wayseam Window arrives at the wrong presenter. The receiving presenter
        uses this to forward the wheel to the window actually under the
        pointer.
        """
        import json

        try:
            cursor = json.loads(self.query("j/cursorpos").decode("utf-8"))
            clients = json.loads(self.query("j/clients").decode("utf-8"))
            cx, cy = float(cursor["x"]), float(cursor["y"])
            for client in clients:
                cls = str(client.get("class", ""))
                if not cls.startswith("org.wayseam."):
                    continue
                left, top = (float(v) for v in client["at"])
                width, height = (float(v) for v in client["size"])
                if left <= cx < left + width and top <= cy < top + height:
                    return cls, int(client.get("pid", -1)), cx - left, cy - top
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return None

    def locate_own_tile(self, app_id: str) -> tuple[int, int] | None:
        """Return this window's tile origin in compositor coordinates.

        Wayland never tells a client where its surface sits; only the
        compositor knows. The guest mirrors the host tile layout (window at
        the same desktop position as its tile), so presented windows never
        overlap — Windows routes wheel input by z-order under the cursor,
        and overlapping guest windows made scrolling one tile scroll another.
        """
        import json

        try:
            clients = json.loads(self.query("j/clients").decode("utf-8"))
            for client in clients:
                if client.get("class") == app_id:
                    left, top = (int(v) for v in client["at"])
                    return max(0, left), max(0, top)
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return None

    def locate(self) -> tuple[float, float] | None:
        """Return ``(x, y)`` inside this window, or ``None`` when unknown/outside."""
        import json

        try:
            cursor = json.loads(self.query("j/cursorpos").decode("utf-8"))
            clients = json.loads(self.query("j/clients").decode("utf-8"))
            cx, cy = float(cursor["x"]), float(cursor["y"])
            for client in clients:
                if int(client.get("pid", -1)) != self.pid:
                    continue
                if self.app_id and str(client.get("class", "")) != self.app_id:
                    continue
                left, top = (float(v) for v in client["at"])
                width, height = (float(v) for v in client["size"])
                x, y = cx - left, cy - top
                if 0 <= x < width and 0 <= y < height:
                    return x, y
                return None
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return None


def presenter_hwnd_for_pid(pid: int) -> int | None:
    """Read another presenter's HWND from its argv (host-side registry)."""
    try:
        parts = [
            part.decode("utf-8", "replace")
            for part in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            if part
        ]
        index = parts.index("--hwnd")
        return int(parts[index + 1], 0)
    except (OSError, ValueError, IndexError):
        return None


class PointerResync:
    """Keep the guest pointer under the real cursor when the surface changes.

    The guest only learns where the pointer is from the motion events GTK
    delivers. When the compositor moves or resizes the surface under a still
    pointer (a keyboard or mouse resize, a float/tile toggle, a workspace or
    monitor change, or a fresh window after switching back from Desktop Mode),
    no motion event follows, so hover highlights, brush previews, and the
    app's own cursor stay pinned to a stale, wrongly mapped spot until the
    user jiggles the mouse. That is the "drift".

    Rather than try to detect every event that could cause it, this
    continuously reconciles: whenever the pointer is at rest inside the
    surface it re-reads the compositor's true cursor position and re-maps it,
    so any drift self-heals within ``REST_DELAY`` regardless of cause. It
    stays out of the way while the user is actively moving (motion events
    already carry the position at lower latency) and while a button is held
    (a drag must not be perturbed). A geometry change triggers an immediate
    reconcile instead of waiting for the rest interval.

    GTK also synthesizes one motion event from its cached, now stale,
    surface-relative position when the surface moves under a still pointer;
    motions matching that pre-change position are ignored briefly so they do
    not re-introduce the drift the reconcile just removed.
    """

    STALE_WINDOW = 0.5
    REST_DELAY = 0.12
    PERIODIC_INTERVAL = 0.12

    def __init__(
        self,
        locate: Callable[[], tuple[float, float] | None] | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._geometry: tuple[object, ...] | None = None
        self.pointer: tuple[float, float] | None = None
        self.busy = False
        self.locate = locate
        self.clock = clock
        self._stale: tuple[float, float] | None = None
        self._stale_until = 0.0
        self._last_motion = 0.0
        self._periodic_at = 0.0
        self._last_point: tuple[float, float] | None = None

    def track(self, x: float, y: float) -> None:
        self.pointer = (x, y)
        self._last_motion = self.clock()
        self._last_point = (x, y)

    def leave(self) -> None:
        self.pointer = None

    def is_stale(self, x: float, y: float) -> bool:
        """True for a synthetic motion that repeats the pre-change position."""
        if self._stale is None or self.clock() >= self._stale_until:
            return False
        return abs(x - self._stale[0]) < 0.5 and abs(y - self._stale[1]) < 0.5

    def _resolve(self, now: float, *, allow_stale_fallback: bool) -> tuple[float, float] | None:
        located = self.locate() if self.locate is not None else None
        if located is None:
            # The compositor says the pointer is outside our window (or the
            # query failed). Only a geometry-change reconcile may fall back to
            # the last event position; the periodic path must not guess.
            return self.pointer if allow_stale_fallback else None
        if self.pointer is not None and located != self.pointer:
            self._stale = self.pointer
            self._stale_until = now + self.STALE_WINDOW
        self.pointer = located
        return located

    def invalidate(self) -> None:
        """Force the next reconcile to resend even an unchanged point.

        Used when the GUEST side moved (window re-placed on the guest
        desktop) while the host pointer stayed still: the mapped point is
        identical but the guest cursor is stranded at the old screen
        position until re-injected.
        """
        self._last_point = None
        self._periodic_at = 0.0

    def poll(self, geometry: tuple[object, ...]) -> tuple[float, float] | None:
        """Return the pointer to replay this tick, or ``None`` to do nothing."""
        now = self.clock()
        changed = geometry != self._geometry
        first = self._geometry is None
        self._geometry = geometry

        if self.busy:
            return None

        if changed and not first:
            point = self._resolve(now, allow_stale_fallback=True)
            if point is not None:
                self._last_point = point
            return point

        # Periodic self-heal while the pointer is at rest. This also runs
        # when no GTK events ever arrived (self.pointer is None): under
        # focus-follows-mouse=0 Hyprland routes pointer events to the
        # keyboard-focused window, so hovering an unfocused Wayseam Window
        # produces no ENTER/motion — the compositor locator is then the only
        # source of truth for "the pointer is over this window".
        if (
            now - self._last_motion >= self.REST_DELAY
            and now - self._periodic_at >= self.PERIODIC_INTERVAL
        ):
            self._periodic_at = now
            point = self._resolve(now, allow_stale_fallback=False)
            if point is None:
                # The compositor says the pointer is not over this window;
                # clear an event-less adopted position so the cursor hides.
                if self.pointer is not None and now - self._last_motion >= self.REST_DELAY:
                    self.pointer = None
                return None
            if point == self._last_point:
                return None
            self._last_point = point
            return point
        return None


@dataclass(frozen=True)
class WindowTree:
    root: dict[str, int]
    owned: tuple[WayseamWindow, ...]


def independent_owned_window(
    window: WayseamWindow,
    root: dict[str, int],
) -> bool:
    """Treat full-size owned app roots as compositor-managed windows.

    WPF applications such as Affinity attach their welcome/editor root to an
    owner HWND even though it is a complete application window. Rendering it
    as a popup alpha overlay merges two windows and leaves the owner's unused
    canvas visible. Small menus, palettes, and dialogs remain transient
    overlays.
    """
    root_width = max(1, int(root["width"]))
    root_height = max(1, int(root["height"]))
    return (
        window.width >= 640
        and window.height >= 480
        and window.width * 10 >= root_width * 9
        and window.height * 10 >= root_height * 9
    )


class WindowTreePump:
    """Observe owned popup HWNDs without blocking GTK's render thread."""

    def __init__(self, client: AgentClient, root_hwnd: int) -> None:
        self.client = client
        self.root_hwnd = root_hwnd
        self._lock = threading.Lock()
        self._latest: WindowTree | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="wayseam-window-tree", daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)

    def take_latest(self) -> WindowTree | None:
        with self._lock:
            value = self._latest
            self._latest = None
            return value

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                root, owned = self.client.owned_windows(self.root_hwnd)
                with self._lock:
                    self._latest = WindowTree(root=root, owned=tuple(owned))
            except Exception:
                pass
            self._stop.wait(0.075)


class ResizeSender:
    """Debounce compositor resizes and apply only the newest guest size."""

    def __init__(
        self,
        client: AgentClient,
        hwnd: int,
        *,
        retry_delay: float = 1.0,
    ) -> None:
        self.client = client
        self.hwnd = hwnd
        self._lock = threading.Lock()
        self._latest: tuple[int, int, tuple[int, int] | None] | None = None
        self._last_sent: tuple[int, int, tuple[int, int] | None] | None = None
        self._retry_delay = retry_delay
        self._retry_at = 0.0
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="wayseam-resize", daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=1.0)

    def submit(
        self, width: int, height: int, origin: tuple[int, int] | None = None,
    ) -> None:
        if width < 160 or height < 120:
            return
        with self._lock:
            if origin is None and self._latest is not None:
                origin = self._latest[2]
            self._latest = (width, height, origin)
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(0.1)
            self._wake.clear()
            if self._stop.wait(0.1):
                break
            if self._wake.is_set():
                continue
            with self._lock:
                request = self._latest
            if request is None or request == self._last_sent:
                continue
            if time.monotonic() < self._retry_at:
                continue
            try:
                actual = self.client.window_resize(
                    self.hwnd, width=request[0], height=request[1],
                    origin=request[2],
                )
                if actual == (request[0], request[1]):
                    self._last_sent = request
                    self._retry_at = 0.0
                else:
                    self._retry_at = time.monotonic() + self._retry_delay
            except Exception as exc:  # noqa: BLE001 - report, then retry
                print(f"wayseam: resize failed: {exc!r}", file=sys.stderr)
                self._retry_at = time.monotonic() + self._retry_delay


class ShellVisibilityLease:
    """Hide guest shell bars while at least one Wayseam presenter is alive."""

    def __init__(
        self,
        client: AgentClient,
        *,
        directory: Path | None = None,
        pid: int | None = None,
        process_alive: Callable[[int], bool] | None = None,
    ) -> None:
        self.client = client
        self.directory = directory or runtime_dir() / "wayseam-shell"
        self.pid = pid or os.getpid()
        self.process_alive = process_alive or self._process_alive
        self.lease_path = self.directory / f"{self.pid}.lease"
        self._acquired = False

    @staticmethod
    def _process_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _cleanup_stale(self) -> None:
        for lease in self.directory.glob("*.lease"):
            try:
                pid = int(lease.stem)
            except ValueError:
                lease.unlink(missing_ok=True)
                continue
            if not self.process_alive(pid):
                lease.unlink(missing_ok=True)

    def acquire(self) -> None:
        if self._acquired:
            return
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_path = self.directory / "lock"
        with lock_path.open("a+", encoding="ascii") as lock:
            os.chmod(lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            self._cleanup_stale()
            self.lease_path.touch(mode=0o600, exist_ok=True)
            os.chmod(self.lease_path, 0o600)
            try:
                self.client.window_shell_visibility(visible=False)
            except Exception:
                self.lease_path.unlink(missing_ok=True)
                raise
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        self._acquired = True

    def release(self) -> None:
        if not self._acquired:
            return
        lock_path = self.directory / "lock"
        with lock_path.open("a+", encoding="ascii") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            self.lease_path.unlink(missing_ok=True)
            self._cleanup_stale()
            if not any(self.directory.glob("*.lease")):
                self.client.window_shell_visibility(visible=True)
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        self._acquired = False


def run_window(
    hwnd: int,
    title: str,
    max_fps: int,
    *,
    app_id: str = "org.wayseam.Window",
    icon_name: str = "wayseam",
) -> int:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    gi.require_version("Graphene", "1.0")
    from gi.repository import Gdk, Gio, GLib, Graphene, Gtk

    SurfaceView = make_surface_view_class(Gtk, Gdk, GLib, Graphene)

    cfg = Config.load()
    client = AgentClient(cfg)
    shell_lease = ShellVisibilityLease(client)
    initial_root, initial_owned = client.owned_windows(hwnd)
    # Prefer the shared-memory ring (ADR 0004): the guest pushes WSD1 frames
    # into an IVSHMEM slot and the HTTP request/response leaves the hot path
    # entirely. Any failure here falls back to HTTP transparently.
    input_sender = InputSender(client, hwnd)
    keyboard_sender = KeyboardSender(client, hwnd)

    def build_shm_source():
        """Assign a ring slot and wire the input writer; None when unavailable.

        Used at startup and by the pump's periodic HTTP->shm upgrade, so a
        window that starts during an agent restart heals onto the ring (and
        the input ring) without a presenter restart.
        """
        slot = client.shm_assign(hwnd)
        source = ShmFrameSource(default_ring_path(cfg), slot.slot)
        try:
            writer = ShmInputWriter(source._buf)
        except Exception as exc:  # noqa: BLE001 - input ring is best effort
            print(f"wayseam: shm input unavailable ({exc}); using HTTP", flush=True)
            writer = None
        input_sender.shm_input = writer
        keyboard_sender.shm_input = writer
        return source

    shm_source = None
    shm_error = None
    for _attempt in range(5):
        try:
            shm_source = build_shm_source()
            break
        except AgentUnavailableError as exc:
            # The agent maps the single-mapper IVSHMEM device lazily; a slot
            # assignment can briefly 503 while another presenter is racing the
            # first mapping. Retry a few times before dropping to HTTP.
            shm_error = exc
            time.sleep(0.3)
        except Exception as exc:  # noqa: BLE001 - no device / old agent / no ring
            shm_error = exc
            break
    if shm_source is None and shm_error is not None:
        print(f"wayseam: shm transport unavailable ({shm_error}); using HTTP", flush=True)
    pump = LatestFramePump(client, hwnd, max_fps=max_fps, shm=shm_source)
    pump.set_shm_factory(build_shm_source)
    tree_pump = WindowTreePump(client, hwnd)
    cursor_pump = CursorPump(client, hwnd, max_fps=min(60, max_fps * 2))
    resize_sender = ResizeSender(client, hwnd)
    forward_close = {"enabled": True}
    app = Gtk.Application(
        application_id=app_id,
        flags=Gio.ApplicationFlags.NON_UNIQUE,
    )

    def activate(application) -> None:
        window = Gtk.ApplicationWindow(application=application, title=title)
        display = window.get_display()
        # The guest frame already contains the application's title bar. A GTK
        # decoration would steal pixels from the requested client size and
        # force SCALE_DOWN, making both text and transient offsets blurry.
        window.set_decorated(False)
        window.set_resizable(True)
        window.set_default_size(initial_root["width"], initial_root["height"])
        window.set_icon_name(icon_name)
        overlay = Gtk.Overlay()
        overlay.set_focusable(True)
        view = SurfaceView()
        view.set_can_target(False)
        overlay.set_child(view)
        cursor_layer = SurfaceView.CursorLayer(view)
        overlay.add_overlay(cursor_layer)
        overlay.set_clip_overlay(cursor_layer, False)
        window.set_child(overlay)
        state = {
            "configured": True,
            "sequence": 0,
            "root_width": 0,
            "root_height": 0,
            "owned": {},
            "allocation": None,
            "cursor_digest": None,
            "cursor": None,
            "cursor_texture": None,
            "cursor_hot": (0, 0),
            "cursor_visible": False,
            "root": initial_root,
            "frame_size": (initial_root["width"], initial_root["height"]),
            "title": "",
        }

        def texture_from_surface(surface: WayseamFrame):
            # PyGObject copies ``bytes`` into GLib.Bytes with one memcpy but
            # marshals a ``bytearray``/memoryview element by element (~375 ms
            # for a 2110x2110 surface). The canvas is a mutable bytearray, so
            # snapshot it to bytes first (~1 ms) and never hand the view a
            # buffer the pump thread keeps writing into.
            pixels = surface.pixels
            if not isinstance(pixels, bytes):
                pixels = bytes(pixels)
            return Gdk.MemoryTexture.new(
                surface.width,
                surface.height,
                Gdk.MemoryFormat.B8G8R8A8,
                GLib.Bytes.new(pixels),
                surface.stride,
            )

        def present_frame(frame: Frame) -> None:
            surface = frame.surface
            size = (surface.width, surface.height)
            if frame.damage is None or size != view.size:
                view.set_full(texture_from_surface(surface))
                return
            for (x, y, width, height), pixels in frame.damage:
                view.add_patch(
                    x,
                    y,
                    Gdk.MemoryTexture.new(
                        width,
                        height,
                        Gdk.MemoryFormat.B8G8R8A8,
                        GLib.Bytes.new(pixels),
                        width * 4,
                    ),
                )

        def map_pointer(x: float, y: float) -> tuple[int, int] | None:
            root = state["root"]
            return map_synced_pointer(
                x,
                y,
                host_size=(overlay.get_width(), overlay.get_height()),
                guest_size=(root["width"], root["height"]),
                frame_size=state["frame_size"],
            )

        def resolve_pointer(x: float, y: float) -> PointerRoute | None:
            root_point = map_pointer(x, y)
            if root_point is None:
                return None
            root = state["root"]
            for child in reversed(tuple(state["owned"].values())):
                info = child["info"]
                offset_x = info.left - root["left"]
                offset_y = info.top - root["top"]
                if not (
                    offset_x <= root_point[0] < offset_x + info.width
                    and offset_y <= root_point[1] < offset_y + info.height
                ):
                    continue

                def child_mapper(
                    host_x: float,
                    host_y: float,
                    *,
                    selected=child,
                ) -> tuple[int, int] | None:
                    mapped_root = map_pointer(host_x, host_y)
                    if mapped_root is None:
                        return None
                    selected_info = selected["info"]
                    selected_root = state["root"]
                    local_x = mapped_root[0] - (selected_info.left - selected_root["left"])
                    local_y = mapped_root[1] - (selected_info.top - selected_root["top"])
                    if not (
                        0 <= local_x < selected_info.width
                        and 0 <= local_y < selected_info.height
                    ):
                        return None
                    return local_x, local_y

                return PointerRoute(child["input"], child_mapper)
            return PointerRoute(input_sender, map_pointer)

        pointer_router = PointerRouter(resolve_pointer)
        blank_cursor = Gdk.Cursor.new_from_name("none", None)
        window.set_cursor(blank_cursor)
        overlay.set_cursor(blank_cursor)
        view.set_cursor(blank_cursor)
        locator = CompositorPointerLocator(app_id=app_id)

        def locate_pointer() -> tuple[float, float] | None:
            located = locator.locate()
            if located is not None:
                return located
            # Not Hyprland (or the query failed): fall back to what GDK last
            # heard from the compositor for this surface.
            try:
                surface = window.get_surface()
                pointer = display.get_default_seat().get_pointer()
                found, x, y, _mask = surface.get_device_position(pointer)
            except Exception:  # noqa: BLE001
                return None
            if not found or not (0 <= x < overlay.get_width() and 0 <= y < overlay.get_height()):
                return None
            return float(x), float(y)

        resync = PointerResync(locate_pointer)
        debug_resync = os.environ.get("WAYSEAM_DEBUG_RESYNC") == "1"
        wheel = WheelAccumulator()
        controller = Gtk.EventControllerLegacy()
        controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)

        def raw_event(controller, supplied_event) -> bool:
            event = supplied_event or controller.get_current_event()
            if event is None:
                return False
            event_type = event.get_event_type()
            positioned, x, y = event.get_position()
            if not positioned:
                return False
            if event_type == Gdk.EventType.MOTION_NOTIFY:
                if resync.is_stale(x, y):
                    return True
                resync.track(x, y)
                pointer_router.motion(x, y)
                return True
            if event_type == Gdk.EventType.ENTER_NOTIFY:
                resync.track(x, y)
                return False
            if event_type == Gdk.EventType.LEAVE_NOTIFY:
                resync.leave()
                return False
            if event_type == Gdk.EventType.SCROLL:
                if event.is_stop():
                    wheel.reset()
                    return True
                direction = event.get_direction()
                if direction == Gdk.ScrollDirection.SMOOTH:
                    dx, dy = event.get_deltas()
                    # Wheels report whole notches even on the smooth path;
                    # touchpads report surface pixels that need scaling.
                    discrete = event.get_unit() != Gdk.ScrollUnit.SURFACE
                    delta_x, delta_y = wheel.consume(dx, dy, discrete=discrete)
                else:
                    dx = dy = 0.0
                    if direction == Gdk.ScrollDirection.UP:
                        dy = -1.0
                    elif direction == Gdk.ScrollDirection.DOWN:
                        dy = 1.0
                    elif direction == Gdk.ScrollDirection.LEFT:
                        dx = -1.0
                    elif direction == Gdk.ScrollDirection.RIGHT:
                        dx = 1.0
                    delta_x, delta_y = wheel.consume(dx, dy, discrete=True)
                pointer_router.scroll(x, y, delta_x, delta_y)
                return True
            if event_type not in {
                Gdk.EventType.BUTTON_PRESS,
                Gdk.EventType.BUTTON_RELEASE,
            }:
                return False
            button = int(event.get_button())
            if button not in (1, 2, 3):
                return False
            pointer_router.button(
                x,
                y,
                button,
                pressed=event_type == Gdk.EventType.BUTTON_PRESS,
            )
            if event_type == Gdk.EventType.BUTTON_PRESS:
                overlay.grab_focus()
            return True

        controller.connect("event", raw_event)
        overlay.add_controller(controller)

        # GTK4's legacy controller does not surface SCROLL events on Wayland;
        # a dedicated scroll controller is the supported path. It reports
        # deltas only, so pair them with the compositor-tracked pointer.
        scroll_controller = Gtk.EventControllerScroll()
        scroll_controller.set_flags(Gtk.EventControllerScrollFlags.BOTH_AXES)
        scroll_controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)

        def scrolled(ctrl, dx, dy) -> bool:
            event = ctrl.get_current_event()
            discrete = True
            if event is not None:
                try:
                    discrete = event.get_unit() != Gdk.ScrollUnit.SURFACE
                except Exception:  # noqa: BLE001 - not a scroll event
                    discrete = True
            delta_x, delta_y = wheel.consume(dx, dy, discrete=discrete)
            if not (delta_x or delta_y):
                return True
            # focus-follows-mouse=0 delivers scrolls to the focused window even
            # while the pointer hovers another one; route by what is actually
            # under the cursor so each window scrolls itself.
            under = locator.locate_wayseam_window()
            if under is not None:
                target_class, target_pid, local_x, local_y = under
                if target_class != app_id:
                    target_hwnd = presenter_hwnd_for_pid(target_pid)
                    if target_hwnd:
                        submitted = input_sender.shm_input is not None and (
                            input_sender.shm_input.wheel(
                                target_hwnd, int(local_x), int(local_y),
                                delta_x, delta_y,
                            )
                        )
                        if not submitted:
                            try:
                                client.window_pointer_input(
                                    target_hwnd, x=int(local_x), y=int(local_y),
                                    action="wheel", delta_x=delta_x, delta_y=delta_y,
                                )
                            except Exception:  # noqa: BLE001
                                pass
                    return True
                pointer_router.scroll(local_x, local_y, delta_x, delta_y)
                return True
            pointer = resync.pointer
            if pointer is None:
                return True  # pointer is not over any Wayseam window: drop
            pointer_router.scroll(pointer[0], pointer[1], delta_x, delta_y)
            return True

        scroll_controller.connect("scroll", scrolled)
        overlay.add_controller(scroll_controller)

        key_controller = Gtk.EventControllerKey()
        key_controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)

        def key_pressed(_controller, keyval, _keycode, state) -> bool:
            if state & Gdk.ModifierType.SUPER_MASK or keyval in _SUPER_KEYVALS:
                return False
            event = translate_keyboard_event(
                int(keyval),
                int(Gdk.keyval_to_unicode(keyval)),
                pressed=True,
            )
            if event is None:
                return False
            keyboard_sender.submit(event)
            return True

        def key_released(_controller, keyval, _keycode, state) -> None:
            if state & Gdk.ModifierType.SUPER_MASK or keyval in _SUPER_KEYVALS:
                return
            event = translate_keyboard_event(
                int(keyval),
                int(Gdk.keyval_to_unicode(keyval)),
                pressed=False,
            )
            if event is not None:
                keyboard_sender.submit(event)

        key_controller.connect("key-pressed", key_pressed)
        key_controller.connect("key-released", key_released)
        window.add_controller(key_controller)

        def read_host_clipboard(deliver: Callable[[str | None], None]) -> None:
            def finished(source, result) -> None:
                try:
                    deliver(source.read_text_finish(result))
                except Exception:  # noqa: BLE001 - non-text or empty selections
                    deliver(None)

            try:
                display.get_clipboard().read_text_async(None, finished)
            except Exception:  # noqa: BLE001
                deliver(None)

        def write_host_clipboard(text: str) -> None:
            def apply() -> bool:
                display.get_clipboard().set(text)
                return GLib.SOURCE_REMOVE

            GLib.idle_add(apply)

        clipboard = ClipboardSync(
            client, read_host=read_host_clipboard, write_host=write_host_clipboard,
        )

        def pull_guest_clipboard() -> bool:
            threading.Thread(
                target=clipboard.pull_guest, name="wayseam-clipboard", daemon=True,
            ).start()
            return GLib.SOURCE_CONTINUE

        clipboard_poll = {"source": 0}

        def active_changed(active_window, _parameter) -> None:
            if active_window.is_active():
                overlay.grab_focus()
                clipboard.push_host()
                if not clipboard_poll["source"]:
                    clipboard_poll["source"] = GLib.timeout_add(
                        1000, pull_guest_clipboard,
                    )
            else:
                keyboard_sender.release_all()
                wheel.reset()
                # Only drop the in-frame cursor when the pointer also left
                # the surface; under focus-follows-mouse=0 the user can hover
                # an unfocused window and still needs to see the cursor.
                if resync.pointer is None:
                    cursor_layer.set_cursor_overlay(None, 0, 0, 0, 0)
                    state["cursor_visible"] = False
                if clipboard_poll["source"]:
                    GLib.source_remove(clipboard_poll["source"])
                    clipboard_poll["source"] = 0
                pull_guest_clipboard()

        window.connect("notify::is-active", active_changed)

        def add_owned(info: WayseamWindow, root: dict[str, int]) -> None:
            child_picture = Gtk.Picture()
            child_picture.set_can_shrink(True)
            child_picture.set_can_target(False)
            child_picture.set_content_fit(Gtk.ContentFit.SCALE_DOWN)
            child_picture.set_size_request(info.width, info.height)
            child_pump = LatestFramePump(
                client,
                info.hwnd,
                max_fps=max_fps,
                popup_alpha=True,
            )
            child_pump.set_frame_callback(schedule_presentation)
            child_input = InputSender(client, info.hwnd)
            child_picture.set_halign(Gtk.Align.START)
            child_picture.set_valign(Gtk.Align.START)
            child_picture.set_margin_start(max(0, info.left - root["left"]))
            child_picture.set_margin_top(max(0, info.top - root["top"]))
            child_picture.set_cursor(blank_cursor)
            overlay.add_overlay(child_picture)
            overlay.set_clip_overlay(child_picture, False)
            # GTK4 has no reorder for overlays: re-add the cursor layer so it
            # stays above the newest owned picture.
            overlay.remove_overlay(cursor_layer)
            overlay.add_overlay(cursor_layer)
            overlay.set_clip_overlay(cursor_layer, False)
            state["owned"][info.hwnd] = {
                "picture": child_picture,
                "pump": child_pump,
                "input": child_input,
                "info": info,
            }
            child_pump.start()
            child_input.start()

        def sync_owned(tree: WindowTree) -> None:
            state["root"] = tree.root
            transient = tuple(
                item
                for item in tree.owned
                if not independent_owned_window(item, tree.root)
            )
            visible = {item.hwnd for item in transient}
            for child_hwnd in set(state["owned"]) - visible:
                child = state["owned"].pop(child_hwnd)
                child["pump"].stop()
                child["input"].stop()
                overlay.remove_overlay(child["picture"])
            for info in reversed(transient):
                child = state["owned"].get(info.hwnd)
                if child is None:
                    add_owned(info, tree.root)
                    continue
                child["info"] = info
                child["picture"].set_size_request(info.width, info.height)
                child["picture"].set_margin_start(max(0, info.left - tree.root["left"]))
                child["picture"].set_margin_top(max(0, info.top - tree.root["top"]))

        def tick() -> bool:
            if pump.window_gone.is_set():
                # The Windows title-bar close button destroys the guest HWND
                # before GTK receives a close request. Retire the matching host
                # surface without sending a redundant close to a dead handle.
                forward_close["enabled"] = False
                window.close()
                return GLib.SOURCE_REMOVE
            # The compositor may tile the new surface smaller than its initial
            # guest frame. The content allocation, not GTK's requested window
            # size, is authoritative for the HWND resize and input mapping.
            allocation = (overlay.get_width(), overlay.get_height())
            now = time.monotonic()
            tile_origin = state.get("tile_origin")
            if now >= state.get("tile_origin_at", 0.0):
                state["tile_origin_at"] = now + 0.4
                found = locator.locate_own_tile(app_id)
                if not state.get("tile_logged"):
                    state["tile_logged"] = True
                    print(
                        f"wayseam: first tile lookup app_id={app_id!r} -> {found}",
                        file=sys.stderr,
                    )
                if found is not None and found != tile_origin:
                    state["tile_origin"] = tile_origin = found
                    print(f"wayseam: tile origin -> {found}", file=sys.stderr)
                    resync.invalidate()
                    # A pure move changes no allocation; re-place the guest
                    # window at the new tile position with the same size.
                    if allocation == state["allocation"]:
                        resize_sender.submit(*allocation, origin=tile_origin)
            if allocation != state["allocation"]:
                state["allocation"] = allocation
                resize_sender.submit(*allocation, origin=tile_origin)
            frame = pump.take_latest()
            if frame is not None:
                present_frame(frame)
                state["frame_size"] = view.size
                if not state["configured"]:
                    window.set_default_size(*view.size)
                    state["root_width"], state["root_height"] = view.size
                    state["configured"] = True
                state["sequence"] = frame.sequence
                if state["title"] != "ok":
                    window.set_title(f"{title} · Wayseam")
                    state["title"] = "ok"
            elif pump.error:
                if state["title"] != pump.error:
                    window.set_title(f"{title} · {pump.error}")
                    state["title"] = pump.error
            elif state["title"] != "ok":
                # A transient agent hiccup must not leave an error in the
                # title once fetches succeed again, even with no new frame.
                window.set_title(f"{title} · Wayseam")
                state["title"] = "ok"

            tree = tree_pump.take_latest()
            if tree is not None:
                sync_owned(tree)
            for child in state["owned"].values():
                child_frame = child["pump"].take_latest()
                if child_frame is None:
                    continue
                child["picture"].set_paintable(
                    texture_from_surface(child_frame.surface),
                )

            root = state["root"]
            geometry = (allocation, (root["width"], root["height"]), state["frame_size"])
            resync.busy = bool(pointer_router._pressed)
            replay = resync.poll(geometry)
            if replay is not None:
                mapped = map_pointer(*replay)
                if debug_resync:
                    print(
                        f"resync geometry={geometry} pointer={replay} mapped={mapped}",
                        file=sys.stderr, flush=True,
                    )
                if mapped is not None:
                    pointer_router.motion(*replay)

            cursor_sample = cursor_pump.take_latest()
            if debug_resync and state.get("dbg_tick", 0) % 60 == 0:
                print(
                    f"cursordbg sample={cursor_sample is not None} "
                    f"pointer={resync.pointer} frame={state['frame_size']}",
                    file=sys.stderr, flush=True,
                )
            state["dbg_tick"] = state.get("dbg_tick", 0) + 1
            if cursor_sample is not None:
                cursor = cursor_sample.cursor
                if debug_resync and state["dbg_tick"] % 30 == 1:
                    print(
                        f"cursordbg visible={cursor.visible} pos=({cursor.x},{cursor.y}) "
                        f"tex={state.get('cursor_texture') is not None}",
                        file=sys.stderr, flush=True,
                    )
                if cursor.visible:
                    digest = hashlib.blake2s(cursor.png, digest_size=16).digest()
                    if digest != state["cursor_digest"]:
                        state["cursor_texture"] = Gdk.Texture.new_from_bytes(
                            GLib.Bytes.new(cursor.png),
                        )
                        state["cursor_hot"] = (cursor.hot_x, cursor.hot_y)
                        state["cursor_digest"] = digest
                    texture = state.get("cursor_texture")
                    frame_w, frame_h = state["frame_size"]
                    # Show the cursor whenever the host pointer is over this
                    # surface (ENTER/LEAVE tracked by PointerResync) — focus is
                    # the wrong gate under focus-follows-mouse=0, where the
                    # user hovers unfocused windows and we blank the
                    # compositor cursor. Bounds still guard stale positions.
                    inside = (
                        resync.pointer is not None
                        and 0 <= cursor.x < frame_w
                        and 0 <= cursor.y < frame_h
                    )
                    if debug_resync and state["dbg_tick"] % 30 == 2:
                        print(
                            f"cursordbg branch tex={texture is not None} inside={inside}",
                            file=sys.stderr, flush=True,
                        )
                    if texture is not None and inside:
                        if debug_resync and state["dbg_tick"] % 30 == 3:
                            print("cursordbg OVERLAY-SET", file=sys.stderr, flush=True)
                        hot_x, hot_y = state["cursor_hot"]
                        # cursor.x / cursor.y are the pointer in root-frame
                        # pixels captured with the frame; drawing the cursor
                        # here puts its hotspot exactly on the ink point,
                        # regardless of cursor shape or focus changes.
                        cursor_layer.set_cursor_overlay(
                            texture, cursor.x, cursor.y, hot_x, hot_y,
                        )
                        state["cursor_visible"] = True
                    elif state.get("cursor_visible"):
                        # The pointer moved onto another window; let that
                        # window's own cursor show instead of a stale overlay.
                        cursor_layer.set_cursor_overlay(None, 0, 0, 0, 0)
                        state["cursor_visible"] = False
                elif state.get("cursor_visible"):
                    cursor_layer.set_cursor_overlay(None, 0, 0, 0, 0)
                    state["cursor_visible"] = False
            return True

        def close(*_args) -> bool:
            if forward_close["enabled"]:
                try:
                    client.window_close(hwnd)
                except Exception as exc:
                    window.set_title(f"{title} · close failed: {exc}")
                    return True
            pump.stop()
            input_sender.stop()
            keyboard_sender.stop()
            tree_pump.stop()
            cursor_pump.stop()
            resize_sender.stop()
            for child in state["owned"].values():
                child["pump"].stop()
                child["input"].stop()
            return False

        presentation_lock = threading.Lock()
        presentation_pending = {"value": False}

        def run_scheduled_presentation() -> bool:
            with presentation_lock:
                presentation_pending["value"] = False
            tick()
            return GLib.SOURCE_REMOVE

        def schedule_presentation() -> None:
            with presentation_lock:
                if presentation_pending["value"]:
                    return
                presentation_pending["value"] = True
            GLib.idle_add(run_scheduled_presentation)

        pump.set_frame_callback(schedule_presentation)
        window.connect("close-request", close)
        GLib.timeout_add(16, tick)
        sync_owned(WindowTree(root=initial_root, owned=tuple(initial_owned)))
        window.present()
        overlay.grab_focus()
        pump.start()
        input_sender.start()
        keyboard_sender.start()
        tree_pump.start()
        cursor_pump.start()
        resize_sender.start()

    app.connect("activate", activate)

    def stop_from_signal() -> bool:
        forward_close["enabled"] = False
        app.quit()
        return GLib.SOURCE_REMOVE

    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, stop_from_signal)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, stop_from_signal)
    shell_lease.acquire()
    try:
        return int(app.run(None))
    finally:
        shell_lease.release()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Present a Windows HWND as a Wayseam Window")
    parser.add_argument("--hwnd", required=True, type=lambda value: int(value, 0))
    parser.add_argument("--title", default="Windows application")
    parser.add_argument("--max-fps", type=int, default=60)
    parser.add_argument("--app-id", default="org.wayseam.Window")
    parser.add_argument("--icon-name", default="wayseam")
    args = parser.parse_args(argv)
    if args.hwnd <= 0:
        parser.error("--hwnd must be positive")
    return run_window(
        args.hwnd,
        args.title,
        args.max_fps,
        app_id=args.app_id,
        icon_name=args.icon_name,
    )


if __name__ == "__main__":
    raise SystemExit(main())
