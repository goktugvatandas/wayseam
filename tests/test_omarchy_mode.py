# SPDX-License-Identifier: MIT
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from wayseam.omarchy import mode as omarchy_mode
from wayseam.omarchy.mode import presenter_pids, switch_mode, watcher_pid
from wayseam.omarchy.state import get_mode, set_mode
from wayseam.paths import runtime_dir


def _fake_proc(tmp_path: Path, processes: dict[int, list[str]]) -> Path:
    proc = tmp_path / "proc"
    for pid, argv in processes.items():
        (proc / str(pid)).mkdir(parents=True)
        (proc / str(pid) / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    (proc / "self").mkdir()
    return proc


class _Recorder:
    def __init__(self) -> None:
        self.terminated: list[int] = []
        self.launched: list[object] = []
        self.watcher_calls = 0

    def terminate(self, pid: int) -> bool:
        self.terminated.append(pid)
        return True

    def launch(self, cfg: object) -> object:
        self.launched.append(cfg)
        return SimpleNamespace(process=None)

    def ensure_watcher(self) -> bool:
        self.watcher_calls += 1
        return True


def test_presenter_pids_requires_exact_module_token(tmp_path: Path) -> None:
    proc = _fake_proc(
        tmp_path,
        {
            101: ["/usr/bin/python", "-m", "wayseam.present.window", "--hwnd", "0x1a"],
            102: ["/usr/bin/python", "-m", "wayseam.present.window.helper"],
            103: ["vim", "src/wayseam/wayseam/window.py"],
            104: ["/usr/bin/python", "-m", "wayseam.present.watch"],
            105: ["python", "wayseam.present.window"],
        },
    )

    assert presenter_pids(proc) == [101, 105]


def test_watcher_pid_verifies_cmdline_of_pid_file(tmp_path: Path) -> None:
    proc = _fake_proc(
        tmp_path,
        {
            200: ["/usr/bin/python", "-m", "wayseam.present.watch"],
            201: ["sleep", "infinity"],
        },
    )
    pid_file = runtime_dir() / "wayseam-watch.pid"
    pid_file.parent.mkdir(parents=True, exist_ok=True)

    assert watcher_pid(proc) is None
    pid_file.write_text("201\n", encoding="ascii")
    assert watcher_pid(proc) is None, "recycled pid must not be signalled"
    pid_file.write_text("200\n", encoding="ascii")
    assert watcher_pid(proc) == 200
    pid_file.write_text("garbage", encoding="ascii")
    assert watcher_pid(proc) is None


def test_switch_to_desktop_stops_presenters_and_watcher_then_opens_desktop() -> None:
    rec = _Recorder()
    cfg = object()

    result = switch_mode(
        "desktop",
        cfg=cfg,
        terminate=rec.terminate,
        presenters=lambda: [11, 12],
        watcher=lambda: 7,
        desktop_pid=lambda: None,
        launch_desktop=rec.launch,
        agent_factory=lambda cfg: pytest.fail("agent must not be used for desktop"),
        ensure_watcher=lambda: pytest.fail("watcher must not be started"),
    )

    assert get_mode() == "desktop"
    assert rec.terminated == [7, 11, 12], "watcher first so it cannot re-present"
    assert rec.launched == [cfg]
    assert result["presenters_terminated"] == 2
    assert result["watcher_terminated"] is True
    assert result["desktop_client"] == "started"
    assert result["changed"] is True
    assert result["warnings"] == []


def test_switch_to_desktop_reuses_running_desktop_client_and_is_idempotent() -> None:
    set_mode("desktop")
    rec = _Recorder()

    result = switch_mode(
        "desktop",
        cfg=object(),
        terminate=rec.terminate,
        presenters=lambda: [],
        watcher=lambda: None,
        desktop_pid=lambda: 4242,
        launch_desktop=rec.launch,
    )

    assert rec.launched == []
    assert rec.terminated == []
    assert result["desktop_client"] == "running"
    assert result["changed"] is False


def test_switch_to_desktop_reports_launch_failure_but_keeps_mode() -> None:
    def boom(cfg: object) -> object:
        raise RuntimeError("no freerdp")

    result = switch_mode(
        "desktop",
        cfg=object(),
        terminate=lambda pid: True,
        presenters=lambda: [],
        watcher=lambda: None,
        desktop_pid=lambda: None,
        launch_desktop=boom,
    )

    assert get_mode() == "desktop"
    assert result["desktop_client"] == "failed"
    assert result["warnings"] == ["desktop client launch failed: no freerdp"]


def test_switch_to_wayseam_disconnects_desktop_reattaches_console_and_starts_watcher() -> None:
    set_mode("desktop")
    rec = _Recorder()
    waited: list[tuple[int, float]] = []
    calls: list[str] = []

    class Agent:
        def __init__(self, cfg: object) -> None:
            calls.append("agent")

        def session_reconnect_console(self) -> dict[str, object]:
            calls.append("reconnect")
            return {"session_id": 1, "state": "active", "station": "Console"}

    def wait_exit(pid: int, timeout: float) -> bool:
        waited.append((pid, timeout))
        calls.append("wait")
        return True

    def ensure_watcher() -> bool:
        calls.append("watch")
        return True

    result = switch_mode(
        "wayseam",
        cfg=object(),
        terminate=lambda pid: calls.append(f"term:{pid}") or True,
        presenters=lambda: pytest.fail("presenters are not touched"),
        watcher=lambda: pytest.fail("watcher pid is not needed"),
        desktop_pid=lambda: 555,
        launch_desktop=rec.launch,
        agent_factory=Agent,
        ensure_watcher=ensure_watcher,
        wait_exit=wait_exit,
        exit_timeout=2.5,
    )

    assert get_mode() == "wayseam"
    assert calls == ["term:555", "wait", "agent", "reconnect", "watch"]
    assert waited == [(555, 2.5)]
    assert rec.launched == []
    assert result["desktop_client"] == "stopped"
    assert result["session"] == {"session_id": 1, "state": "active", "station": "Console"}
    assert result["watcher_started"] is True
    assert result["warnings"] == []


def test_switch_to_wayseam_tolerates_old_agent_without_reconnect() -> None:
    class OldAgent:
        def __init__(self, cfg: object) -> None:
            pass

    rec = _Recorder()
    result = switch_mode(
        "wayseam",
        cfg=object(),
        terminate=rec.terminate,
        desktop_pid=lambda: None,
        agent_factory=OldAgent,
        ensure_watcher=rec.ensure_watcher,
    )

    assert rec.terminated == []
    assert result["desktop_client"] == "unchanged"
    assert result["session"] is None
    assert result["warnings"] == ["guest agent does not support console reconnect"]
    assert rec.watcher_calls == 1


def test_switch_to_wayseam_collects_agent_and_watcher_failures() -> None:
    class Agent:
        def __init__(self, cfg: object) -> None:
            pass

        def session_reconnect_console(self) -> dict[str, object]:
            raise RuntimeError("agent offline")

    def watcher_fails() -> bool:
        raise OSError("no python")

    result = switch_mode(
        "wayseam",
        cfg=object(),
        terminate=lambda pid: True,
        desktop_pid=lambda: None,
        agent_factory=Agent,
        ensure_watcher=watcher_fails,
    )

    assert result["watcher_started"] is False
    assert result["warnings"] == [
        "console reconnect failed: agent offline",
        "window watcher failed to start: no python",
    ]


def test_switch_mode_rejects_unknown_target_without_persisting() -> None:
    with pytest.raises(ValueError):
        switch_mode("coherence", cfg=object())
    assert get_mode() == "wayseam"


def test_terminate_pid_ignores_dead_or_invalid_pids() -> None:
    assert omarchy_mode.terminate_pid(0) is False
    assert omarchy_mode.terminate_pid(-5) is False
    # A pid that cannot exist on Linux (above the default pid_max ceiling).
    assert omarchy_mode.terminate_pid(4_194_305) is False


def test_watcher_exits_quietly_outside_wayseam_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    from wayseam.present import watch

    set_mode("desktop")
    monkeypatch.setattr(watch, "AgentClient", lambda cfg: SimpleNamespace())
    monkeypatch.setattr(watch, "list_available_apps", list)
    monkeypatch.setattr(
        watch, "presented_hwnds", lambda: pytest.fail("watcher polled in Desktop Mode")
    )

    assert watch.run_watcher(poll_interval=0.0, idle_seconds=0.0) == 0


def test_watcher_keeps_running_in_wayseam_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    from wayseam.present import watch

    polls: list[int] = []

    def presented() -> set[int]:
        polls.append(1)
        return set()

    monkeypatch.setattr(watch, "AgentClient", lambda cfg: SimpleNamespace())
    monkeypatch.setattr(watch, "list_available_apps", list)
    monkeypatch.setattr(watch, "presented_hwnds", presented)

    assert watch.run_watcher(poll_interval=0.0, idle_seconds=0.0) == 0
    assert polls == [1], "wayseam mode reaches the presenter scan, then idles out"


def _run(app: str, file: str | None) -> int:
    from argparse import Namespace

    from wayseam.cli import _cmd_run

    return _cmd_run(Namespace(app=app, file=file, wait=False))


def test_run_app_in_desktop_mode_skips_presenter_and_launches_in_guest(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from wayseam import apps as core_app
    from wayseam import config
    from wayseam.desktop_mode import freerdp as rdp
    from wayseam.guest import agent as core_agent
    from wayseam.present import launch as wayseam_launch

    set_mode("desktop")
    cfg = object()
    info = SimpleNamespace(
        name="affinity",
        full_name="Affinity",
        executable="C:\\Affinity.exe",
        launch_uri="",
        wm_class_hint="",
        args="",
        icon_path="",
        rdp_overrides={},
        presentation="wayseam",
    )
    events: list[object] = []

    class FakeAgent:
        def __init__(self, got_cfg: object) -> None:
            events.append(("agent", got_cfg))

    monkeypatch.setattr(config.Config, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(core_app, "find_app", lambda name: info if name == "affinity" else None)
    monkeypatch.setattr(rdp, "_find_existing_session", lambda name: events.append(("find", name)))
    monkeypatch.setattr(rdp, "launch_desktop", lambda got_cfg: events.append(("desktop", got_cfg)))
    monkeypatch.setattr(core_agent, "AgentClient", FakeAgent)
    monkeypatch.setattr(
        wayseam_launch,
        "_launch_guest_window",
        lambda client, app, file: events.append(("guest", app.name, file))
        or {"hwnd": 1, "pid": 2, "title": "x"},
    )
    monkeypatch.setattr(
        wayseam_launch,
        "launch_wayseam_app",
        lambda *a, **k: pytest.fail("presenter must not start in Desktop Mode"),
    )
    assert _run("affinity", "/tmp/doc.afphoto") == 0
    assert events == [
        ("find", "desktop"),
        ("desktop", cfg),
        ("agent", cfg),
        ("guest", "affinity", "/tmp/doc.afphoto"),
    ]
    assert "Launching Affinity in Desktop Mode" in capsys.readouterr().out


def test_run_app_in_desktop_mode_reuses_running_desktop_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wayseam import apps as core_app
    from wayseam import config
    from wayseam.desktop_mode import freerdp as rdp
    from wayseam.guest import agent as core_agent
    from wayseam.present import launch as wayseam_launch

    set_mode("desktop")
    info = SimpleNamespace(name="paint", full_name="Paint", presentation="wayseam")
    monkeypatch.setattr(config.Config, "load", classmethod(lambda cls: object()))
    monkeypatch.setattr(core_app, "find_app", lambda name: info)
    monkeypatch.setattr(rdp, "_find_existing_session", lambda name: SimpleNamespace())
    monkeypatch.setattr(
        rdp, "launch_desktop", lambda cfg: pytest.fail("desktop client already running")
    )
    monkeypatch.setattr(core_agent, "AgentClient", lambda cfg: object())
    launched: list[str] = []
    monkeypatch.setattr(
        wayseam_launch, "_launch_guest_window", lambda c, app, f: launched.append(app.name)
    )
    assert _run("paint", None) == 0
    assert launched == ["paint"]


def test_run_app_in_wayseam_mode_still_uses_presenter(monkeypatch: pytest.MonkeyPatch) -> None:
    from wayseam import apps as core_app
    from wayseam import config
    from wayseam.desktop_mode import freerdp as rdp
    from wayseam.present import launch as wayseam_launch

    info = SimpleNamespace(name="paint", full_name="Paint", presentation="wayseam")
    monkeypatch.setattr(config.Config, "load", classmethod(lambda cls: object()))
    monkeypatch.setattr(core_app, "find_app", lambda name: info)
    monkeypatch.setattr(
        rdp, "launch_desktop", lambda cfg: pytest.fail("desktop client must not start")
    )
    presented: list[str] = []
    monkeypatch.setattr(
        wayseam_launch,
        "launch_wayseam_app",
        lambda cfg, app, *, file_path=None, wait=False: presented.append(app.name),
    )
    assert _run("paint", None) == 0
    assert presented == ["paint"]
