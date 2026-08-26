# SPDX-License-Identifier: MIT
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from wayseam.omarchy.menu import (
    BEGIN_MARKER,
    END_MARKER,
    _parse_jsonc,
    menu_action,
    sync_menu,
)
from wayseam.omarchy.state import set_menu_placement


def _app(name: str, full_name: str, *, hidden: bool = False) -> SimpleNamespace:
    return SimpleNamespace(name=name, full_name=full_name, hidden=hidden)


class _Entries:
    """Fake desktop-entry writer so tests never touch the real XDG dirs."""

    def __init__(self) -> None:
        self.installed: list[tuple[str, bool]] = []
        self.removed: list[str] = []
        self.refreshed = 0

    def install(self, app: SimpleNamespace, *, no_display: bool) -> None:
        self.installed.append((app.name, no_display))

    def remove(self, slug: str) -> None:
        self.removed.append(slug)

    def refresh(self) -> None:
        self.refreshed += 1


def _sync(entries: _Entries, **kwargs: object) -> int:
    return sync_menu(
        install_entry=entries.install,
        remove_entry=entries.remove,
        refresh_database=entries.refresh,
        **kwargs,
    )


@pytest.fixture
def menu_path(tmp_path: Path) -> Path:
    return tmp_path / "omarchy-menu.jsonc"


def test_windows_apps_placement_writes_action_rows_omarchy4_understands(
    menu_path: Path,
) -> None:
    entries = _Entries()
    apps = [
        _app("notepad", "Notepad"),
        _app("hidden-tool", "Hidden Tool", hidden=True),
        _app("calculator", "Calculator"),
        _app("bad slug!", "Injected"),
    ]

    count = _sync(entries, apps=apps, menu_path=menu_path, placement="windows-apps")

    assert count == 2
    text = menu_path.read_text(encoding="utf-8")
    parsed = _parse_jsonc(text)
    assert BEGIN_MARKER in text and END_MARKER in text
    assert parsed["windows-apps"] == {"icon": "", "label": "Windows Apps"}
    assert parsed["windows-apps.calculator"] == {
        "icon": "",
        "label": "Calculator",
        "action": "uwsm-app -- gtk-launch wayseam-calculator.desktop",
    }
    assert parsed["windows-apps.notepad"]["action"] == menu_action("notepad")
    assert text.index("Calculator") < text.index("Notepad")
    assert "desktopId" not in text
    assert "Hidden Tool" not in text
    assert "Injected" not in text
    # Submenu-only placement hides the entries from the shell's Apps list.
    assert entries.installed == [("calculator", True), ("notepad", True)]
    assert entries.removed == ["hidden-tool"]
    assert entries.refreshed == 1


def test_both_placement_keeps_block_and_visible_entries(menu_path: Path) -> None:
    entries = _Entries()

    _sync(entries, apps=[_app("paint", "Paint")], menu_path=menu_path, placement="both")

    assert "windows-apps.paint" in _parse_jsonc(menu_path.read_text(encoding="utf-8"))
    assert entries.installed == [("paint", False)]


def test_apps_placement_removes_managed_block_and_shows_entries(menu_path: Path) -> None:
    entries = _Entries()
    prefix = (
        "{\n"
        '  "quick-chat": {"label":"Quick Chat","action":"quick-chat"},\n'
        "  // user comment must survive\n"
    )
    old_block = (
        f"  {BEGIN_MARKER}\n"
        '  "windows-apps": {"label":"Old"},\n'
        '  "windows-apps.old": {"desktopId":"wayseam-old","label":"Old App"},\n'
        f"  {END_MARKER}\n"
    )
    suffix = '  "personal": {"label":"Personal"},\n}\n'
    menu_path.write_text(prefix + old_block + suffix, encoding="utf-8")

    count = _sync(entries, apps=[_app("paint", "Paint")], menu_path=menu_path, placement="apps")

    assert count == 1
    text = menu_path.read_text(encoding="utf-8")
    assert text == prefix + suffix
    assert "windows-apps" not in _parse_jsonc(text)
    assert entries.installed == [("paint", False)]


def test_none_placement_removes_block_and_hides_entries(menu_path: Path) -> None:
    entries = _Entries()
    menu_path.write_text(
        "{\n"
        f"  {BEGIN_MARKER}\n"
        '  "windows-apps": {"label":"Old"},\n'
        f"  {END_MARKER}\n"
        "}\n",
        encoding="utf-8",
    )

    _sync(entries, apps=[_app("paint", "Paint")], menu_path=menu_path, placement="none")

    assert menu_path.read_text(encoding="utf-8") == "{\n}\n"
    assert entries.installed == [("paint", True)]


def test_removing_block_also_drops_the_separator_comma_we_inserted(menu_path: Path) -> None:
    entries = _Entries()
    menu_path.write_text(
        "{\n"
        '  "quick-chat": {"label":"Quick Chat"}\n'
        "  // keep this trailing comment exactly\n"
        "}\n",
        encoding="utf-8",
    )
    _sync(entries, apps=[_app("paint", "Paint")], menu_path=menu_path, placement="both")
    with_block = menu_path.read_text(encoding="utf-8")
    assert "  ,\n" in with_block
    assert "  // keep this trailing comment exactly\n" in with_block
    _parse_jsonc(with_block)

    _sync(entries, apps=[_app("paint", "Paint")], menu_path=menu_path, placement="apps")

    assert menu_path.read_text(encoding="utf-8") == (
        "{\n"
        '  "quick-chat": {"label":"Quick Chat"}\n'
        "  // keep this trailing comment exactly\n"
        "}\n"
    )


def test_apps_placement_does_not_create_or_rewrite_untouched_menu(menu_path: Path) -> None:
    entries = _Entries()

    _sync(entries, apps=[_app("paint", "Paint")], menu_path=menu_path, placement="apps")
    assert not menu_path.exists()

    menu_path.write_text('{\n  "a": {"label":"A"},\n}\n', encoding="utf-8")
    before = menu_path.stat().st_mtime_ns
    _sync(entries, apps=[_app("paint", "Paint")], menu_path=menu_path, placement="apps")
    assert menu_path.stat().st_mtime_ns == before
    assert menu_path.read_text(encoding="utf-8") == '{\n  "a": {"label":"A"},\n}\n'


def test_replaces_only_managed_block_and_preserves_user_content(menu_path: Path) -> None:
    entries = _Entries()
    prefix = (
        "{\n"
        '  "quick-chat": {"label":"Quick Chat","action":"quick-chat"},\n'
        "  // user comment must survive\n"
    )
    old_block = (
        f"  {BEGIN_MARKER}\n"
        '  "windows-apps": {"label":"Old"},\n'
        '  "windows-apps.old": {"label":"Old App","action":"false"},\n'
        f"  {END_MARKER}\n"
    )
    suffix = '  "personal": {"label":"Personal"},\n}\n'
    menu_path.write_text(prefix + old_block + suffix, encoding="utf-8")

    _sync(entries, apps=[_app("paint", "Paint")], menu_path=menu_path, placement="windows-apps")

    text = menu_path.read_text(encoding="utf-8")
    assert text.startswith(prefix)
    assert text.endswith(suffix)
    assert "Old App" not in text
    assert text.count(BEGIN_MARKER) == 1
    assert text.count(END_MARKER) == 1
    assert '"windows-apps.paint"' in text
    _parse_jsonc(text)


def test_placement_defaults_to_persisted_state(menu_path: Path) -> None:
    entries = _Entries()
    set_menu_placement("windows-apps")

    _sync(entries, apps=[_app("paint", "Paint")], menu_path=menu_path)

    assert BEGIN_MARKER in menu_path.read_text(encoding="utf-8")
    assert entries.installed == [("paint", True)]


def test_rejects_invalid_existing_jsonc_without_overwriting_it(menu_path: Path) -> None:
    entries = _Entries()
    original = "{ this is not jsonc }\n"
    menu_path.write_text(original, encoding="utf-8")

    with pytest.raises(ValueError):
        _sync(entries, apps=[_app("paint", "Paint")], menu_path=menu_path, placement="both")

    assert menu_path.read_text(encoding="utf-8") == original
    assert entries.installed == []


def test_rejects_incomplete_managed_block(menu_path: Path) -> None:
    entries = _Entries()
    menu_path.write_text("{\n" + f"  {BEGIN_MARKER}\n" + "}\n", encoding="utf-8")

    with pytest.raises(ValueError):
        _sync(entries, apps=[], menu_path=menu_path, placement="apps")


def test_rejects_unknown_placement(menu_path: Path) -> None:
    with pytest.raises(ValueError):
        _sync(_Entries(), apps=[], menu_path=menu_path, placement="dock")


def test_default_entry_writers_write_no_display_and_honor_hidden(
    monkeypatch: pytest.MonkeyPatch, menu_path: Path
) -> None:
    from wayseam.apps import AppInfo
    from wayseam.desktop import entries as entry
    from wayseam.paths import applications_dir

    monkeypatch.setattr(entry, "_wayseam_exe", lambda: "/usr/bin/wayseam")
    refreshed: list[int] = []
    monkeypatch.setattr(entry, "update_desktop_database", lambda: refreshed.append(1))
    visible = AppInfo(name="paint", full_name="Paint", executable="C:\\paint.exe")
    hidden = AppInfo(name="xbox", full_name="XBOX", executable="C:\\xbox.exe", hidden=True)
    entry.install_desktop_entry(hidden)
    hidden_path = applications_dir() / "wayseam-xbox.desktop"
    assert hidden_path.exists()

    sync_menu(apps=[visible, hidden], menu_path=menu_path, placement="windows-apps")

    content = (applications_dir() / "wayseam-paint.desktop").read_text(encoding="utf-8")
    assert "NoDisplay=true\n" in content
    assert "Exec=/usr/bin/wayseam run paint %u" in content
    assert not hidden_path.exists()
    assert refreshed == [1]

    sync_menu(apps=[visible], menu_path=menu_path, placement="apps")
    content = (applications_dir() / "wayseam-paint.desktop").read_text(encoding="utf-8")
    assert "NoDisplay" not in content


def test_parse_jsonc_accepts_omarchy_comments_and_trailing_commas() -> None:
    assert _parse_jsonc('{\n  // c\n  "a": {"b": 1,},\n}\n') == {"a": {"b": 1}}
    assert json.dumps(_parse_jsonc("{}")) == "{}"


class TestOmarchyMenuCli:
    def _parse(self, argv: list[str]) -> object:
        from wayseam.cli import _build_parser

        args = _build_parser().parse_args(argv[1:] if argv[:1] == ["omarchy"] else argv)
        args.omarchy_command = args.command
        return args

    def test_cli_exposes_omarchy_menu_sync(self) -> None:
        args = self._parse(["omarchy", "menu", "sync"])
        assert (args.command, args.omarchy_command, args.menu_command) == (
            "menu",
            "menu",
            "sync",
        )

    def test_cli_exposes_menu_placement(self) -> None:
        args = self._parse(["omarchy", "menu", "placement", "set", "both"])
        assert args.placement_command == "set"
        assert args.placement == "both"
        args = self._parse(["omarchy", "menu", "placement", "get"])
        assert args.placement_command == "get"
