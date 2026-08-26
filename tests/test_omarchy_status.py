# SPDX-License-Identifier: MIT
from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

from wayseam.apps import AppInfo
from wayseam.guest.agent import WayseamTopLevel
from wayseam.omarchy.state import set_menu_placement, set_mode
from wayseam.omarchy.status import collect_state, vm_running


def _completed(stdout: str = "", returncode: int = 0, stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, returncode=returncode, stderr=stderr)


def _docker(status: str):
    def run(cmd, **kwargs):
        assert cmd[:2] == ["docker", "inspect"]
        assert cmd[-1] == "omarchy-windows"
        assert kwargs["timeout"] == 5.0
        return _completed(status + "\n")

    return run


def _window(hwnd: int, path: str) -> WayseamTopLevel:
    return WayseamTopLevel(
        hwnd=hwnd,
        pid=10,
        process_path=path,
        title="",
        class_name="X",
        left=0,
        top=0,
        width=800,
        height=600,
    )


class _Agent:
    def __init__(self, cfg: object, *, windows=None, session=None, health=None) -> None:
        self._windows = windows or []
        self._session = session
        self._health = health if health is not None else {"ok": True, "version": "1.2.3"}
        self.calls: list[str] = []

    def health(self) -> dict:
        self.calls.append("health")
        return self._health

    def top_level_windows(self) -> list[WayseamTopLevel]:
        self.calls.append("windows")
        return list(self._windows)

    def session_state(self) -> dict:
        self.calls.append("session")
        return self._session


def test_vm_running_tolerates_missing_docker_and_timeouts() -> None:
    def missing(cmd, **kwargs):
        raise FileNotFoundError("docker")

    def slow(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 5)

    assert vm_running(run=missing) == (False, "docker is not installed")
    assert vm_running(run=slow) == (False, "docker inspect timed out")
    assert vm_running(run=_docker("running")) == (True, None)
    assert vm_running(run=_docker("exited")) == (False, None)
    assert vm_running(run=lambda cmd, **k: _completed("", 1, "Error: No such object")) == (
        False,
        "Error: No such object",
    )


def test_collect_state_full_document_is_json_and_secret_free() -> None:
    set_mode("desktop")
    set_menu_placement("both")
    affinity = AppInfo(
        name="affinity",
        full_name="Affinity",
        executable="C:\\Program Files\\Affinity\\Affinity.exe",
        icon_path="/icons/affinity.png",
    )
    notepad = AppInfo(
        name="notepad", full_name="Notepad", executable="C:\\notepad.exe", hidden=True
    )
    agents: list[_Agent] = []

    def factory(cfg: object) -> _Agent:
        agent = _Agent(
            cfg,
            windows=[
                _window(0x10, affinity.executable),
                _window(0x20, notepad.executable),
            ],
            session={"session_id": 1, "state": "active", "station": "Console"},
        )
        agents.append(agent)
        return agent

    state = collect_state(
        cfg=object(),
        run=_docker("running"),
        agent_factory=factory,
        apps=[notepad, affinity],
        presented={0x10},
        desktop_running=lambda: True,
    )

    assert json.loads(json.dumps(state)) == state
    assert state == {
        "schema": 1,
        "mode": "desktop",
        "menu_placement": "both",
        "vm": {"running": True, "container": "omarchy-windows"},
        "agent": {"ok": True, "version": "1.2.3"},
        "session": {
            "desktop_client_running": True,
            "presenters": 1,
            "guest": {"session_id": 1, "state": "active", "station": "Console"},
        },
        "apps": [
            {
                "slug": "affinity",
                "name": "Affinity",
                "hidden": False,
                "icon": "/icons/affinity.png",
                "presented": True,
            },
            {"slug": "notepad", "name": "Notepad", "hidden": True, "icon": "", "presented": False},
        ],
        "errors": [],
    }
    assert agents[0].calls == ["health", "session", "windows"]


def test_collect_state_degrades_when_agent_and_docker_fail() -> None:
    def factory(cfg: object) -> object:
        raise ConnectionRefusedError("agent down")

    def no_docker(cmd, **kwargs):
        raise FileNotFoundError("docker")

    state = collect_state(
        cfg=object(),
        run=no_docker,
        agent_factory=factory,
        apps=[AppInfo(name="paint", full_name="Paint", executable="C:\\p.exe")],
        presented={0x1, 0x2},
        desktop_running=lambda: False,
    )

    assert state["vm"] == {"running": False, "container": "omarchy-windows"}
    assert state["agent"] == {"ok": False, "version": None}
    assert state["session"] == {"desktop_client_running": False, "presenters": 2, "guest": None}
    assert state["apps"][0]["presented"] is False
    assert state["errors"] == ["docker is not installed", "guest agent unavailable: agent down"]


def test_collect_state_skips_window_query_without_presenters_and_old_agents() -> None:
    class OldAgent:
        def __init__(self, cfg: object) -> None:
            self.calls: list[str] = []

        def health(self) -> dict:
            self.calls.append("health")
            return {"ok": True}

        def top_level_windows(self) -> list:
            raise AssertionError("no presenters, no window query")

    holder: list[OldAgent] = []

    def factory(cfg: object) -> OldAgent:
        agent = OldAgent(cfg)
        holder.append(agent)
        return agent

    state = collect_state(
        cfg=object(),
        run=_docker("running"),
        agent_factory=factory,
        apps=[],
        presented=set(),
        desktop_running=lambda: False,
    )

    assert state["agent"] == {"ok": True, "version": None}
    assert state["session"]["guest"] is None
    assert state["errors"] == []
    assert holder[0].calls == ["health"]


def test_collect_state_reports_window_list_failure_without_raising() -> None:
    class Agent(_Agent):
        def top_level_windows(self) -> list:
            raise RuntimeError("top-level unreachable")

    state = collect_state(
        cfg=object(),
        run=_docker("running"),
        agent_factory=lambda cfg: Agent(cfg, session={"state": "unknown"}),
        apps=[AppInfo(name="paint", full_name="Paint", executable="C:\\p.exe")],
        presented={0x1},
        desktop_running=lambda: False,
    )

    assert state["apps"][0]["presented"] is False
    assert state["session"]["guest"] == {"state": "unknown"}
    assert state["errors"] == ["window list unavailable: top-level unreachable"]
