# SPDX-License-Identifier: MIT
from __future__ import annotations

import json
from argparse import Namespace

import pytest

from wayseam.cli_omarchy import handle_omarchy
from wayseam.omarchy import menu as omarchy_menu
from wayseam.omarchy import mode as omarchy_mode
from wayseam.omarchy import plugin as omarchy_plugin
from wayseam.omarchy import status as omarchy_status
from wayseam.omarchy.state import get_menu_placement, get_mode, set_menu_placement, set_mode


def _parse(argv: list[str]) -> Namespace:
    from wayseam.cli import _build_parser

    if argv and argv[0] == "omarchy":
        argv = argv[1:]
    args = _build_parser().parse_args(argv)
    args.omarchy_command = args.command
    return args


def _state_doc() -> dict:
    return {
        "schema": 1,
        "mode": "wayseam",
        "menu_placement": "apps",
        "vm": {"running": True, "container": "omarchy-windows"},
        "agent": {"ok": True, "version": "9"},
        "session": {"desktop_client_running": False, "presenters": 2, "guest": {"state": "active"}},
        "apps": [
            {"slug": "a", "name": "A", "hidden": False, "icon": "", "presented": True},
            {"slug": "b", "name": "B", "hidden": True, "icon": "", "presented": False},
        ],
        "errors": ["docker is not installed"],
    }


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["omarchy", "state", "--json"], {"omarchy_command": "state", "json": True}),
        (["omarchy", "state"], {"omarchy_command": "state", "json": False}),
        (["omarchy", "mode", "get"], {"mode_command": "get"}),
        (["omarchy", "mode", "set", "desktop"], {"mode_command": "set", "mode": "desktop"}),
        (["omarchy", "menu", "sync"], {"menu_command": "sync"}),
        (
            ["omarchy", "menu", "placement", "set", "windows-apps"],
            {"placement_command": "set", "placement": "windows-apps"},
        ),
        (
            ["omarchy", "apps", "visibility", "--hide", "a", "b", "--show", "c"],
            {"apps_command": "visibility", "hide": ["a", "b"], "show": ["c"]},
        ),
        (["omarchy", "apps", "visibility", "--all"], {"all": True, "none": False}),
        (["omarchy", "plugin", "install"], {"plugin_command": "install"}),
        (["omarchy", "plugin", "status"], {"plugin_command": "status"}),
        (["pod", "restart", "--no-wait"], {"pod_command": "restart", "no_wait": True}),
        (["pod", "start", "--no-wait"], {"pod_command": "start", "no_wait": True}),
        (["pod", "stop"], {"pod_command": "stop"}),
    ],
)
def test_parser_exposes_every_omarchy_subcommand(argv: list[str], expected: dict) -> None:
    args = _parse(argv)
    assert args.command == (argv[1] if argv[0] == "omarchy" else argv[0])
    for key, value in expected.items():
        assert getattr(args, key) == value


def test_parser_rejects_invalid_mode_and_placement() -> None:
    with pytest.raises(SystemExit):
        _parse(["omarchy", "mode", "set", "coherence"])
    with pytest.raises(SystemExit):
        _parse(["omarchy", "menu", "placement", "set", "dock"])


def test_main_dispatch_exits_with_handler_code(monkeypatch: pytest.MonkeyPatch) -> None:
    from wayseam import cli, cli_omarchy

    monkeypatch.setattr(cli_omarchy, "handle_omarchy", lambda args: 3)
    assert cli.main(["plugin", "status"]) == 3

    monkeypatch.setattr(cli_omarchy, "handle_omarchy", lambda args: 0)
    assert cli.main(["state"]) == 0


def test_state_json_prints_collected_document(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(omarchy_status, "collect_state", _state_doc)

    assert handle_omarchy(Namespace(omarchy_command="state", json=True)) == 0
    assert json.loads(capsys.readouterr().out) == _state_doc()


def test_state_human_summary(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(omarchy_status, "collect_state", _state_doc)

    assert handle_omarchy(Namespace(omarchy_command="state", json=False)) == 0
    out = capsys.readouterr().out
    assert "Mode:            wayseam" in out
    assert "Menu placement:  apps" in out
    assert "VM:              running (omarchy-windows)" in out
    assert "Guest agent:     ok v9" in out
    assert "desktop client stopped, 2 presenter(s), guest session active" in out
    assert "Apps:            2 known, 1 hidden, 1 presented" in out
    assert "! docker is not installed" in out


def test_mode_get_and_set(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    switched: list[str] = []

    def fake_switch(target: str) -> dict:
        switched.append(target)
        set_mode(target)
        return {"warnings": ["agent offline"], "desktop_client": "unchanged"}

    monkeypatch.setattr(omarchy_mode, "switch_mode", fake_switch)

    assert handle_omarchy(Namespace(omarchy_command="mode", mode_command="get")) == 0
    assert capsys.readouterr().out.strip() == "wayseam"

    set_desktop = Namespace(omarchy_command="mode", mode_command="set", mode="desktop")
    assert handle_omarchy(set_desktop) == 0
    captured = capsys.readouterr()
    assert switched == ["desktop"]
    assert get_mode() == "desktop"
    assert "Switched to Desktop Mode." in captured.out
    assert "warning: agent offline" in captured.err

    assert handle_omarchy(Namespace(omarchy_command="mode", mode_command="set", mode="rail")) == 2
    assert "unknown mode" in capsys.readouterr().err


def test_mode_set_returns_failure_when_desktop_client_could_not_start(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    failed = {"warnings": ["desktop client launch failed: x"], "desktop_client": "failed"}
    monkeypatch.setattr(omarchy_mode, "switch_mode", lambda target: failed)
    set_desktop = Namespace(omarchy_command="mode", mode_command="set", mode="desktop")
    assert handle_omarchy(set_desktop) == 1
    assert "desktop client launch failed" in capsys.readouterr().err


def test_menu_sync_and_placement(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    syncs: list[int] = []
    monkeypatch.setattr(omarchy_menu, "sync_menu", lambda: syncs.append(1) or 3)

    assert handle_omarchy(Namespace(omarchy_command="menu", menu_command="sync")) == 0
    assert "Synchronized 3 Windows applications" in capsys.readouterr().out
    assert syncs == [1]

    assert (
        handle_omarchy(
            Namespace(omarchy_command="menu", menu_command="placement", placement_command="get")
        )
        == 0
    )
    assert capsys.readouterr().out.strip() == "apps"

    assert (
        handle_omarchy(
            Namespace(
                omarchy_command="menu",
                menu_command="placement",
                placement_command="set",
                placement="both",
            )
        )
        == 0
    )
    assert get_menu_placement() == "both"
    assert syncs == [1, 1], "placement set runs the sync"
    assert "(placement: both)" in capsys.readouterr().out

    assert (
        handle_omarchy(
            Namespace(
                omarchy_command="menu",
                menu_command="placement",
                placement_command="set",
                placement="dock",
            )
        )
        == 2
    )
    assert get_menu_placement() == "both"


def test_apps_visibility_uses_set_app_hidden_then_syncs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from types import SimpleNamespace

    from wayseam import apps as core_app

    changes: list[tuple[str, bool]] = []
    syncs: list[int] = []
    known = {"paint", "xbox", "notepad"}

    def set_hidden(name: str, hidden: bool):
        changes.append((name, hidden))
        return SimpleNamespace(name=name) if name in known else None

    monkeypatch.setattr(core_app, "set_app_hidden", set_hidden)
    monkeypatch.setattr(
        core_app, "list_available_apps", lambda: [SimpleNamespace(name=n) for n in sorted(known)]
    )
    monkeypatch.setattr(omarchy_menu, "sync_menu", lambda: syncs.append(1) or len(known))
    set_menu_placement("windows-apps")

    def visibility(**kwargs):
        base = {"all": False, "none": False, "show": None, "hide": None}
        base.update(kwargs)
        return Namespace(omarchy_command="apps", apps_command="visibility", **base)

    assert handle_omarchy(visibility(hide=["xbox"], show=["paint"])) == 0
    assert changes == [("paint", False), ("xbox", True)]
    assert syncs == [1]

    changes.clear()
    assert handle_omarchy(visibility(none=True)) == 0
    assert changes == [("notepad", True), ("paint", True), ("xbox", True)]

    changes.clear()
    assert handle_omarchy(visibility(all=True)) == 0
    assert changes == [("notepad", False), ("paint", False), ("xbox", False)]

    changes.clear()
    assert handle_omarchy(visibility(show=["ghost"])) == 1
    assert "app 'ghost' not found" in capsys.readouterr().err
    assert syncs == [1, 1, 1, 1], "sync still runs so the menu matches the database"

    assert handle_omarchy(visibility(show=["../evil"])) == 1
    assert "invalid app name" in capsys.readouterr().err

    assert handle_omarchy(visibility()) == 2
    assert handle_omarchy(visibility(all=True, show=["paint"])) == 2
    assert handle_omarchy(visibility(show=["paint"], hide=["paint"])) == 2
    assert "cannot both show and hide: paint" in capsys.readouterr().err


def test_plugin_install_and_status(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        omarchy_plugin,
        "install_plugin",
        lambda: {
            "id": "goktugvatandas.wayseam",
            "link": "/home/u/.config/omarchy/plugins/goktugvatandas.wayseam",
            "source": "/pkg/wayseam/omarchy/plugin",
            "link_changed": True,
            "rescanned": True,
            "enabled": False,
            "warnings": ["omarchy-plugin-enable is not installed"],
        },
    )
    assert handle_omarchy(Namespace(omarchy_command="plugin", plugin_command="install")) == 0
    captured = capsys.readouterr()
    assert "Linked /home/u/.config/omarchy/plugins/goktugvatandas.wayseam -> /pkg" in captured.out
    assert "enable it with: omarchy-plugin-enable goktugvatandas.wayseam" in captured.out
    assert "warning: omarchy-plugin-enable is not installed" in captured.err

    def failing_install():
        raise omarchy_plugin.PluginError("plugin source directory does not exist yet: /x")

    monkeypatch.setattr(omarchy_plugin, "install_plugin", failing_install)
    assert handle_omarchy(Namespace(omarchy_command="plugin", plugin_command="install")) == 1
    assert "does not exist yet" in capsys.readouterr().err

    monkeypatch.setattr(
        omarchy_plugin,
        "plugin_status",
        lambda: {
            "id": "goktugvatandas.wayseam",
            "link": "/l",
            "source": "/s",
            "source_exists": True,
            "linked": True,
            "target": "/s",
            "listed": True,
            "enabled": True,
            "warnings": [],
        },
    )
    assert handle_omarchy(Namespace(omarchy_command="plugin", plugin_command="status")) == 0
    out = capsys.readouterr().out
    assert "Symlink:  /l -> /s (ok)" in out
    assert "Shell:    listed, enabled" in out


def test_usage_for_missing_or_unknown_subcommands(capsys: pytest.CaptureFixture[str]) -> None:
    assert handle_omarchy(Namespace(omarchy_command=None)) == 0
    assert "Usage: wayseam" in capsys.readouterr().out
    assert handle_omarchy(Namespace(omarchy_command="mode", mode_command=None)) == 2
    assert handle_omarchy(Namespace(omarchy_command="menu", menu_command=None)) == 2
    assert handle_omarchy(Namespace(omarchy_command="apps", apps_command=None)) == 2
    assert handle_omarchy(Namespace(omarchy_command="plugin", plugin_command=None)) == 2
