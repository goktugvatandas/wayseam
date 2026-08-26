# SPDX-License-Identifier: MIT
"""Collect a compact, secret-free status snapshot for the Omarchy applet.

Every probe is bounded by a timeout and failure-isolated: a missing docker
binary, an unreachable guest agent, or a broken app profile turns into an
entry in ``errors`` rather than an exception, so the applet always gets a
well-formed document quickly.
"""

from __future__ import annotations

import dataclasses
import subprocess
from collections.abc import Callable, Iterable
from typing import Any

from wayseam.omarchy.state import SCHEMA, get_menu_placement, get_mode

CONTAINER = "omarchy-windows"
_DOCKER_TIMEOUT = 5.0


def _as_dict(value: Any) -> dict[str, Any] | None:
    """Accept either a plain dict or the agent client's frozen dataclasses."""
    if isinstance(value, dict):
        return value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    return None


def vm_running(
    container: str = CONTAINER,
    *,
    run: Callable[..., Any] = subprocess.run,
) -> tuple[bool, str | None]:
    """Return ``(running, error)`` from ``docker inspect``; tolerate docker missing."""
    cmd = ["docker", "inspect", "--format", "{{.State.Status}}", container]
    try:
        completed = run(cmd, capture_output=True, text=True, timeout=_DOCKER_TIMEOUT, check=False)
    except FileNotFoundError:
        return False, "docker is not installed"
    except subprocess.TimeoutExpired:
        return False, "docker inspect timed out"
    except (subprocess.SubprocessError, OSError) as exc:
        return False, f"docker inspect failed: {exc}"
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip().splitlines()
        return False, detail[-1] if detail else f"docker inspect exit {completed.returncode}"
    return (completed.stdout or "").strip() == "running", None


def _default_agent(cfg: Any) -> Any:
    from wayseam.guest.agent import AgentClient

    return AgentClient(cfg)


def _load_config() -> Any:
    from wayseam.config import Config

    return Config.load()


def _default_apps() -> list[Any]:
    from wayseam.apps import list_available_apps

    return list_available_apps()


def _default_presented() -> set[int]:
    from wayseam.present.watch import presented_hwnds

    return presented_hwnds()


def _default_desktop_running() -> bool:
    from wayseam.desktop_mode.freerdp import _find_existing_session

    return _find_existing_session("desktop") is not None


def _presented_slugs(windows: Iterable[Any], presented: set[int], apps: list[Any]) -> set[str]:
    from wayseam.present.watch import match_window_to_app

    slugs: set[str] = set()
    for window in windows:
        if window.hwnd not in presented:
            continue
        app = match_window_to_app(window, apps)
        if app is not None:
            slugs.add(app.name)
    return slugs


def collect_state(
    cfg: Any = None,
    *,
    container: str = CONTAINER,
    run: Callable[..., Any] = subprocess.run,
    agent_factory: Callable[[Any], Any] = _default_agent,
    apps: list[Any] | None = None,
    presented: set[int] | None = None,
    desktop_running: Callable[[], bool] = _default_desktop_running,
) -> dict[str, Any]:
    """Return the applet status document (see module docstring)."""
    errors: list[str] = []
    state: dict[str, Any] = {
        "schema": SCHEMA,
        "mode": get_mode(),
        "menu_placement": get_menu_placement(),
        "vm": {"running": False, "container": container},
        "agent": {"ok": False, "version": None},
        "session": {"desktop_client_running": False, "presenters": 0, "guest": None},
        "apps": [],
        "errors": errors,
    }

    running, error = vm_running(container, run=run)
    state["vm"]["running"] = running
    if error:
        errors.append(error)

    try:
        state["session"]["desktop_client_running"] = bool(desktop_running())
    except Exception as exc:  # noqa: BLE001
        errors.append(f"desktop client probe failed: {exc}")

    if presented is None:
        try:
            presented = _default_presented()
        except Exception as exc:  # noqa: BLE001
            presented = set()
            errors.append(f"presenter scan failed: {exc}")
    state["session"]["presenters"] = len(presented)

    if apps is None:
        try:
            apps = _default_apps()
        except Exception as exc:  # noqa: BLE001
            apps = []
            errors.append(f"app catalog unavailable: {exc}")

    client: Any = None
    try:
        client = agent_factory(cfg if cfg is not None else _load_config())
        health = client.health()
        state["agent"]["ok"] = bool(health.get("ok", True)) if isinstance(health, dict) else True
        version = health.get("version") if isinstance(health, dict) else None
        state["agent"]["version"] = str(version) if version is not None else None
    except Exception as exc:  # noqa: BLE001
        client = None
        errors.append(f"guest agent unavailable: {exc}")

    presented_slugs: set[str] = set()
    if client is not None and state["agent"]["ok"]:
        session_state = getattr(client, "session_state", None)
        if callable(session_state):
            try:
                guest = session_state()
                state["session"]["guest"] = _as_dict(guest)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"guest session state unavailable: {exc}")
        if presented:
            try:
                presented_slugs = _presented_slugs(client.top_level_windows(), presented, apps)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"window list unavailable: {exc}")

    state["apps"] = [
        {
            "slug": app.name,
            "name": app.full_name or app.name,
            "hidden": bool(getattr(app, "hidden", False)),
            "icon": str(getattr(app, "icon_path", "") or ""),
            "presented": app.name in presented_slugs,
        }
        for app in sorted(apps, key=lambda a: ((a.full_name or a.name).casefold(), a.name))
    ]
    return state
