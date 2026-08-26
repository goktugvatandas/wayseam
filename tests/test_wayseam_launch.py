# SPDX-License-Identifier: MIT

from __future__ import annotations

import base64
import inspect
from types import SimpleNamespace

import pytest

from wayseam.apps import AppInfo
from wayseam.guest.agent import ExecResult
from wayseam.present.launch import (
    _guest_launch_script,
    _launch_guest_window,
    application_id,
    launch_wayseam_app,
    present_wayseam_window,
)


def _app(**overrides) -> AppInfo:
    values = {
        "name": "grok-bot",
        "full_name": "Grok Bot",
        "executable": "C:\\Programs\\Grok Bot.exe",
        "args": "--safe",
        "presentation": "wayseam",
    }
    values.update(overrides)
    return AppInfo(**values)


def test_application_id_is_stable_and_dbus_safe() -> None:
    assert application_id("grok-bot") == "org.wayseam.App.grok_bot"
    assert application_id("3d-viewer") == "org.wayseam.App.app_3d_viewer"


def test_guest_launch_script_never_interpolates_guest_strings_as_source() -> None:
    app = _app(
        executable="C:\\bad'; Remove-Item C:\\Users -Recurse; '.exe",
        args='--name "danger"; Stop-Process -Name explorer',
    )

    script = _guest_launch_script(app, None)

    assert app.executable not in script
    assert app.args not in script
    assert base64.b64encode(app.executable.encode()).decode() in script
    assert base64.b64encode(app.args.encode()).decode() in script


def test_guest_launch_script_activates_packaged_app_by_aumid() -> None:
    app = _app(
        executable=(
            "C:\\Program Files\\WindowsApps\\Canva.Affinity_3.2.3.4646_x64__"
            "8a0j1tnjnt4a4"
        ),
        args="",
        launch_uri="Canva.Affinity_8a0j1tnjnt4a4!Canva.Affinity",
    )

    script = _guest_launch_script(app, None)

    encoded_aumid = base64.b64encode(app.launch_uri.encode()).decode()
    assert encoded_aumid in script
    assert "shell:AppsFolder\\$aumid" in script
    assert "Get-AppxPackage" in script
    assert "PackageFamilyName -ieq $family" in script
    assert "EnumWindows" in script
    assert "$stableCount -ge $requiredStable" in script


def test_launch_guest_window_validates_and_returns_metadata() -> None:
    client = SimpleNamespace(
        exec=lambda script, timeout: ExecResult(
            0,
            '{"pid":3440,"hwnd":1245564,"title":"Grok Bot"}\n',
            "",
        )
    )

    metadata = _launch_guest_window(client, _app(), None)

    assert metadata == {"pid": 3440, "hwnd": 1245564, "title": "Grok Bot"}


def test_wayseam_launch_ensures_one_shared_audio_bridge() -> None:
    source = inspect.getsource(present_wayseam_window)

    assert "ensure_audio_bridge()" in source


def test_wayseam_launch_ensures_automatic_window_watcher() -> None:
    source = inspect.getsource(launch_wayseam_app)

    assert "ensure_window_watcher()" in source


def test_wayseam_presenters_default_to_sixty_fps() -> None:
    assert inspect.signature(launch_wayseam_app).parameters["max_fps"].default == 60
    assert inspect.signature(present_wayseam_window).parameters["max_fps"].default == 60


def test_claim_hwnd_is_exclusive_while_a_spawn_is_in_flight(tmp_path) -> None:
    from wayseam.present.launch import WindowAlreadyPresented, claim_hwnd

    claim = claim_hwnd(tmp_path, 0x303F0)
    assert claim.exists() and claim.read_text() == ""
    # A second spawner (the watcher racing ``wayseam app run``) must back off
    # even before the first has written the presenter PID.
    with pytest.raises(WindowAlreadyPresented):
        claim_hwnd(tmp_path, 0x303F0)


def test_claim_hwnd_refuses_while_presenter_pid_is_alive(tmp_path) -> None:
    import os

    from wayseam.present.launch import WindowAlreadyPresented, claim_hwnd

    claim = claim_hwnd(tmp_path, 0x20090)
    claim.write_text(f"{os.getpid()}\n")
    with pytest.raises(WindowAlreadyPresented):
        claim_hwnd(tmp_path, 0x20090)


def test_claim_hwnd_reclaims_dead_presenter(tmp_path, monkeypatch) -> None:
    from wayseam.present import launch

    claim = launch.claim_hwnd(tmp_path, 0x1234)
    claim.write_text("999999999\n")
    monkeypatch.setattr(launch, "_pid_alive", lambda pid: False)
    assert launch.claim_hwnd(tmp_path, 0x1234) == claim
    assert claim.read_text() == ""


def test_claim_hwnd_reclaims_stale_empty_claim(tmp_path) -> None:
    import os
    import time

    from wayseam.present.launch import claim_hwnd

    claim = claim_hwnd(tmp_path, 0xABC)
    old = time.time() - 120
    os.utime(claim, (old, old))
    assert claim_hwnd(tmp_path, 0xABC) == claim


def test_present_and_watcher_both_tolerate_already_presented() -> None:
    from wayseam.present import watch

    assert "claim = claim_hwnd(rd, hwnd)" in inspect.getsource(present_wayseam_window)
    assert "except WindowAlreadyPresented" in inspect.getsource(launch_wayseam_app)
    assert "except WindowAlreadyPresented" in inspect.getsource(watch.run_watcher)
