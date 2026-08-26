# SPDX-License-Identifier: MIT

from __future__ import annotations

from wayseam.apps import AppInfo
from wayseam.guest.agent import WayseamTopLevel
from wayseam.present.watch import (
    _source_environment,
    match_window_to_app,
    missing_presentations,
)


def _window(
    path: str,
    *,
    hwnd: int = 0xD03DE,
    class_name: str = "Chrome_WidgetWin_1",
) -> WayseamTopLevel:
    return WayseamTopLevel(
        hwnd=hwnd,
        pid=4400,
        process_path=path,
        title="Browser",
        class_name=class_name,
        left=100,
        top=100,
        width=1200,
        height=800,
    )


def _app(**overrides) -> AppInfo:
    values = {
        "name": "microsoft-edge",
        "full_name": "Microsoft Edge",
        "executable": r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        "presentation": "wayseam",
    }
    values.update(overrides)
    return AppInfo(**values)


def test_match_window_to_app_uses_case_insensitive_executable_identity() -> None:
    app = _app()
    window = _window(
        r"c:\PROGRAM FILES (X86)\Microsoft\Edge\Application\MSEDGE.EXE"
    )

    assert match_window_to_app(window, [app]) is app


def test_match_window_to_packaged_app_accepts_executable_under_package_root() -> None:
    app = _app(
        name="affinity",
        executable=(
            r"C:\Program Files\WindowsApps\Canva.Affinity_3.2.3.4646_x64__"
            "8a0j1tnjnt4a4"
        ),
        launch_uri="Canva.Affinity_8a0j1tnjnt4a4!Canva.Affinity",
    )
    window = _window(app.executable + r"\Affinity.exe")

    assert match_window_to_app(window, [app]) is app


def test_missing_presentations_ignores_presented_and_explicit_rail_windows() -> None:
    wayseam = _app()
    rail = _app(
        name="legacy-browser",
        executable=r"C:\Legacy\browser.exe",
        presentation="rail",
    )
    already = _window(wayseam.executable, hwnd=0x100)
    missing = _window(wayseam.executable, hwnd=0x200)
    legacy = _window(rail.executable, hwnd=0x300)

    assert missing_presentations(
        [already, missing, legacy],
        presented_hwnds={already.hwnd},
        apps=[wayseam, rail],
    ) == [(missing, wayseam)]


def test_watcher_preserves_wayland_session_but_not_unrelated_secrets(monkeypatch) -> None:
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-test")
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/tmp/test-bus")
    monkeypatch.setenv("GDK_BACKEND", "wayland,x11")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-leak")

    env = _source_environment()

    assert env["WAYLAND_DISPLAY"] == "wayland-test"
    assert env["DISPLAY"] == ":99"
    assert env["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/tmp/test-bus"
    assert env["GDK_BACKEND"] == "wayland,x11"
    assert "UNRELATED_SECRET" not in env


def test_missing_presentations_never_turns_explorer_shell_surfaces_into_windows() -> None:
    explorer = _app(
        name="file-explorer",
        executable=r"C:\Windows\explorer.exe",
    )
    desktop = _window(explorer.executable, hwnd=0x10104, class_name="Progman")
    shell_host = _window(
        explorer.executable,
        hwnd=0x900AE,
        class_name="ApplicationFrameWindow",
    )
    folder = _window(
        explorer.executable,
        hwnd=0x98030A,
        class_name="CabinetWClass",
    )

    assert missing_presentations(
        [desktop, shell_host, folder],
        presented_hwnds=set(),
        apps=[explorer],
    ) == [(folder, explorer)]


def test_presentation_backoff_holds_fresh_spawns_and_backs_off_failures():
    from wayseam.present.watch import PresentationBackoff

    now = {"t": 100.0}
    backoff = PresentationBackoff(clock=lambda: now["t"])
    assert backoff.ready(0x10)
    backoff.started(0x10)
    assert not backoff.ready(0x10)  # still initializing: no duplicate presenter
    now["t"] += 3.0
    assert backoff.ready(0x10)

    # Failures back off exponentially (2, 4, 8, 16 s ...) then give up.
    for attempt, delay in enumerate((2.0, 4.0, 8.0, 16.0), start=1):
        backoff.failed(0x20)
        assert backoff.failures(0x20) == attempt
        assert not backoff.ready(0x20)
        now["t"] += delay
        assert backoff.ready(0x20)
    backoff.failed(0x20)
    now["t"] += 1000.0
    assert not backoff.ready(0x20)  # fifth failure: stop retrying

    # Once the guest window is gone, its history is dropped.
    backoff.forget_missing({0x10})
    assert backoff.ready(0x20)


def test_watcher_records_presenter_outcomes_in_backoff():
    import inspect

    from wayseam.present.watch import run_watcher

    source = inspect.getsource(run_watcher)
    assert "backoff = PresentationBackoff()" in source
    assert "if backoff.ready(item[0].hwnd)" in source
    assert "backoff.failed(window.hwnd)" in source
    assert "backoff.started(window.hwnd)" in source
    assert "backoff.forget_missing(" in source
