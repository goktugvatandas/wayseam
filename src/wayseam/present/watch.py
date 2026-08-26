# SPDX-License-Identifier: MIT
"""Automatically present known guest applications that open new root HWNDs."""

from __future__ import annotations

import argparse
import fcntl
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from wayseam.apps import AppInfo, list_available_apps
from wayseam.config import Config
from wayseam.guest.agent import AgentClient, WayseamTopLevel
from wayseam.omarchy.state import get_mode
from wayseam.paths import runtime_dir

_SHELL_CLASSES = {
    "progman",
    "workerw",
    "shell_traywnd",
    "shell_secondarytraywnd",
}
_EXPLORER_WINDOW_CLASSES = {"cabinetwclass", "explorewclass"}


def _windows_path(value: str) -> str:
    return value.replace("/", "\\").rstrip("\\").casefold()


def match_window_to_app(
    window: WayseamTopLevel,
    apps: list[AppInfo],
) -> AppInfo | None:
    """Match a guest process path to a discovered Wayseam application."""
    process_path = _windows_path(window.process_path)
    for app in apps:
        if app.presentation != "wayseam":
            continue
        executable = _windows_path(app.executable)
        if not executable:
            continue
        if process_path == executable:
            return app
        if app.launch_uri and process_path.startswith(executable + "\\"):
            return app
    return None


def _is_shell_surface(window: WayseamTopLevel) -> bool:
    class_name = window.class_name.casefold()
    if class_name in _SHELL_CLASSES:
        return True
    process_path = _windows_path(window.process_path)
    return (
        process_path.endswith("\\explorer.exe")
        and class_name not in _EXPLORER_WINDOW_CLASSES
    )


def missing_presentations(
    windows: list[WayseamTopLevel],
    *,
    presented_hwnds: set[int],
    apps: list[AppInfo],
) -> list[tuple[WayseamTopLevel, AppInfo]]:
    """Return known Wayseam roots that do not yet have a host surface."""
    result: list[tuple[WayseamTopLevel, AppInfo]] = []
    for window in windows:
        if window.hwnd in presented_hwnds or _is_shell_surface(window):
            continue
        app = match_window_to_app(window, apps)
        if app is not None:
            result.append((window, app))
    return result


def presented_hwnds() -> set[int]:
    """Read only Wayseam presenter argv and return their bounded HWND IDs."""
    result: set[int] = set()
    for cmdline_path in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            args = [
                part.decode("utf-8", "replace")
                for part in cmdline_path.read_bytes().split(b"\0")
                if part
            ]
        except OSError:
            continue
        if "wayseam.present.window" not in args or "--hwnd" not in args:
            continue
        index = args.index("--hwnd")
        if index + 1 >= len(args):
            continue
        try:
            hwnd = int(args[index + 1], 0)
        except ValueError:
            continue
        if hwnd > 0:
            result.add(hwnd)
    return result


def _watcher_process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    return b"wayseam.present.watch" in cmdline


def _source_environment() -> dict[str, str]:
    source_root = str(Path(__file__).resolve().parents[2])
    allowed = {
        "DBUS_SESSION_BUS_ADDRESS",
        "DISPLAY",
        "GDK_BACKEND",
        "GTK_THEME",
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "PYTHONPATH",
        "WAYLAND_DISPLAY",
        "XAUTHORITY",
        "XCURSOR_SIZE",
        "XCURSOR_THEME",
        "XDG_CONFIG_DIRS",
        "XDG_DATA_HOME",
        "XDG_DATA_DIRS",
        "XDG_CURRENT_DESKTOP",
        "XDG_RUNTIME_DIR",
        "XDG_SESSION_TYPE",
    }
    env = {key: value for key, value in os.environ.items() if key in allowed}
    inherited = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = source_root + (os.pathsep + inherited if inherited else "")
    return env


def ensure_window_watcher() -> bool:
    """Start the singleton window watcher without delaying application launch."""
    directory = runtime_dir()
    directory.mkdir(parents=True, exist_ok=True)
    pid_path = directory / "wayseam-watch.pid"
    start_lock_path = directory / "wayseam-watch-start.lock"
    with start_lock_path.open("a+b") as start_lock:
        fcntl.flock(start_lock.fileno(), fcntl.LOCK_EX)
        try:
            pid = int(pid_path.read_text(encoding="ascii").strip())
        except (FileNotFoundError, OSError, ValueError):
            pid = 0
        if _watcher_process_alive(pid):
            return True
        log_path = directory / "wayseam-watch.log"
        with log_path.open("ab", buffering=0) as log_stream:
            process = subprocess.Popen(
                [sys.executable, "-m", "wayseam.present.watch"],
                stdin=subprocess.DEVNULL,
                stdout=log_stream,
                stderr=subprocess.STDOUT,
                env=_source_environment(),
                start_new_session=True,
            )
        pid_path.write_text(f"{process.pid}\n", encoding="ascii")
        time.sleep(0.05)
        return process.poll() is None


class PresentationBackoff:
    """Remember HWNDs whose presenter just started or keeps failing.

    Without this the watcher re-spawned a presenter every poll while the
    previous one was still initializing (two surfaces fighting over one
    HWND's size) or crashing on startup (thousands of log lines per minute
    when GTK could not open a display).
    """

    def __init__(
        self,
        *,
        grace_seconds: float = 3.0,
        base_delay: float = 2.0,
        max_delay: float = 60.0,
        give_up_after: int = 5,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.grace_seconds = grace_seconds
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.give_up_after = give_up_after
        self.clock = clock
        self._next_try: dict[int, float] = {}
        self._failures: dict[int, int] = {}

    def ready(self, hwnd: int) -> bool:
        """True when this HWND may be presented right now."""
        if self._failures.get(hwnd, 0) >= self.give_up_after:
            return False
        return self.clock() >= self._next_try.get(hwnd, 0.0)

    def started(self, hwnd: int) -> None:
        """A presenter was spawned; hold off until it can show up in /proc."""
        self._next_try[hwnd] = self.clock() + self.grace_seconds

    def failed(self, hwnd: int) -> None:
        """A presenter failed to start; retry later with exponential backoff."""
        failures = self._failures.get(hwnd, 0) + 1
        self._failures[hwnd] = failures
        delay = min(self.max_delay, self.base_delay * (2 ** (failures - 1)))
        self._next_try[hwnd] = self.clock() + delay

    def forget_missing(self, live_hwnds: set[int]) -> None:
        """Drop bookkeeping for HWNDs that no longer exist in the guest."""
        for hwnd in tuple(self._next_try):
            if hwnd not in live_hwnds:
                self._next_try.pop(hwnd, None)
                self._failures.pop(hwnd, None)

    def failures(self, hwnd: int) -> int:
        return self._failures.get(hwnd, 0)


def _reap_children() -> None:
    """Collect exited presenters so they never linger as zombies.

    A zombie still answers ``kill -0``, which kept its HWND claim alive and
    stopped the watcher from re-presenting the window.
    """
    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def run_watcher(*, poll_interval: float = 0.2, idle_seconds: float = 5.0) -> int:
    """Present missing known roots until Wayseam Mode becomes idle."""
    directory = runtime_dir()
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / "wayseam-watch.lock"
    with lock_path.open("a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        stopping = False

        def stop(_signum: int, _frame: object) -> None:
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        client = AgentClient(Config.load())
        backoff = PresentationBackoff()
        apps = list_available_apps()
        apps_loaded_at = time.monotonic()
        idle_since: float | None = None
        while not stopping:
            # ADR 0002: Desktop Mode owns presentation; never re-present roots
            # while the desktop client is the active mode.
            if get_mode() != "wayseam":
                return 0
            now = time.monotonic()
            presented = presented_hwnds()
            if not presented:
                idle_since = idle_since or now
                if now - idle_since >= idle_seconds:
                    return 0
            else:
                idle_since = None
            if now - apps_loaded_at >= 5:
                apps = list_available_apps()
                apps_loaded_at = now
            try:
                windows = client.top_level_windows()
                backoff.forget_missing({window.hwnd for window in windows})
                missing = [
                    item
                    for item in missing_presentations(
                        windows, presented_hwnds=presented, apps=apps,
                    )
                    if backoff.ready(item[0].hwnd)
                ]
            except Exception:
                missing = []
            if missing:
                from wayseam.present.launch import (
                    WindowAlreadyPresented,
                    present_wayseam_window,
                )

                cfg = Config.load()
                for window, app in missing[:8]:
                    try:
                        present_wayseam_window(
                            cfg,
                            app,
                            hwnd=window.hwnd,
                            guest_pid=window.pid,
                            title=app.full_name,
                            automatic=True,
                        )
                    except WindowAlreadyPresented:
                        continue  # ``wayseam app run`` is presenting it
                    except Exception as exc:  # noqa: BLE001 - keep watching
                        backoff.failed(window.hwnd)
                        print(
                            f"wayseam watch: presenter for {hex(window.hwnd)} "
                            f"({app.name}) failed (attempt "
                            f"{backoff.failures(window.hwnd)}): {exc}",
                            file=sys.stderr,
                            flush=True,
                        )
                    else:
                        backoff.started(window.hwnd)
            _reap_children()
            time.sleep(poll_interval)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Watch for new Wayseam guest windows")
    parser.parse_args(argv)
    return run_watcher()


if __name__ == "__main__":
    raise SystemExit(main())
