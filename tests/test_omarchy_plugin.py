# SPDX-License-Identifier: MIT
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from wayseam.omarchy.plugin import (
    PLUGIN_ID,
    PluginError,
    ensure_plugin_link,
    install_plugin,
    plugin_link_path,
    plugin_source_dir,
    plugin_status,
)


class _Runner:
    def __init__(self, *, listed: bool = True, enabled: bool = True, fail: str = "") -> None:
        self.calls: list[list[str]] = []
        self.listed = listed
        self.enabled = enabled
        self.fail = fail

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        assert kwargs["timeout"] > 0
        assert kwargs["capture_output"] is True
        if self.fail and cmd[0] == self.fail:
            return SimpleNamespace(returncode=1, stdout="", stderr="shell not running")
        if cmd[:3] == ["omarchy-shell", "shell", "listPlugins"]:
            plugins = [{"id": "omarchy.bar", "enabled": True}]
            if self.listed:
                plugins.append({"id": PLUGIN_ID, "enabled": self.enabled})
            return SimpleNamespace(returncode=0, stdout=json.dumps(plugins), stderr="")
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")


def _missing(cmd, **kwargs):
    raise FileNotFoundError(cmd[0])


def test_paths_follow_package_and_omarchy_conventions() -> None:
    assert plugin_source_dir().name == "plugin"
    assert plugin_source_dir().parent.name == "omarchy"
    assert plugin_link_path() == Path.home() / ".config" / "omarchy" / "plugins" / PLUGIN_ID


def test_ensure_plugin_link_creates_repairs_and_is_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    source.mkdir()
    link = tmp_path / "plugins" / PLUGIN_ID

    assert ensure_plugin_link(source, link) is True
    assert link.is_symlink() and os.readlink(link) == str(source)
    assert ensure_plugin_link(source, link) is False

    link.unlink()
    link.symlink_to(tmp_path / "elsewhere")
    assert ensure_plugin_link(source, link) is True
    assert os.readlink(link) == str(source)


def test_ensure_plugin_link_refuses_missing_source_and_real_directories(tmp_path: Path) -> None:
    link = tmp_path / "plugins" / PLUGIN_ID
    with pytest.raises(PluginError, match="does not exist yet"):
        ensure_plugin_link(tmp_path / "nope", link)

    source = tmp_path / "plugin"
    source.mkdir()
    link.mkdir(parents=True)
    with pytest.raises(PluginError, match="not a symlink"):
        ensure_plugin_link(source, link)


def test_install_plugin_links_rescans_and_enables(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    source.mkdir()
    link = tmp_path / "plugins" / PLUGIN_ID
    runner = _Runner()

    result = install_plugin(source=source, link=link, run=runner)

    assert result["link_changed"] is True
    assert result["rescanned"] is True
    assert result["enabled"] is True
    assert result["warnings"] == []
    assert runner.calls == [
        ["omarchy-shell", "shell", "rescanPlugins"],
        ["omarchy-plugin-enable", PLUGIN_ID],
        ["omarchy-restart-shell"],
    ]

    again = install_plugin(source=source, link=link, run=runner)
    assert again["link_changed"] is False
    assert link.is_symlink()


def test_install_plugin_tolerates_missing_or_failing_shell_tools(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    source.mkdir()
    link = tmp_path / "plugins" / PLUGIN_ID

    result = install_plugin(source=source, link=link, run=_missing)
    assert link.is_symlink()
    assert result["rescanned"] is False and result["enabled"] is False
    assert result["warnings"] == [
        "omarchy-shell is not installed",
        "omarchy-plugin-enable is not installed",
    ]

    result = install_plugin(source=source, link=link, run=_Runner(fail="omarchy-plugin-enable"))
    assert result["rescanned"] is True
    assert result["enabled"] is False
    assert result["warnings"] == [f"omarchy-plugin-enable {PLUGIN_ID}: shell not running"]


def test_plugin_status_reports_link_and_shell_listing(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    source.mkdir()
    link = tmp_path / "plugins" / PLUGIN_ID

    status = plugin_status(source=source, link=link, run=_Runner(listed=False))
    assert status["linked"] is False
    assert status["target"] is None
    assert status["listed"] is False
    assert status["enabled"] is None

    ensure_plugin_link(source, link)
    status = plugin_status(source=source, link=link, run=_Runner(listed=True, enabled=False))
    assert status["linked"] is True
    assert status["target"] == str(source)
    assert status["listed"] is True
    assert status["enabled"] is False

    status = plugin_status(source=source, link=link, run=_missing)
    assert status["listed"] is None
    assert status["warnings"] == ["omarchy-shell is not installed"]
