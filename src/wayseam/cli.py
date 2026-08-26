# SPDX-License-Identifier: MIT
"""The ``wayseam`` command line."""

from __future__ import annotations

import argparse
import sys

from wayseam import __version__


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wayseam",
        description="Windows apps as native Hyprland windows on Omarchy.",
    )
    parser.add_argument("--version", action="version", version=f"wayseam {__version__}")
    sub = parser.add_subparsers(dest="command")

    setup = sub.add_parser(
        "setup",
        help="One command: adopt the Omarchy Windows VM, bootstrap the guest, "
        "discover apps, sync the menu, install the applet",
    )
    setup.add_argument("--timeout", type=int, default=300, help="seconds to wait for the guest agent")
    setup.add_argument("--no-applet", action="store_true", help="skip installing the shell applet")

    run = sub.add_parser("run", help="Launch a Windows app as a Wayseam window")
    run.add_argument("app", help="app slug (see `wayseam apps list`)")
    run.add_argument("file", nargs="?", help="file or URL to open with the app")
    run.add_argument("--wait", action="store_true", help="block until the window closes")

    state = sub.add_parser("state", help="Show the integration state")
    state.add_argument("--json", action="store_true", help="print the state as JSON")

    mode = sub.add_parser("mode", help="Get or set the presentation mode")
    mode_sub = mode.add_subparsers(dest="mode_command")
    mode_sub.add_parser("get", help="print the persisted presentation mode")
    mode_set = mode_sub.add_parser("set", help="switch between Wayseam and Desktop mode")
    mode_set.add_argument("mode", choices=["wayseam", "desktop"])

    menu = sub.add_parser("menu", help="Manage Windows apps in the Omarchy menu")
    menu_sub = menu.add_subparsers(dest="menu_command")
    menu_sub.add_parser("sync", help="apply the placement to omarchy-menu.jsonc and .desktop entries")
    placement = menu_sub.add_parser("placement", help="where Windows apps appear in the menu")
    placement_sub = placement.add_subparsers(dest="placement_command")
    placement_sub.add_parser("get", help="print the menu placement")
    placement_set = placement_sub.add_parser("set", help="set the menu placement")
    placement_set.add_argument("placement", choices=["apps", "windows-apps", "both", "none"])

    apps = sub.add_parser("apps", help="List and manage Windows apps")
    apps_sub = apps.add_subparsers(dest="apps_command")
    apps_sub.add_parser("list", help="list discovered apps")
    apps_sub.add_parser("refresh", help="re-run guest app discovery and sync the menu")
    visibility = apps_sub.add_parser("visibility", help="choose which apps the menu shows")
    visibility.add_argument("--all", action="store_true", help="show every app")
    visibility.add_argument("--none", action="store_true", help="hide every app")
    visibility.add_argument("--show", nargs="+", metavar="SLUG", help="show these apps")
    visibility.add_argument("--hide", nargs="+", metavar="SLUG", help="hide these apps")

    plugin = sub.add_parser("plugin", help="Manage the Wayseam shell applet")
    plugin_sub = plugin.add_subparsers(dest="plugin_command")
    plugin_sub.add_parser("install", help="link the applet into ~/.config/omarchy/plugins")
    plugin_sub.add_parser("status", help="show whether the applet is linked and listed")

    pod = sub.add_parser("pod", help="Control the Windows VM")
    pod_sub = pod.add_subparsers(dest="pod_command")
    pod_start = pod_sub.add_parser("start", help="start the VM and wait for it")
    pod_start.add_argument("--timeout", type=int, default=0, help="boot timeout override (s)")
    pod_start.add_argument("--no-wait", action="store_true", help="return once the container is up")
    pod_sub.add_parser("stop", help="stop the VM (Wayseam windows close first)")
    pod_restart = pod_sub.add_parser("restart", help="stop and start the VM")
    pod_restart.add_argument("--no-wait", action="store_true", help="return once the container is up")
    pod_sub.add_parser("status", help="show VM status")

    agent = sub.add_parser("agent", help="Guest agent operations")
    agent_sub = agent.add_subparsers(dest="agent_command")
    agent_sub.add_parser("status", help="check the guest agent health and version")
    agent_sub.add_parser("deploy", help="push the bundled agent into the running guest")

    return parser


def _cmd_run(args: argparse.Namespace) -> int:
    from wayseam.apps import find_app
    from wayseam.config import Config
    from wayseam.desktop.notify import notify_error

    if args.app == "desktop":
        from wayseam.desktop_mode.freerdp import launch_desktop

        try:
            session = launch_desktop(Config.load())
        except RuntimeError as exc:
            notify_error(str(exc))
            print(f"Launch failed: {exc}", file=sys.stderr)
            return 1
        print(f"Launching the Windows desktop... (stderr log: {session.stderr_log})")
        return 0
    app_info = find_app(args.app)
    if not app_info:
        print(f"Unknown app: {args.app}. Run 'wayseam apps list' to see available apps.")
        return 1
    cfg = Config.load()
    try:
        from wayseam.omarchy.state import get_mode

        if get_mode() == "desktop":
            from wayseam.desktop_mode.freerdp import _find_existing_session, launch_desktop
            from wayseam.guest.agent import AgentClient
            from wayseam.present.launch import _launch_guest_window

            if _find_existing_session("desktop") is None:
                launch_desktop(cfg)
            _launch_guest_window(AgentClient(cfg), app_info, args.file)
            print(f"Launching {app_info.full_name} in Desktop Mode...")
            return 0
        from wayseam.present.launch import launch_wayseam_app

        launch_wayseam_app(cfg, app_info, file_path=args.file, wait=args.wait)
        print(f"Launching {app_info.full_name} in Wayseam Mode...")
    except RuntimeError as exc:
        notify_error(str(exc))
        print(f"Launch failed: {exc}", file=sys.stderr)
        return 1
    return 0


def _retire_presentation() -> None:
    """Stop the watcher, presenters and desktop client before the VM goes away."""
    from wayseam.omarchy.mode import (
        desktop_session_pid,
        presenter_pids,
        terminate_pid,
        watcher_pid,
    )

    watcher = watcher_pid()
    if watcher:
        terminate_pid(watcher)
    for pid in presenter_pids():
        terminate_pid(pid)
    desktop = desktop_session_pid()
    if desktop:
        terminate_pid(desktop)


def _start_pod(cfg, *, wait: bool):
    from wayseam.vm.lifecycle import PodState, PodStatus, get_backend, start_pod

    if wait:
        return start_pod(cfg)
    backend = get_backend(cfg)
    try:
        backend.start()
    except Exception as exc:  # noqa: BLE001
        return PodStatus(state=PodState.ERROR, error=str(exc))
    return PodStatus(state=PodState.STARTING, ip=cfg.rdp.ip)


def _cmd_pod(args: argparse.Namespace) -> int:
    from wayseam.config import Config
    from wayseam.vm.lifecycle import PodState, pod_status, stop_pod

    cfg = Config.load()
    if args.pod_command == "start":
        if args.timeout:
            cfg.pod.boot_timeout = args.timeout
        status = _start_pod(cfg, wait=not args.no_wait)
    elif args.pod_command == "stop":
        _retire_presentation()
        status = stop_pod(cfg)
    elif args.pod_command == "restart":
        _retire_presentation()
        status = stop_pod(cfg)
        if status.state is not PodState.ERROR:
            status = _start_pod(cfg, wait=not args.no_wait)
    else:
        status = pod_status(cfg)
    print(f"State:    {status.state.value}")
    if status.ip:
        print(f"IP:       {status.ip}")
    if status.error:
        print(f"Error:    {status.error}", file=sys.stderr)
    return 0 if status.state not in (PodState.ERROR,) else 1


def _cmd_agent(args: argparse.Namespace) -> int:
    from wayseam.config import Config
    from wayseam.guest.agent import AgentClient, AgentError

    if args.agent_command == "deploy":
        from wayseam.guest.deploy import deploy_agent

        return deploy_agent(Config.load())
    try:
        health = AgentClient(Config.load()).health()
    except AgentError as exc:
        print(f"agent unreachable: {exc}", file=sys.stderr)
        return 1
    print(f"agent {health.get('version', '?')} healthy")
    try:
        display = AgentClient(Config.load()).display_status()
    except AgentError:
        return 0
    keepalive = "running" if display.get("keepalive") else "stopped"
    print(f"display: {display.get('status', '?')}")
    for item in display.get("displays", []):
        print(f"  {item.get('name')} {item.get('mode')} at {item.get('position')} {item.get('role')}"
              f" ({item.get('description')})")
    print(f"  keepalive {keepalive}, system DPI {display.get('system_dpi')}")
    return 0


def _cmd_apps_extra(args: argparse.Namespace) -> int | None:
    if args.apps_command == "list":
        from wayseam.apps import list_available_apps

        for app in list_available_apps():
            flag = " (hidden)" if getattr(app, "hidden", False) else ""
            print(f"{app.name:<24} {app.full_name}{flag}")
        return 0
    if args.apps_command == "refresh":
        from wayseam.config import Config
        from wayseam.guest.discovery import discover_apps, persist_discovered
        from wayseam.omarchy.menu import sync_menu

        apps = discover_apps(Config.load())
        written = persist_discovered(apps)
        print(f"Discovered {len(apps)} apps; wrote {len(written)} entries.")
        sync_menu()
        return 0
    return None


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "run":
        return _cmd_run(args)
    if args.command == "pod":
        return _cmd_pod(args)
    if args.command == "agent":
        return _cmd_agent(args)
    if args.command == "apps":
        handled = _cmd_apps_extra(args)
        if handled is not None:
            return handled
    # Omarchy integration commands share their handlers with the ported module.
    from wayseam.cli_omarchy import handle_omarchy

    args.omarchy_command = args.command
    return handle_omarchy(args)


if __name__ == "__main__":
    raise SystemExit(main())
