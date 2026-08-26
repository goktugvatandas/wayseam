# SPDX-License-Identifier: MIT
"""Switch between the mutually exclusive Wayseam and Desktop presentation modes.

ADR 0002: each VM has exactly one active presentation mode. Switching keeps
the Windows applications running — only the Linux-side presentation changes:

* ``desktop``: stop every Wayseam presenter (``wayseam.present.window``) and the
  window watcher, then open the conventional desktop RDP client. Presenters are
  sent SIGTERM; their signal path closes the host surface without forwarding a
  close into Windows.
* ``wayseam``: terminate the desktop RDP client (this only disconnects the RDP
  session; Windows keeps the applications alive), ask the guest agent to
  reattach the console session so DWM/WGC capture works again, and start the
  window watcher so every open application window is re-presented.

All process interaction is injectable so unit tests never touch real
processes. This module never calls ``omarchy-windows-vm launch`` because that
launcher stops the VM when its RDP client exits.
"""

from __future__ import annotations

import dataclasses
import os
import signal
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from wayseam.omarchy.state import MODES, get_mode, set_mode
from wayseam.paths import runtime_dir

PRESENTER_MODULE = "wayseam.present.window"
WATCHER_MODULE = "wayseam.present.watch"
DESKTOP_SESSION = "desktop"


def _as_dict(value: Any) -> dict[str, Any] | None:
    """Accept either a plain dict or the agent client's frozen dataclasses."""
    if isinstance(value, dict):
        return value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    return None


def _read_argv(cmdline_path: Path) -> list[str] | None:
    try:
        raw = cmdline_path.read_bytes()
    except OSError:
        return None
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def presenter_pids(proc: Path = Path("/proc")) -> list[int]:
    """Return PIDs whose argv contains the exact ``wayseam.present.window`` token."""
    result: list[int] = []
    for cmdline_path in proc.glob("[0-9]*/cmdline"):
        argv = _read_argv(cmdline_path)
        if not argv or PRESENTER_MODULE not in argv:
            continue
        try:
            result.append(int(cmdline_path.parent.name))
        except ValueError:
            continue
    return sorted(result)


def watcher_pid(proc: Path = Path("/proc")) -> int | None:
    """Return the live watcher PID from its pid file, verified against argv."""
    pid_path = runtime_dir() / "wayseam-watch.pid"
    try:
        pid = int(pid_path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None
    if pid <= 0:
        return None
    argv = _read_argv(proc / str(pid) / "cmdline")
    if not argv or WATCHER_MODULE not in argv:
        return None
    return pid


def desktop_session_pid() -> int | None:
    """Return the PID of the running desktop RDP client, if any."""
    from wayseam.desktop_mode.process import is_freerdp_pid

    pid_file = runtime_dir() / f"{DESKTOP_SESSION}.cproc"
    try:
        pid = int(pid_file.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None
    if pid <= 0 or not is_freerdp_pid(pid):
        return None
    return pid


def terminate_pid(pid: int) -> bool:
    """SIGTERM ``pid`` (its whole group when it is a session leader we spawned)."""
    if pid <= 0:
        return False
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, PermissionError):
        return False
    try:
        if pgid == pid:
            os.killpg(pgid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def wait_for_exit(pid: int, timeout: float) -> bool:
    """Poll until ``pid`` is gone or ``timeout`` seconds elapsed."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def _default_launch_desktop(cfg: Any) -> Any:
    from wayseam.desktop_mode.freerdp import launch_desktop

    return launch_desktop(cfg)


def _default_agent(cfg: Any) -> Any:
    from wayseam.guest.agent import AgentClient

    return AgentClient(cfg)


def _default_ensure_watcher() -> bool:
    from wayseam.present.watch import ensure_window_watcher

    return ensure_window_watcher()


def _load_config() -> Any:
    from wayseam.config import Config

    return Config.load()


def switch_mode(
    target: str,
    *,
    cfg: Any = None,
    terminate: Callable[[int], bool] = terminate_pid,
    presenters: Callable[[], list[int]] = presenter_pids,
    watcher: Callable[[], int | None] = watcher_pid,
    desktop_pid: Callable[[], int | None] = desktop_session_pid,
    launch_desktop: Callable[[Any], Any] = _default_launch_desktop,
    agent_factory: Callable[[Any], Any] = _default_agent,
    ensure_watcher: Callable[[], bool] = _default_ensure_watcher,
    wait_exit: Callable[[int, float], bool] = wait_for_exit,
    exit_timeout: float = 5.0,
) -> dict[str, Any]:
    """Persist ``target`` and reconcile Linux-side processes with it.

    Returns a summary dict. Warnings are collected instead of raised so a
    partially available environment (agent down, no watcher) still lands in
    the requested mode; the caller decides how loudly to report them.
    """
    if target not in MODES:
        raise ValueError(f"unknown mode {target!r}; expected one of {', '.join(MODES)}")

    previous = get_mode()
    result: dict[str, Any] = {
        "mode": target,
        "previous_mode": previous,
        "changed": previous != target,
        "presenters_terminated": 0,
        "watcher_terminated": False,
        "desktop_client": "unchanged",
        "session": None,
        "watcher_started": None,
        "warnings": [],
    }
    warnings: list[str] = result["warnings"]

    # Persist first: the watcher polls the mode and exits on its own, and any
    # presenter started by a racing launch sees the new mode too.
    set_mode(target)

    if target == "desktop":
        pid = watcher()
        if pid:
            result["watcher_terminated"] = terminate(pid)
        for presenter in presenters():
            if terminate(presenter):
                result["presenters_terminated"] += 1
        if desktop_pid():
            result["desktop_client"] = "running"
        else:
            try:
                launch_desktop(cfg if cfg is not None else _load_config())
                result["desktop_client"] = "started"
            except Exception as exc:  # noqa: BLE001 - reported, mode already persisted
                result["desktop_client"] = "failed"
                warnings.append(f"desktop client launch failed: {exc}")
        return result

    pid = desktop_pid()
    if pid:
        if terminate(pid):
            result["desktop_client"] = "stopped"
            if not wait_exit(pid, exit_timeout):
                warnings.append("desktop client did not exit in time")
        else:
            warnings.append("could not signal the desktop client")

    try:
        client = agent_factory(cfg if cfg is not None else _load_config())
    except Exception as exc:  # noqa: BLE001
        client = None
        warnings.append(f"guest agent unavailable: {exc}")
    reconnect = getattr(client, "session_reconnect_console", None) if client else None
    if callable(reconnect):
        try:
            session = reconnect()
            result["session"] = _as_dict(session)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"console reconnect failed: {exc}")
    elif client is not None:
        warnings.append("guest agent does not support console reconnect")

    try:
        result["watcher_started"] = bool(ensure_watcher())
    except Exception as exc:  # noqa: BLE001
        result["watcher_started"] = False
        warnings.append(f"window watcher failed to start: {exc}")
    return result
