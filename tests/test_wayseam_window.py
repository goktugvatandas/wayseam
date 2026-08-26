# SPDX-License-Identifier: MIT
from __future__ import annotations

import inspect
import threading
import time

from wayseam.guest.agent import AgentWindowGoneError, WayseamFrame, WayseamWindow
from wayseam.present.window import (
    WHEEL_DELTA,
    ClipboardSync,
    CompositorPointerLocator,
    Frame,
    InputSender,
    KeyboardEvent,
    KeyboardSender,
    LatestFramePump,
    PointerEvent,
    PointerResync,
    PointerRoute,
    PointerRouter,
    ResizeSender,
    ShellVisibilityLease,
    WheelAccumulator,
    fitted_geometry,
    independent_owned_window,
    make_surface_view_class,
    map_synced_pointer,
    run_window,
    translate_keyboard_event,
)
from wayseam.present.window import (
    main as window_main,
)


class _Frames:
    def __init__(self, values):
        self.values = iter(values)

    def window_frame_bgra(self, _hwnd, **_kwargs):
        try:
            return next(self.values)
        except StopIteration:
            return WayseamFrame(1, 1, 4, b"last", b"last", 3, True)


def test_frame_pump_drops_duplicates_and_exposes_only_latest():
    first = WayseamFrame(1, 1, 4, b"one!", b"first", 1, True)
    second = WayseamFrame(1, 1, 4, b"two!", b"second", 2, True)
    pump = LatestFramePump(_Frames([first, first, second]), 0x123, max_fps=120)
    pump.start()
    deadline = time.monotonic() + 1
    frame = None
    while time.monotonic() < deadline:
        candidate = pump.take_latest()
        if candidate is not None and candidate.surface == second:
            frame = candidate
            break
        time.sleep(0.01)
    pump.stop()

    assert frame is not None
    assert frame.surface == second
    assert frame.sequence == 2
    assert pump.take_latest() is None


def test_frame_pump_notifies_presentation_loop_on_changed_frame():
    notified = threading.Event()
    frame = WayseamFrame(1, 1, 4, b"one!", b"first", 1, True)
    pump = LatestFramePump(_Frames([frame]), 0x123, max_fps=120)
    pump.set_frame_callback(notified.set)

    pump.start()
    assert notified.wait(timeout=1)
    pump.stop()


def test_wayseam_window_defaults_to_sixty_fps():
    source = inspect.getsource(window_main)
    assert 'parser.add_argument("--max-fps", type=int, default=60)' in source


class _GoneFrames:
    def window_frame_bgra(self, _hwnd, **_kwargs):
        raise AgentWindowGoneError("guest window is closed")


def test_frame_pump_distinguishes_closed_window_from_transient_capture_errors():
    pump = LatestFramePump(_GoneFrames(), 0x123, max_fps=120)
    pump.start()

    assert pump.window_gone.wait(timeout=1)

    pump.stop()


class _ResizeClient:
    def __init__(self, results=None):
        self.calls = []
        self.results = iter(results or [])

    def window_resize(self, hwnd, *, width, height, origin=None):
        self.calls.append((hwnd, width, height))
        self.origins = getattr(self, "origins", [])
        self.origins.append(origin)
        try:
            return next(self.results)
        except StopIteration:
            return width, height


def test_resize_sender_debounces_to_latest_compositor_size():
    client = _ResizeClient()
    sender = ResizeSender(client, 0x123)
    sender.start()
    sender.submit(900, 500)
    sender.submit(1000, 600)
    sender.submit(1100, 700)
    deadline = time.monotonic() + 1
    while not client.calls and time.monotonic() < deadline:
        time.sleep(0.01)
    sender.stop()

    assert client.calls == [(0x123, 1100, 700)]


def test_resize_sender_retries_when_guest_clamps_the_requested_size():
    client = _ResizeClient(results=[(1000, 580), (1000, 600)])
    sender = ResizeSender(client, 0x123, retry_delay=0.02)
    sender.start()
    sender.submit(1000, 600)
    deadline = time.monotonic() + 1
    while len(client.calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    sender.stop()

    assert client.calls == [(0x123, 1000, 600), (0x123, 1000, 600)]


def test_full_size_owned_window_is_independent_but_popup_is_not():
    root = {"left": 0, "top": 0, "width": 1300, "height": 820}
    full_size = WayseamWindow(
        hwnd=0x701AE,
        owner=0x9001E,
        title="Welcome",
        class_name="HwndWrapper",
        left=0,
        top=0,
        width=1300,
        height=820,
    )
    popup = WayseamWindow(
        hwnd=0x801AE,
        owner=0x9001E,
        title="Brushes",
        class_name="Popup",
        left=900,
        top=120,
        width=240,
        height=400,
    )

    assert independent_owned_window(full_size, root)
    assert not independent_owned_window(popup, root)


def test_pointer_mapping_waits_for_resized_guest_and_frame_to_converge():
    assert map_synced_pointer(
        400,
        250,
        host_size=(1901, 1048),
        guest_size=(941, 1048),
        frame_size=(941, 1048),
    ) is None
    assert map_synced_pointer(
        400,
        250,
        host_size=(1901, 1048),
        guest_size=(1901, 1048),
        frame_size=(941, 1048),
    ) is None
    assert map_synced_pointer(
        400,
        250,
        host_size=(1901, 1048),
        guest_size=(1901, 1048),
        frame_size=(1901, 1048),
    ) == (400, 250)


def test_pointer_mapping_tracks_scaled_guest_when_app_rejects_resize():
    assert map_synced_pointer(
        473,
        257,
        host_size=(946, 514),
        guest_size=(854, 539),
        frame_size=(854, 539),
    ) == (427, 269)
    assert map_synced_pointer(
        20,
        257,
        host_size=(946, 514),
        guest_size=(854, 539),
        frame_size=(854, 539),
    ) is None


class _InputClient:
    def __init__(self):
        self.calls = []

    def window_pointer_input(self, hwnd, **kwargs):
        self.calls.append((hwnd, kwargs))


def test_input_sender_keeps_motion_between_drag_boundaries():
    client = _InputClient()
    sender = InputSender(client, 0x123)
    sender.submit(PointerEvent(10, 10, "down", 1))
    sender.submit(PointerEvent(20, 20, "move"))
    sender.submit(PointerEvent(30, 30, "move"))
    sender.submit(PointerEvent(30, 30, "up", 1))
    sender.start()
    deadline = time.monotonic() + 1
    while len(client.calls) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    sender.stop()

    assert [call[1]["action"] for call in client.calls] == ["down", "move", "up"]
    assert client.calls[1][1]["x"] == 30
    assert client.calls[1][1]["y"] == 30


def test_input_sender_survives_idle_wait_timeout():
    sender = InputSender(_InputClient(), 0x123)
    sender.start()
    time.sleep(0.15)

    assert sender._thread.is_alive()
    sender.stop()


def test_input_sender_repairs_recorded_duplicate_boundaries():
    client = _InputClient()
    sender = InputSender(client, 0x123)
    sender.submit(PointerEvent(10, 10, "down", 1))
    sender.submit(PointerEvent(20, 20, "down", 1))
    sender.submit(PointerEvent(30, 30, "move"))
    sender.submit(PointerEvent(30, 30, "up", 1))
    sender.submit(PointerEvent(40, 40, "up", 1))
    sender.start()
    deadline = time.monotonic() + 1
    while len(client.calls) < 5 and time.monotonic() < deadline:
        time.sleep(0.01)
    sender.stop()

    assert [call[1]["action"] for call in client.calls] == [
        "down", "up", "down", "move", "up",
    ]


def test_input_sender_releases_guest_button_when_stopped_mid_drag():
    client = _InputClient()
    sender = InputSender(client, 0x123)
    sender.submit(PointerEvent(10, 10, "down", 1))
    sender.start()
    deadline = time.monotonic() + 1
    while not client.calls and time.monotonic() < deadline:
        time.sleep(0.01)
    sender.stop()

    assert [call[1]["action"] for call in client.calls] == ["down", "up"]


class _KeyboardClient:
    def __init__(self):
        self.calls = []

    def window_keyboard_input(self, hwnd, **kwargs):
        self.calls.append((hwnd, kwargs))


def test_keyboard_translation_preserves_virtual_key_boundaries_and_unicode():
    assert translate_keyboard_event(ord("k"), ord("k"), pressed=True) == KeyboardEvent(
        "down", 0x4B,
    )
    assert translate_keyboard_event(ord("k"), ord("k"), pressed=False) == KeyboardEvent(
        "up", 0x4B,
    )
    assert translate_keyboard_event(0xFF53, 0, pressed=True) == KeyboardEvent(
        "down", 0x27, 0, True,
    )
    assert translate_keyboard_event(0x010000E7, 0xE7, pressed=True) == KeyboardEvent(
        "press", 0, 0xE7,
    )
    assert translate_keyboard_event(0x010000E7, 0xE7, pressed=False) is None


def test_keyboard_translation_never_forwards_super():
    assert translate_keyboard_event(0xFFEB, 0, pressed=True) is None
    assert translate_keyboard_event(0xFFEC, 0, pressed=False) is None


def test_keyboard_sender_releases_held_keys_on_blur():
    client = _KeyboardClient()
    sender = KeyboardSender(client, 0x123)
    sender.start()
    sender.submit(KeyboardEvent("down", 0x11))
    sender.submit(KeyboardEvent("down", 0x43))
    sender.submit(KeyboardEvent("up", 0x43))
    sender.release_all()
    deadline = time.monotonic() + 1
    while len(client.calls) < 4 and time.monotonic() < deadline:
        time.sleep(0.01)
    sender.stop()

    assert [call[1]["action"] for call in client.calls] == ["down", "down", "up", "up"]
    assert client.calls[-1][1]["virtual_key"] == 0x11


class _ShellClient:
    def __init__(self):
        self.visible = []

    def window_shell_visibility(self, *, visible):
        self.visible.append(visible)
        return 1


def test_shell_visibility_lease_restores_only_after_last_presenter(tmp_path):
    client = _ShellClient()
    alive = {101, 202}
    first = ShellVisibilityLease(
        client, directory=tmp_path, pid=101, process_alive=lambda pid: pid in alive,
    )
    second = ShellVisibilityLease(
        client, directory=tmp_path, pid=202, process_alive=lambda pid: pid in alive,
    )

    first.acquire()
    second.acquire()
    first.release()
    second.release()

    assert client.visible == [False, False, True]
    assert not list(tmp_path.glob("*.lease"))


class _RecordedSender:
    def __init__(self):
        self.events = []

    def submit(self, event):
        self.events.append(event)


def test_pointer_router_replays_recorded_physical_drag_with_press_boundary():
    sender = _RecordedSender()
    route = PointerRoute(
        sender,
        lambda x, y: (int(x), int(y)) if x >= 0 and y >= 0 else None,
    )
    router = PointerRouter(lambda _x, _y: route)

    router.button(100, 200, 1, pressed=True)
    router.motion(110, 205)
    router.motion(120, 215)
    router.button(130, 225, 1, pressed=False)

    assert [event.action for event in sender.events] == ["down", "move", "move", "up"]
    assert sender.events[0] == PointerEvent(100, 200, "down", 1)
    assert sender.events[-1] == PointerEvent(130, 225, "up", 1)


def test_pointer_router_releases_at_last_valid_point_when_pointer_leaves_surface():
    sender = _RecordedSender()
    route = PointerRoute(
        sender,
        lambda x, y: (int(x), int(y)) if x < 50 and y < 50 else None,
    )
    router = PointerRouter(lambda _x, _y: route)

    router.button(10, 10, 1, pressed=True)
    router.motion(30, 30)
    router.button(100, 100, 1, pressed=False)

    assert sender.events[-1] == PointerEvent(30, 30, "up", 1)


def test_window_uses_one_raw_top_level_controller_for_all_pointer_boundaries():
    source = inspect.getsource(run_window)

    assert "Gtk.EventControllerLegacy" in source
    assert "Gtk.PropagationPhase.CAPTURE" in source
    assert "controller.get_current_event()" in source
    assert "view.set_can_target(False)" in source
    assert "Gtk.GestureDrag" not in source
    assert "Gtk.GestureClick" not in source
    assert "allocation = (overlay.get_width(), overlay.get_height())" in source
    assert 'picture.set_size_request(initial_root["width"]' not in source


def test_guest_cursor_is_composited_in_frame_and_compositor_cursor_hidden():
    source = inspect.getsource(run_window)

    # The compositor cursor is blanked over our surfaces; the guest cursor is
    # drawn into the frame at its captured pointer position so its hotspot
    # lands on the ink for every cursor shape and survives focus changes.
    assert 'Gdk.Cursor.new_from_name("none", None)' in source
    assert "window.set_cursor(blank_cursor)" in source
    assert "view.set_cursor(blank_cursor)" in source
    # The cursor lives on a dedicated topmost layer, not inside the frame:
    # owned-window pictures (Affinity confirmation dialogs) are stacked
    # above the frame and would otherwise hide it.
    assert "cursor_layer = SurfaceView.CursorLayer(view)" in source
    assert "cursor_layer.set_cursor_overlay(" in source
    assert "overlay.remove_overlay(cursor_layer)" in source  # re-raised per owned
    assert "texture, cursor.x, cursor.y, hot_x, hot_y" in source
    # Do not draw a stale overlay while the pointer is over another window,
    # and drop it entirely when the window loses focus so it never looks
    # stuck on the edge.
    assert "resync.pointer is not None" in source  # pointer-over, not focus
    assert "0 <= cursor.x < frame_w" in source and "0 <= cursor.y < frame_h" in source
    assert source.count("cursor_layer.set_cursor_overlay(None, 0, 0, 0, 0)") >= 2


class _OverlayWidget:
    def __init__(self):
        self.draws = 0

    def set_overflow(self, value):
        pass

    def queue_draw(self):
        self.draws += 1

    def queue_resize(self):
        pass

    def get_width(self):
        return 100

    def get_height(self):
        return 100


class _OverlayGtk:
    Widget = _OverlayWidget

    class Overflow:
        HIDDEN = "h"

    class Orientation:
        HORIZONTAL = 0
        VERTICAL = 1


class _OverlayGraphene:
    class Point:
        def init(self, x, y):
            self.x, self.y = x, y
            return self

    class Rect:
        def init(self, x, y, w, h):
            self.x, self.y, self.w, self.h = x, y, w, h
            return self


class _OverlaySnapshot:
    def __init__(self):
        self.ops = []

    def save(self):
        pass

    def restore(self):
        pass

    def translate(self, point):
        pass

    def scale(self, x, y):
        pass

    def append_texture(self, texture, rect):
        self.ops.append((texture.name, rect.x, rect.y))


class _OverlayTexture:
    def __init__(self, name):
        self.name = name

    def get_width(self):
        return 14

    def get_height(self):
        return 14


def test_surface_view_draws_cursor_overlay_after_base_and_patches():
    SurfaceView = make_surface_view_class(_OverlayGtk, None, None, _OverlayGraphene)
    view = SurfaceView()
    view.set_full(_OverlayTexture("base"))
    view.set_cursor_overlay(_OverlayTexture("cursor"), 40, 50, 7, 7)
    snap = _OverlaySnapshot()
    view.do_snapshot(snap)
    # base first, cursor last with its hotspot subtracted from the position.
    assert snap.ops[0][0] == "base"
    assert snap.ops[-1] == ("cursor", 33, 43)
    # Clearing the overlay removes it from the next snapshot.
    view.set_cursor_overlay(None, 0, 0, 0, 0)
    snap2 = _OverlaySnapshot()
    view.do_snapshot(snap2)
    assert all(op[0] != "cursor" for op in snap2.ops)


def test_window_installs_keyboard_forwarding_without_stealing_super():
    source = inspect.getsource(run_window)

    assert "Gtk.EventControllerKey" in source
    assert 'key_controller.connect("key-pressed"' in source
    assert 'key_controller.connect("key-released"' in source
    assert "Gdk.ModifierType.SUPER_MASK" in source
    assert "keyboard_sender.submit" in source
    assert "keyboard_sender.release_all" in source


def test_window_holds_reversible_shell_visibility_lease():
    source = inspect.getsource(run_window)

    assert "ShellVisibilityLease" in source


def test_host_close_forwards_to_guest_but_signal_restart_does_not():
    source = inspect.getsource(run_window)

    assert "client.window_close(hwnd)" in source
    assert 'forward_close["enabled"] = False' in source
    assert "shell_lease.acquire()" in source
    assert "shell_lease.release()" in source
    assert "GLib.unix_signal_add" in source
    assert "pump.window_gone.is_set()" in source


def test_wheel_accumulator_converts_gdk_deltas_to_windows_notches():
    wheel = WheelAccumulator()
    # One discrete notch down in GDK is one WHEEL_DELTA *down* on Windows.
    assert wheel.consume(0.0, 1.0, discrete=True) == (0, -WHEEL_DELTA)
    assert wheel.consume(-1.0, 0.0, discrete=True) == (-WHEEL_DELTA, 0)
    # Smooth touchpad deltas are surface pixels (40 px per notch) and carry
    # fractional remainders until a whole WHEEL_DELTA unit accumulates.
    assert wheel.consume(0.0, -10.0, discrete=False) == (0, 30)
    assert wheel.consume(0.0, -10.0, discrete=False) == (0, 30)
    wheel.reset()
    assert wheel.consume(0.0, 0.0, discrete=False) == (0, 0)
    # Runaway bursts are clamped to the agent's 100-notch bound.
    assert wheel.consume(0.0, -500.0, discrete=True) == (0, 100 * WHEEL_DELTA)


class _WheelClient:
    def __init__(self):
        self.events = []

    def window_pointer_input(self, hwnd, **kwargs):
        self.events.append((hwnd, kwargs))


def test_input_sender_folds_wheel_bursts_but_keeps_button_boundaries():
    client = _WheelClient()
    sender = InputSender(client, 0x77)
    sender.submit(PointerEvent(5, 6, "down", 1))
    sender.submit(PointerEvent(5, 6, "wheel", 0, 0, -120))
    sender.submit(PointerEvent(5, 7, "wheel", 0, 0, -120))
    sender.submit(PointerEvent(5, 7, "up", 1))
    sender.start()
    deadline = time.monotonic() + 2
    while len(client.events) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    sender.stop()

    assert [event[1]["action"] for event in client.events] == ["down", "wheel", "up"]
    assert client.events[1][1] == {
        "x": 5, "y": 7, "action": "wheel", "delta_x": 0, "delta_y": -240,
    }


def test_pointer_router_scrolls_hwnd_under_pointer_or_drag_target():
    recorded = _RecordedSender()
    other = _RecordedSender()
    routes = {
        "recorded": PointerRoute(recorded, lambda x, y: (int(x), int(y))),
        "other": PointerRoute(other, lambda x, y: (int(x) + 100, int(y))),
    }
    router = PointerRouter(
        lambda x, y: routes["other"] if x >= 50 else routes["recorded"],
    )

    router.scroll(10, 10, 0, -120)
    router.button(10, 10, 1, pressed=True)
    router.scroll(60, 10, 0, 120)  # drag continues; wheel follows the pressed HWND
    router.button(60, 10, 1, pressed=False)
    router.scroll(60, 10, 0, 0)  # zero deltas never reach the guest

    assert [(e.action, e.delta_y) for e in recorded.events] == [
        ("wheel", -120), ("down", 0), ("wheel", 120), ("up", 0),
    ]
    assert other.events == []


class _ClipboardClient:
    def __init__(self, guest_text="", sequence=7):
        self.guest_text = guest_text
        self.sequence = sequence
        self.set_calls = []
        self.get_calls = []

    def clipboard_set(self, text):
        self.set_calls.append(text)
        self.guest_text = text
        self.sequence += 1
        return self.sequence

    def clipboard_get(self, *, since=None):
        from wayseam.guest.agent import WayseamClipboard

        self.get_calls.append(since)
        if since == self.sequence:
            return WayseamClipboard(self.sequence, False)
        return WayseamClipboard(self.sequence, True, self.guest_text)


def test_clipboard_sync_pushes_host_text_once_and_mirrors_guest_changes():
    client = _ClipboardClient()
    host = {"text": "from linux", "written": []}
    sync = ClipboardSync(
        client,
        read_host=lambda deliver: deliver(host["text"]),
        write_host=host["written"].append,
    )

    sync.push_host()
    sync.push_host()  # unchanged host text is not re-sent
    assert client.set_calls == ["from linux"]

    # Our own write is not echoed back to the host.
    assert sync.pull_guest() is False
    assert client.get_calls[-1] == client.sequence

    # Text copied inside the Windows app reaches the host exactly once.
    client.guest_text = "from windows"
    client.sequence += 1
    assert sync.pull_guest() is True
    assert sync.pull_guest() is False
    assert host["written"] == ["from windows"]

    # Guest text mirrored to the host is not pushed back into the guest.
    host["text"] = "from windows"
    sync.push_host()
    assert client.set_calls == ["from linux"]


def test_clipboard_sync_ignores_empty_host_text_and_survives_agent_errors():
    class _Broken:
        def clipboard_set(self, text):
            raise RuntimeError("agent down")

        def clipboard_get(self, *, since=None):
            raise RuntimeError("agent down")

    sync = ClipboardSync(
        _Broken(), read_host=lambda deliver: deliver(""), write_host=lambda t: None,
    )
    sync.push_host()
    assert sync.error is None
    sync = ClipboardSync(
        _Broken(), read_host=lambda deliver: deliver("x"), write_host=lambda t: None,
    )
    sync.push_host()
    assert sync.error and "agent down" in sync.error
    assert sync.pull_guest() is False


def test_window_forwards_scroll_and_syncs_clipboard_on_focus_changes():
    source = inspect.getsource(run_window)
    assert "Gdk.EventType.SCROLL" in source
    assert "pointer_router.scroll(" in source
    assert "clipboard.push_host()" in source
    assert "pull_guest_clipboard()" in source


def _delta(width, height, damage, pixels, sequence):
    return WayseamFrame(
        width, height, width * 4, pixels, bytes([sequence]), sequence, True, 0,
        damage, b"\xff" * (damage[2] * damage[3] * 4) if damage else b"",
    )


def test_frame_pump_accumulates_damage_between_presentations():
    canvas = bytearray(16 * 16 * 4)
    frames = [
        _delta(16, 16, None, canvas, 1),
        _delta(16, 16, (0, 0, 2, 2), canvas, 2),
        _delta(16, 16, (4, 4, 1, 1), canvas, 3),
    ]
    pump = LatestFramePump(_Frames(frames), 0x1, max_fps=120)
    pump.start()
    deadline = time.monotonic() + 2
    while pump._sequence < 3 and time.monotonic() < deadline:
        time.sleep(0.005)
    pump.stop()

    # The first frame is a full surface; everything after it is patches.
    latest = pump.take_latest()
    assert latest is not None and latest.damage is None
    pump._pending_damage = []
    pump._pending_bytes = 0
    pump._record_damage(frames[1])
    pump._record_damage(frames[2])
    pump._latest = Frame(frames[2], 9, 0.0)
    latest = pump.take_latest()
    assert latest.damage == (
        ((0, 0, 2, 2), b"\xff" * 16),
        ((4, 4, 1, 1), b"\xff" * 4),
    )
    # Taking clears the accumulator for the next presentation.
    pump._latest = Frame(frames[2], 10, 0.0)
    assert pump.take_latest().damage == ()


def test_frame_pump_falls_back_to_full_surface_when_damage_is_large():
    canvas = bytearray(16 * 16 * 4)
    pump = LatestFramePump(_Frames([]), 0x1)
    pump._pending_damage = []
    for sequence in range(LatestFramePump.MAX_PATCHES + 1):
        pump._record_damage(_delta(16, 16, (0, 0, 1, 1), canvas, sequence))
    assert pump._pending_damage is None
    pump._pending_damage = []
    pump._record_damage(_delta(16, 16, (0, 0, 16, 9), canvas, 1))
    assert pump._pending_damage is None  # more than half the surface
    pump._pending_damage = []
    pump._record_damage(_delta(16, 16, None, canvas, 2))
    assert pump._pending_damage is None  # a full frame always resets


def test_fitted_geometry_matches_pointer_mapping():
    assert fitted_geometry((100, 100), (50, 50)) == (1.0, 25.0, 25.0)
    assert fitted_geometry((50, 100), (100, 100)) == (0.5, 0.0, 25.0)
    assert fitted_geometry((0, 100), (100, 100)) == (1.0, 0.0, 0.0)
    assert map_synced_pointer(
        25, 25, host_size=(100, 100), guest_size=(50, 50), frame_size=(50, 50),
    ) == (0, 0)


class _Snapshot:
    def __init__(self):
        self.ops = []

    def save(self): self.ops.append("save")
    def restore(self): self.ops.append("restore")
    def translate(self, point): self.ops.append(("translate", point.x, point.y))
    def scale(self, x, y): self.ops.append(("scale", x, y))
    def append_texture(self, texture, rect):
        self.ops.append(("texture", texture.name, rect.x, rect.y, rect.width, rect.height))


class _Texture:
    def __init__(self, name, width, height):
        self.name, self.width, self.height = name, width, height

    def get_width(self): return self.width
    def get_height(self): return self.height


class _Point:
    def init(self, x, y):
        self.x, self.y = x, y
        return self


class _Rect:
    def init(self, x, y, width, height):
        self.x, self.y, self.width, self.height = x, y, width, height
        return self


def test_surface_view_composites_patches_over_base_with_pointer_geometry():
    class _Widget:
        def __init__(self):
            self.draws = 0
            self.resizes = 0
            self.overflow = None

        def set_overflow(self, value): self.overflow = value
        def queue_draw(self): self.draws += 1
        def queue_resize(self): self.resizes += 1
        def get_width(self): return 50
        def get_height(self): return 100

    class _Gtk:
        Widget = _Widget
        class Overflow:
            HIDDEN = "hidden"
        class Orientation:
            HORIZONTAL = 0
            VERTICAL = 1

    class _Graphene:
        Point = _Point
        Rect = _Rect

    SurfaceView = make_surface_view_class(_Gtk, None, None, _Graphene)
    view = SurfaceView()
    snapshot = _Snapshot()
    view.do_snapshot(snapshot)
    assert snapshot.ops == []  # nothing before the first full frame
    view.add_patch(1, 1, _Texture("early", 1, 1))
    assert view.patch_count == 0

    view.set_full(_Texture("base", 100, 100))
    view.add_patch(10, 20, _Texture("patch", 4, 2))
    assert view.size == (100, 100) and view.resizes == 1 and view.draws == 2
    assert view.do_measure(_Gtk.Orientation.HORIZONTAL, -1) == (0, 100, -1, -1)

    view.do_snapshot(snapshot)
    assert snapshot.ops == [
        "save",
        ("translate", 0.0, 25.0),
        ("scale", 0.5, 0.5),
        ("texture", "base", 0, 0, 100, 100),
        ("texture", "patch", 10, 20, 4, 2),
        "restore",
    ]
    # A new base drops the stacked patches.
    view.set_full(_Texture("base2", 100, 100))
    assert view.patch_count == 0 and view.resizes == 1


def test_window_uploads_only_damage_patches_between_full_frames():
    source = inspect.getsource(run_window)
    assert "in_place" not in source  # the pump owns the canvas, not the window
    assert "present_frame(frame)" in source
    assert "view.add_patch(" in source
    assert "Gtk.Picture()" not in source.split("def add_owned")[0]
    # A bytearray canvas must never reach GLib.Bytes directly: PyGObject
    # marshals it element by element (hundreds of ms per large frame).
    assert "if not isinstance(pixels, bytes):" in source
    assert "pixels = bytes(pixels)" in source


def test_pointer_resync_reconciles_immediately_on_geometry_change():
    now = {"t": 10.0}
    located = {"value": (382.0, 357.0)}
    resync = PointerResync(lambda: located["value"], clock=lambda: now["t"])
    resync.track(370.0, 350.0)
    settled = ((900, 600), (900, 600), (900, 600))
    assert resync.poll(settled) is None  # first geometry: nothing to replay yet
    changed = ((700, 600), (900, 600), (900, 600))
    # Surface changed under a still pointer: replay the compositor position now.
    assert resync.poll(changed) == (382.0, 357.0)
    # GTK's synthetic replay of the pre-change position is swallowed briefly.
    assert resync.is_stale(370.0, 350.0)
    assert not resync.is_stale(371.0, 350.0)


def test_pointer_resync_self_heals_periodically_while_pointer_is_at_rest():
    now = {"t": 0.0}
    located = {"value": (100.0, 100.0)}
    resync = PointerResync(lambda: located["value"], clock=lambda: now["t"])
    geom = ((900, 600), (900, 600), (900, 600))
    resync.poll(geom)              # establish geometry
    now["t"] = 1.0
    resync.track(100.0, 100.0)     # a real motion; guest already correct here
    assert resync.poll(geom) is None            # actively moving: no reconcile
    now["t"] = 1.0 + PointerResync.REST_DELAY
    # At rest and the compositor still reports the same spot: nothing to send.
    assert resync.poll(geom) is None
    # The surface drifted the pointer elsewhere without an event; heal it.
    located["value"] = (250.0, 300.0)
    now["t"] += PointerResync.PERIODIC_INTERVAL
    assert resync.poll(geom) == (250.0, 300.0)
    now["t"] += PointerResync.PERIODIC_INTERVAL
    assert resync.poll(geom) is None            # already reconciled; idle


def test_pointer_resync_never_fights_a_drag_or_a_pointer_that_left():
    now = {"t": 0.0}
    located = {"value": (5.0, 6.0)}
    resync = PointerResync(lambda: located["value"], clock=lambda: now["t"])
    resync.track(100.0, 100.0)
    now["t"] = 1.0
    resync.busy = True            # a button is held: a drag is in progress
    assert resync.poll(("a",)) is None          # geometry change ignored while busy
    resync.busy = False
    resync.leave()               # pointer left the surface...
    located["value"] = None      # ...and the compositor confirms it is elsewhere
    now["t"] += 1.0
    assert resync.poll(("a",)) is None          # nothing to reconcile when absent


def test_window_replays_pointer_after_resize_or_mode_change():
    source = inspect.getsource(run_window)
    assert "resync = PointerResync(locate_pointer)" in source
    assert "Gdk.EventType.ENTER_NOTIFY" in source and "resync.track(x, y)" in source
    assert "Gdk.EventType.LEAVE_NOTIFY" in source and "resync.leave()" in source
    assert '(allocation, (root["width"], root["height"]), state["frame_size"])' in source
    assert "pointer_router.motion(*replay)" in source
    assert "if resync.is_stale(x, y):" in source
    assert "resync.busy = bool(pointer_router._pressed)" in source
    assert "replay = resync.poll(geometry)" in source


def test_compositor_pointer_locator_uses_window_origin_from_hyprland():
    import json

    replies = {
        "j/cursorpos": json.dumps({"x": 1142, "y": 562}),
        "j/clients": json.dumps([
            {"pid": 999, "class": "other", "at": [0, 0], "size": [3840, 2160]},
            {"pid": 4242, "class": "org.wayseam.App.paint", "at": [10, 10], "size": [100, 100]},
            {
                "pid": 4242, "class": "org.wayseam.App.affinity",
                "at": [772, 38], "size": [740, 1048],
            },
        ]),
    }
    locator = CompositorPointerLocator(
        pid=4242, app_id="org.wayseam.App.affinity", query=lambda req: replies[req].encode(),
    )
    assert locator.locate() == (370.0, 524.0)

    replies["j/cursorpos"] = json.dumps({"x": 5, "y": 5})  # outside our window
    assert locator.locate() is None

    def broken(_req):
        raise OSError("no hyprland")

    assert CompositorPointerLocator(pid=1, query=broken).locate() is None


def test_pointer_resync_prefers_compositor_position_on_geometry_change():
    located = {"value": (5.0, 6.0)}
    resync = PointerResync(lambda: located["value"])
    resync.track(100.0, 100.0)
    resync.poll(("first",))  # establish geometry
    # The surface moved under a still pointer: the tracked (100, 100) is stale.
    assert resync.poll(("a",)) == (5.0, 6.0)
    assert resync.pointer == (5.0, 6.0)
    located["value"] = None  # compositor unavailable: fall back to tracked events
    resync.track(7.0, 8.0)
    assert resync.poll(("c",)) == (7.0, 8.0)


class _FakeShmSource:
    def __init__(self, results):
        self.results = list(results)
        self.closed = False
        self.polls = 0

    def poll(self, previous):
        self.polls += 1
        if self.results:
            return self.results.pop(0)
        return None

    def close(self):
        self.closed = True


class _ShmClient:
    def __init__(self):
        self.assigns = 0
        self.releases = 0
        self.full_fetches = 0

    def shm_assign(self, hwnd):
        self.assigns += 1

    def shm_release(self, hwnd):
        self.releases += 1

    def window_frame_bgra(self, hwnd, **kwargs):
        self.full_fetches += 1
        return WayseamFrame(2, 2, 8, b"\x01" * 16, b"full", 9, True, 16)


def _wait(predicate, seconds=2.0):
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.005)
    return predicate()


def test_frame_pump_prefers_shared_memory_and_publishes_ring_frames():

    frames = [
        WayseamFrame(2, 2, 8, bytearray(16), b"a", 1, True, 16),
        WayseamFrame(2, 2, 8, bytearray(16), b"b", 2, True, 16, (0, 0, 1, 1), b"\xff" * 4),
    ]
    client = _ShmClient()
    source = _FakeShmSource(frames)
    pump = LatestFramePump(client, 0x1, max_fps=60, shm=source)
    assert pump.transport == "shm"
    pump.start()
    assert _wait(lambda: pump._sequence >= 2)
    pump.stop()

    latest = pump.take_latest()
    assert latest is not None and latest.sequence == 2
    assert client.full_fetches == 0  # no HTTP on the hot path
    assert source.closed and client.releases == 1  # slot released on stop


def test_frame_pump_fetches_too_large_updates_over_http_once():
    from wayseam.present.shm import ShmTooLarge

    client = _ShmClient()
    source = _FakeShmSource([ShmTooLarge(4)])
    pump = LatestFramePump(client, 0x1, max_fps=60, shm=source)
    pump.start()
    assert _wait(lambda: client.full_fetches >= 1)
    pump.stop()
    latest = pump.take_latest()
    assert latest is not None and latest.surface.wire_digest == b"full"
    assert client.full_fetches == 1


def test_frame_pump_falls_back_to_http_when_the_ring_breaks():
    class _BrokenSource:
        def __init__(self):
            self.closed = False

        def poll(self, previous):
            raise RuntimeError("ring gone")

        def close(self):
            self.closed = True

    class _HttpAfterFallback(_ShmClient):
        def __init__(self):
            super().__init__()
            self.http_polls = 0

        def window_frame_bgra(self, hwnd, **kwargs):
            self.http_polls += 1
            return WayseamFrame(
                1, 1, 4, b"\x00" * 4, b"h%d" % self.http_polls, self.http_polls, True, 4,
            )

    client = _HttpAfterFallback()
    pump = LatestFramePump(client, 0x1, max_fps=120, shm=_BrokenSource())
    pump.SHM_POLL_SECONDS = 0.0005
    pump.start()
    assert _wait(lambda: pump.transport == "http" and client.http_polls >= 1, seconds=3.0)
    pump.stop()
    assert pump.shm is None  # ring abandoned


def test_frame_pump_keepalive_reassigns_the_slot_periodically():
    client = _ShmClient()
    pump = LatestFramePump(client, 0x1, max_fps=60, shm=_FakeShmSource([]))
    pump.SHM_KEEPALIVE_SECONDS = 0.05
    pump.start()
    assert _wait(lambda: client.assigns >= 2)
    pump.stop()


def test_run_window_assigns_a_ring_slot_with_http_fallback():
    source = inspect.getsource(run_window)
    assert "client.shm_assign(hwnd)" in source
    assert "ShmFrameSource(default_ring_path(cfg), slot.slot)" in source
    assert "shm transport unavailable" in source  # graceful HTTP fallback message
    assert "LatestFramePump(client, hwnd, max_fps=max_fps, shm=shm_source)" in source


def test_shm_pump_retires_window_when_keepalive_assign_is_rejected():
    from wayseam.guest.agent import AgentError

    class _GoneClient(_ShmClient):
        def shm_assign(self, hwnd):
            raise AgentError("/wayseam/shm rejected request: HTTP 400")

    pump = LatestFramePump(_GoneClient(), 0x1, max_fps=60, shm=_FakeShmSource([]))
    pump.SHM_KEEPALIVE_SECONDS = 0.01
    pump.start()
    assert _wait(lambda: pump.window_gone.is_set(), seconds=2.0)
    pump.stop()


class _RecordingWriter:
    def __init__(self, ok=True):
        self.ok = ok
        self.moves = []
        self.wheels = []
        self.keys = []
        self.unicodes = []
        self.flushes = 0

    def move(self, hwnd, x, y):
        self.moves.append((hwnd, x, y))
        return self.ok

    def wheel(self, hwnd, x, y, dx, dy):
        self.wheels.append((x, y, dx, dy))
        return self.ok

    def key(self, hwnd, vk, *, down, extended):
        self.keys.append((vk, down, extended))
        return self.ok

    def unicode(self, hwnd, cp):
        self.unicodes.append(cp)
        return self.ok

    def flush(self, timeout=0.02):
        self.flushes += 1
        return True


def test_input_sender_prefers_the_ring_for_motion_and_falls_back_for_clicks():
    client = _InputClient()
    writer = _RecordingWriter(ok=True)
    sender = InputSender(client, 0x55, writer)
    sender.submit(PointerEvent(4, 5, "move"))
    sender.submit(PointerEvent(4, 5, "down", 1))  # click -> HTTP + flush first
    sender.start()
    deadline = time.monotonic() + 2
    while not client.calls and time.monotonic() < deadline:
        time.sleep(0.01)
    sender.stop()

    assert writer.moves == [(0x55, 4, 5)]  # motion went to the ring
    assert writer.flushes >= 1  # flushed before the click
    assert any(kw["action"] == "down" for _h, kw in client.calls)  # click over HTTP


def test_input_sender_falls_back_to_http_when_ring_write_fails():
    client = _InputClient()
    writer = _RecordingWriter(ok=False)  # ring full / consumer gone
    sender = InputSender(client, 0x55, writer)
    sender.submit(PointerEvent(7, 8, "move"))
    sender.start()
    deadline = time.monotonic() + 2
    while not client.calls and time.monotonic() < deadline:
        time.sleep(0.01)
    sender.stop()
    assert client.calls[0][1]["action"] == "move"  # HTTP carried the motion


def test_keyboard_sender_prefers_the_ring():
    client = _KeyboardClient()
    writer = _RecordingWriter(ok=True)
    sender = KeyboardSender(client, 0x55, writer)
    sender.submit(KeyboardEvent("down", virtual_key=0x41))
    sender.submit(KeyboardEvent("press", unicode=0x131))
    sender.start()
    deadline = time.monotonic() + 2
    while len(writer.keys) + len(writer.unicodes) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    sender.stop()  # releases the held 0x41 on stop -> a second ring key event
    assert (0x41, True, False) in writer.keys
    assert writer.unicodes == [0x131]
    assert client.calls == []  # nothing hit HTTP


def test_pointer_resync_adopts_compositor_position_without_any_events():
    # follow_mouse=0: hovering an unfocused window produces no GTK events at
    # all; the locator alone must drive both the replay and pointer presence.
    now = {"t": 0.0}
    located = {"value": None}
    resync = PointerResync(lambda: located["value"], clock=lambda: now["t"])
    geom = ("g",)
    resync.poll(geom)
    now["t"] = 1.0
    assert resync.poll(geom) is None  # pointer elsewhere: nothing to do
    located["value"] = (40.0, 50.0)   # user hovers the unfocused window
    now["t"] += PointerResync.PERIODIC_INTERVAL
    assert resync.poll(geom) == (40.0, 50.0)
    assert resync.pointer == (40.0, 50.0)  # cursor overlay may show now
    located["value"] = None           # pointer left again
    now["t"] += PointerResync.PERIODIC_INTERVAL
    assert resync.poll(geom) is None
    assert resync.pointer is None     # cursor overlay hides again
