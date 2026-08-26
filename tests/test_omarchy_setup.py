# SPDX-License-Identifier: MIT
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from wayseam.guest.agent import AgentUnavailableError
from wayseam.omarchy.plugin import PluginError
from wayseam.omarchy.setup import run_setup
from wayseam.vm.lifecycle import PodState, PodStatus


class _App:
    def __init__(self, name: str) -> None:
        self.name = name
        self.slug = name


def _fake_cfg() -> SimpleNamespace:
    saved: list[bool] = []
    pod = SimpleNamespace(backend="podman")
    return SimpleNamespace(pod=pod, save=lambda: saved.append(True), _saved=saved)


def _base_kwargs(calls: list[str], **overrides: Any) -> dict[str, Any]:
    """Fully-injected happy-path defaults; override per test."""

    def rec(name: str, ret: Any = None):
        def _fn(*a: Any, **k: Any) -> Any:
            calls.append(name)
            return ret
        return _fn

    kwargs: dict[str, Any] = {
        "cfg": _fake_cfg(),
        "timeout": 5,
        "clock": lambda: 0.0,
        "sleep": rec("sleep"),
        "docker_which": lambda name: "/usr/bin/docker",
        "compose_exists": lambda: True,
        "inspect": rec("inspect", SimpleNamespace(container_name="omarchy-windows")),
        "adopt": rec("adopt"),
        "ensure_override": rec("ensure_override"),
        "ensure_token": rec("ensure_token", "deadbeef"),
        "load_config": rec("load_config", _fake_cfg()),
        "start": rec("start", PodStatus(state=PodState.RUNNING, ip="127.0.0.1")),
        "health": rec("health", {"ok": True, "version": "1.0"}),
        "recover": rec("recover"),
        "discover": rec("discover", [_App("acrobat"), _App("word"), _App("excel")]),
        "persist": rec("persist"),
        "sync": rec("sync", 3),
        "applet": rec("applet", {"enabled": True, "warnings": []}),
    }
    kwargs.update(overrides)
    return kwargs


def test_happy_path_runs_steps_in_order() -> None:
    calls: list[str] = []
    result = run_setup(**_base_kwargs(calls))

    assert result["ok"] is True
    assert result["stage"] == "done"
    assert result["apps"] == 3
    assert result["warnings"] == []
    # Ordered external actions.
    assert calls == [
        "inspect",
        "adopt",
        "ensure_override",
        "ensure_token",
        "start",
        "health",
        "discover",
        "persist",
        "sync",
        "applet",
    ]
    step_names = [s["name"] for s in result["steps"]]
    assert step_names == [
        "preflight",
        "adopt",
        "start",
        "wait-agent",
        "discover",
        "menu",
        "applet",
    ]


def test_preflight_fails_when_docker_missing() -> None:
    calls: list[str] = []
    result = run_setup(**_base_kwargs(calls, docker_which=lambda name: None))

    assert result["ok"] is False
    assert result["stage"] == "preflight"
    # No later external action ran.
    assert calls == []
    assert result["steps"][-1]["name"] == "preflight"


def test_preflight_fails_when_compose_missing() -> None:
    calls: list[str] = []
    result = run_setup(**_base_kwargs(calls, compose_exists=lambda: False))

    assert result["ok"] is False
    assert result["stage"] == "preflight"
    assert calls == []


def test_agent_not_healthy_pauses_for_bootstrap() -> None:
    calls: list[str] = []

    def _unhealthy(cfg: Any) -> dict[str, Any]:
        calls.append("health")
        raise AgentUnavailableError("connection refused")

    result = run_setup(
        **_base_kwargs(calls, timeout=0, health=_unhealthy)
    )

    assert result["ok"] is False
    assert result["awaiting_bootstrap"] is True
    assert result["stage"] == "guest-bootstrap"
    assert "recover" in calls
    # Discovery / menu / applet must NOT run before the guest is bootstrapped.
    assert "discover" not in calls
    assert "sync" not in calls
    assert "applet" not in calls


def test_idempotent_rerun_after_agent_healthy_completes() -> None:
    calls: list[str] = []
    kwargs = _base_kwargs(calls)
    # Simulate a re-run: everything already adopted, agent now healthy.
    first = run_setup(**kwargs)
    assert first["ok"] is True

    calls.clear()
    second = run_setup(**_base_kwargs(calls))
    assert second["ok"] is True
    assert "discover" in calls
    assert "sync" in calls
    assert "applet" in calls


def test_plugin_error_degrades_to_warning() -> None:
    calls: list[str] = []

    def _applet() -> dict[str, Any]:
        calls.append("applet")
        raise PluginError("omarchy-shell not running")

    result = run_setup(**_base_kwargs(calls, applet=_applet))

    assert result["ok"] is True
    assert any("applet" in w for w in result["warnings"])
    applet_step = next(s for s in result["steps"] if s["name"] == "applet")
    assert applet_step["status"] == "warning"


def test_no_applet_skips_plugin_install() -> None:
    calls: list[str] = []
    result = run_setup(**_base_kwargs(calls, install_applet=False))

    assert result["ok"] is True
    assert "applet" not in calls
    applet_step = next(s for s in result["steps"] if s["name"] == "applet")
    assert applet_step["status"] == "skipped"


def test_start_failure_returns_error() -> None:
    calls: list[str] = []
    result = run_setup(
        **_base_kwargs(
            calls,
            start=lambda cfg: PodStatus(state=PodState.ERROR, error="port in use"),
        )
    )

    assert result["ok"] is False
    assert result["stage"] == "start"
    assert "discover" not in calls


# --- CLI wiring ---------------------------------------------------------------


def test_cmd_setup_returns_3_on_awaiting_bootstrap(monkeypatch: pytest.MonkeyPatch) -> None:
    import wayseam.omarchy.setup as setup_mod
    from wayseam.cli_omarchy import _cmd_setup

    def _fake_run_setup(**kwargs: Any) -> dict[str, Any]:
        return {
            "ok": False,
            "awaiting_bootstrap": True,
            "stage": "guest-bootstrap",
            "steps": [{"name": "preflight", "status": "ok", "detail": ""}],
            "warnings": [],
            "message": "paste the PowerShell",
        }

    monkeypatch.setattr(setup_mod, "run_setup", _fake_run_setup)
    args = SimpleNamespace(timeout=300, no_applet=False)
    assert _cmd_setup(args) == 3


def test_cmd_setup_returns_0_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    import wayseam.omarchy.setup as setup_mod
    from wayseam.cli_omarchy import _cmd_setup

    def _fake_run_setup(**kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "stage": "done",
            "apps": 4,
            "steps": [{"name": "applet", "status": "ok", "detail": "enabled"}],
            "warnings": [],
        }

    monkeypatch.setattr(setup_mod, "run_setup", _fake_run_setup)
    args = SimpleNamespace(timeout=300, no_applet=False)
    assert _cmd_setup(args) == 0


def test_cmd_setup_returns_1_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import wayseam.omarchy.setup as setup_mod
    from wayseam.cli_omarchy import _cmd_setup

    def _fake_run_setup(**kwargs: Any) -> dict[str, Any]:
        return {
            "ok": False,
            "stage": "start",
            "error": "port in use",
            "steps": [{"name": "start", "status": "failed", "detail": "port in use"}],
            "warnings": [],
        }

    monkeypatch.setattr(setup_mod, "run_setup", _fake_run_setup)
    args = SimpleNamespace(timeout=300, no_applet=False)
    assert _cmd_setup(args) == 1


def test_setup_treats_token_rejection_as_bootstrap_needed() -> None:
    from types import SimpleNamespace

    from wayseam.guest.agent import AgentAuthError
    from wayseam.omarchy.setup import run_setup

    calls: list[str] = []

    def auth_probe(cfg):
        calls.append("probe")
        raise AgentAuthError("401")

    result = run_setup(
        cfg=SimpleNamespace(pod=SimpleNamespace(backend="omarchy"), rdp=SimpleNamespace(ip="127.0.0.1"), save=lambda: None),
        docker_which=lambda name: "/usr/bin/docker",
        compose_exists=lambda: True,
        inspect=lambda: object(),
        adopt=lambda cfg, inst: None,
        ensure_override=lambda cfg: None,
        ensure_token=lambda: "t",
        start=lambda cfg: SimpleNamespace(state=SimpleNamespace(value="running")),
        health=lambda cfg: {"ok": True},
        auth_probe=auth_probe,
        recover=lambda: calls.append("recover"),
        timeout=0,
        clock=lambda: 0.0,
        sleep=lambda s: None,
    )
    assert calls == ["probe", "recover"]
    assert result["awaiting_bootstrap"] is True
    assert "rejects this host" in result["steps"][-1]["detail"]
