# SPDX-License-Identifier: MIT
"""CLI handlers for Omarchy desktop integration.

Every handler returns a process exit code (0 on success) and reports failures
on stderr so shell callers (the Omarchy menu, the applet) can react.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

_USAGE = (
    "Usage: wayseam {setup|state|mode|menu|apps|plugin} ...\n"
    "  setup [--timeout SECONDS] [--no-applet]\n"
    "  state [--json]\n"
    "  mode get | mode set <wayseam|desktop>\n"
    "  menu sync | menu placement get | menu placement set <apps|windows-apps|both|none>\n"
    "  apps visibility --all | --none | --show SLUG... | --hide SLUG...\n"
    "  plugin install | plugin status"
)


def _err(message: str) -> None:
    print(message, file=sys.stderr)


def _print_warnings(warnings: list[str]) -> None:
    for warning in warnings:
        _err(f"warning: {warning}")


def handle_omarchy(args: argparse.Namespace) -> int:
    """Route Omarchy integration subcommands and return an exit code."""
    command = getattr(args, "omarchy_command", None)
    try:
        if command == "setup":
            return _cmd_setup(args)
        if command == "state":
            return _cmd_state(bool(getattr(args, "json", False)))
        if command == "mode":
            return _cmd_mode(args)
        if command == "menu":
            return _cmd_menu(args)
        if command == "apps":
            return _cmd_apps(args)
        if command == "plugin":
            return _cmd_plugin(args)
    except ValueError as exc:
        _err(f"error: {exc}")
        return 2
    print(_USAGE)
    return 0 if command is None else 2


def _cmd_setup(args: argparse.Namespace) -> int:
    from wayseam.omarchy.setup import default_auth_probe, run_setup

    result = run_setup(
        timeout=getattr(args, "timeout", 300),
        install_applet=not getattr(args, "no_applet", False),
        auth_probe=default_auth_probe,
    )
    steps = result.get("steps", [])
    total = len(steps)
    for index, step in enumerate(steps, 1):
        detail = step.get("detail", "")
        suffix = f" — {detail}" if detail else ""
        print(f"[{index}/{total}] {step['name']}: {step['status']}{suffix}")
    _print_warnings(result.get("warnings", []))

    if result.get("ok"):
        print(f"Wayseam setup complete ({result.get('apps', 0)} app(s) discovered).")
        return 0
    if result.get("awaiting_bootstrap"):
        message = result.get("message")
        if message:
            print(message)
        return 3
    error = result.get("error")
    if error:
        _err(f"error: {error}")
    return 1


def _cmd_state(as_json: bool) -> int:
    from wayseam.omarchy.status import collect_state

    state = collect_state()
    if as_json:
        print(json.dumps(state, ensure_ascii=False))
        return 0
    print(f"Mode:            {state['mode']}")
    print(f"Menu placement:  {state['menu_placement']}")
    vm = state["vm"]
    print(f"VM:              {'running' if vm['running'] else 'stopped'} ({vm['container']})")
    agent = state["agent"]
    version = f" v{agent['version']}" if agent["version"] else ""
    print(f"Guest agent:     {'ok' if agent['ok'] else 'unavailable'}{version}")
    session = state["session"]
    guest = session.get("guest") or {}
    guest_note = f", guest session {guest.get('state')}" if guest else ""
    print(
        "Session:         "
        f"desktop client {'running' if session['desktop_client_running'] else 'stopped'}, "
        f"{session['presenters']} presenter(s){guest_note}"
    )
    apps = state["apps"]
    presented = sum(1 for app in apps if app["presented"])
    hidden = sum(1 for app in apps if app["hidden"])
    print(f"Apps:            {len(apps)} known, {hidden} hidden, {presented} presented")
    for error in state["errors"]:
        print(f"  ! {error}")
    return 0


def _cmd_mode(args: argparse.Namespace) -> int:
    from wayseam.omarchy.state import MODES, get_mode

    sub = getattr(args, "mode_command", None)
    if sub == "get":
        print(get_mode())
        return 0
    if sub == "set":
        target = args.mode
        if target not in MODES:
            _err(f"error: unknown mode {target!r}; expected one of {', '.join(MODES)}")
            return 2
        from wayseam.omarchy.mode import switch_mode

        result = switch_mode(target)
        _print_warnings(result["warnings"])
        label = "Desktop Mode" if target == "desktop" else "Wayseam Mode"
        print(f"Switched to {label}.")
        if result["desktop_client"] == "failed":
            return 1
        return 0
    _err("Usage: wayseam mode get | mode set <wayseam|desktop>")
    return 2


def _run_sync() -> int:
    from wayseam.desktop.icons import install_wayseam_icon

    install_wayseam_icon()
    from wayseam.omarchy.menu import sync_menu
    from wayseam.omarchy.state import get_menu_placement

    count = sync_menu()
    noun = "application" if count == 1 else "applications"
    print(
        f"Synchronized {count} Windows {noun} with the Omarchy menu "
        f"(placement: {get_menu_placement()})."
    )
    return 0


def _cmd_menu(args: argparse.Namespace) -> int:
    sub = getattr(args, "menu_command", None)
    if sub == "sync":
        return _run_sync()
    if sub == "placement":
        from wayseam.omarchy.state import PLACEMENTS, get_menu_placement, set_menu_placement

        action = getattr(args, "placement_command", None)
        if action == "get":
            print(get_menu_placement())
            return 0
        if action == "set":
            placement = args.placement
            if placement not in PLACEMENTS:
                _err(
                    f"error: unknown placement {placement!r}; "
                    f"expected one of {', '.join(PLACEMENTS)}"
                )
                return 2
            set_menu_placement(placement)
            return _run_sync()
    _err(
        "Usage: wayseam menu sync | menu placement get | "
        "menu placement set <apps|windows-apps|both|none>"
    )
    return 2


def _cmd_apps(args: argparse.Namespace) -> int:
    sub = getattr(args, "apps_command", None)
    if sub != "visibility":
        _err(
            "Usage: wayseam apps visibility "
            "--all | --none | --show SLUG... | --hide SLUG..."
        )
        return 2
    from wayseam.apps import _SAFE_NAME_RE, list_available_apps, set_app_hidden

    show_all = bool(getattr(args, "all", False))
    hide_all = bool(getattr(args, "none", False))
    show: list[str] = list(getattr(args, "show", None) or [])
    hide: list[str] = list(getattr(args, "hide", None) or [])
    if (show_all or hide_all) and (show or hide):
        _err("error: --all/--none cannot be combined with --show/--hide")
        return 2
    if not (show_all or hide_all or show or hide):
        _err("error: choose --all, --none, --show SLUG... or --hide SLUG...")
        return 2
    overlap = sorted(set(show) & set(hide))
    if overlap:
        _err(f"error: cannot both show and hide: {', '.join(overlap)}")
        return 2

    changes: list[tuple[str, bool]] = []
    if show_all or hide_all:
        changes = [(app.name, hide_all) for app in list_available_apps()]
    else:
        changes = [(slug, False) for slug in show] + [(slug, True) for slug in hide]

    failed = 0
    for slug, hidden in changes:
        if not _SAFE_NAME_RE.match(slug or ""):
            _err(f"error: invalid app name {slug!r}")
            failed += 1
            continue
        if set_app_hidden(slug, hidden) is None:
            _err(f"error: app '{slug}' not found")
            failed += 1
    code = _run_sync()
    return 1 if failed else code


def _cmd_plugin(args: argparse.Namespace) -> int:
    from wayseam.omarchy.plugin import PluginError, install_plugin, plugin_status

    sub = getattr(args, "plugin_command", None)
    if sub == "install":
        try:
            result: dict[str, Any] = install_plugin()
        except PluginError as exc:
            _err(f"error: {exc}")
            return 1
        _print_warnings(result["warnings"])
        verb = "Linked" if result["link_changed"] else "Already linked"
        print(f"{verb} {result['link']} -> {result['source']}")
        if result["enabled"]:
            print(f"Enabled {result['id']} in the Omarchy shell.")
        else:
            print(f"Plugin linked; enable it with: omarchy-plugin-enable {result['id']}")
        return 0
    if sub == "status":
        status = plugin_status()
        _print_warnings(status["warnings"])
        print(f"Plugin:   {status['id']}")
        source_state = "present" if status["source_exists"] else "missing"
        print(f"Source:   {status['source']} ({source_state})")
        target = f" -> {status['target']}" if status["target"] else ""
        print(f"Symlink:  {status['link']}{target} ({'ok' if status['linked'] else 'missing'})")
        if status["listed"] is None:
            print("Shell:    unknown (omarchy-shell unavailable)")
        elif status["listed"]:
            print(f"Shell:    listed, {'enabled' if status['enabled'] else 'disabled'}")
        else:
            print("Shell:    not listed (run: wayseam plugin install)")
        return 0
    _err("Usage: wayseam plugin install | plugin status")
    return 2
