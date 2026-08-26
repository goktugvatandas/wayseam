# SPDX-License-Identifier: MIT
"""Place Windows applications in the Omarchy menu without owning the user's menu.

Placement (persisted in ``omarchy.json``) decides where the apps show up:

* ``apps``         - only the shell's Apps list (per-app .desktop entries visible);
                     no managed block in ``omarchy-menu.jsonc``.
* ``windows-apps`` - only a managed top-level "Windows Apps" submenu; .desktop
                     entries carry ``NoDisplay=true`` so Apps does not list them.
* ``both``         - managed submenu and visible .desktop entries.
* ``none``         - neither (entries stay installed but hidden so file
                     associations keep working).

Omarchy 4's ``MenuModel.js`` only honours icon/label/action/target/provider/
aliases/when/checked/description, so every generated row is an ``action`` row
that launches the app's desktop entry exactly like the shell's own Apps list:
``uwsm-app -- gtk-launch wayseam-<slug>.desktop``.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Protocol

from wayseam.omarchy.state import PLACEMENTS, get_menu_placement

BEGIN_MARKER = "// BEGIN WAYSEAM WINDOWS APPS (managed)"
END_MARKER = "// END WAYSEAM WINDOWS APPS (managed)"
MENU_ICON = ""

_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")
_SAFE_SLUG_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


class _MenuApp(Protocol):
    name: str
    full_name: str
    hidden: bool


def default_menu_path() -> Path:
    """Return Omarchy's user-owned menu extension path."""
    return Path.home() / ".config" / "omarchy" / "extensions" / "omarchy-menu.jsonc"


def _parse_jsonc(text: str) -> object:
    """Parse the same whole-line comments/trailing commas Omarchy accepts."""
    uncommented = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("//")
    )
    return json.loads(_TRAILING_COMMA_RE.sub(r"\1", uncommented))


def _block_span(text: str) -> tuple[int, int] | None:
    """Return the [start, end) line-aligned span of the managed block, if any."""
    begin = text.find(BEGIN_MARKER)
    end = text.find(END_MARKER)
    if begin == -1 and end == -1:
        return None
    if (
        begin == -1
        or end == -1
        or end < begin
        or text.find(BEGIN_MARKER, begin + len(BEGIN_MARKER)) != -1
        or text.find(END_MARKER, end + len(END_MARKER)) != -1
    ):
        raise ValueError("Omarchy menu contains an incomplete Wayseam managed block")

    line_start = text.rfind("\n", 0, begin) + 1
    line_end = text.find("\n", end)
    line_end = len(text) if line_end == -1 else line_end + 1
    return line_start, line_end


def _replace_managed_block(text: str, block: str) -> str | None:
    span = _block_span(text)
    if span is None:
        return None
    line_start, line_end = span
    return text[:line_start] + block + text[line_end:]


def _remove_managed_block(text: str) -> str | None:
    """Drop the managed block (and the separator comma we inserted before it)."""
    span = _block_span(text)
    if span is None:
        return None
    line_start, line_end = span
    before = text[:line_start]
    previous_start = before.rfind("\n", 0, len(before) - 1) + 1 if before else 0
    if before[previous_start:].strip() == ",":
        before = before[:previous_start]
    return before + text[line_end:]


def visible_apps(apps: Iterable[_MenuApp]) -> list[_MenuApp]:
    """Return non-hidden apps with menu-safe slugs, sorted by display name."""
    return sorted(
        (app for app in apps if not app.hidden and _SAFE_SLUG_RE.match(app.name or "")),
        key=lambda app: ((app.full_name or app.name).casefold(), app.name.casefold()),
    )


def menu_action(slug: str) -> str:
    """Return the launch command the shell's own Apps list would use."""
    return f"uwsm-app -- gtk-launch wayseam-{slug}.desktop"


# Omarchy's menu draws JSON rows with a Nerd Font glyph (image icons exist
# only for its own desktop-entry rows), so give each Windows app the closest
# recognisable glyph instead of one generic symbol for the whole list.
_GLYPHS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("explorer", "file"), "\U000f0256"),      # 󰉖 folder
    (("settings", "control"), "\U000f0493"),   # 󰒓 cog
    (("notepad", "note", "text", "editor"), "\U000f039e"),  # 󰎞 note
    (("terminal", "powershell", "cmd", "prompt", "console"), "\U000f018d"),  # 󰆍 console
    (("calculator", "calc"), "\U000f00ec"),    # 󰃬 calculator
    (("edge", "chrome", "firefox", "browser", "internet"), "\U000f059f"),  # 󰖟 web
    (("mail", "outlook", "thunderbird"), "\U000f01ee"),  # 󰇮 email
    (("word", "writer", "docs", "document"), "\U000f0219"),  # 󰈙 document
    (("excel", "sheet", "calc"), "\U000f021b"),  # 󰈛 table
    (("powerpoint", "slides", "presentation"), "\U000f0227"),  # 󰈧 presentation
    (("photo", "affinity", "paint", "image", "designer", "gimp", "draw"), "\U000f02e9"),  # 󰋩 image
    (("music", "media", "player", "video", "vlc", "movies"), "\U000f0388"),  # 󰎈 play circle
    (("camera",), "\U000f0100"),               # 󰄀 camera
    (("clock", "alarm", "calendar"), "\U000f0150"),  # 󰅐 clock
    (("store", "shop"), "\U000f0110"),         # 󰄐 store
    (("game", "xbox", "steam"), "\U000f0297"),  # 󰊗 gamepad
    (("map",), "\U000f034d"),                  # 󰍍 map
    (("weather",), "\U000f0599"),              # 󰖙 weather
    (("phone",), "\U000f03f2"),                # 󰏲 phone
    (("security", "defender", "antivirus"), "\U000f0483"),  # 󰒃 shield
    (("disk", "cleanup", "defrag", "optimizer", "iscsi", "system", "tools", "admin"), "\U000f0493"),  # cog
    (("character",), "\U000f0a1b"),            # 󰨛 characters
)


def menu_glyph(app: _MenuApp) -> str:
    """Pick a Nerd Font glyph for an app from its slug/name; MENU_ICON otherwise."""
    haystack = f"{app.name} {app.full_name or ''}".casefold()
    for needles, glyph in _GLYPHS:
        if any(needle in haystack for needle in needles):
            return glyph
    return MENU_ICON


def _managed_block(visible: list[_MenuApp]) -> str:
    entries: list[tuple[str, dict[str, str]]] = [
        ("windows-apps", {"icon": MENU_ICON, "label": "Windows Apps"})
    ]
    for app in visible:
        entries.append(
            (
                f"windows-apps.{app.name}",
                {
                    "icon": menu_glyph(app),
                    "label": app.full_name or app.name,
                    "action": menu_action(app.name),
                },
            )
        )

    lines = [f"  {BEGIN_MARKER}"]
    for key, value in entries:
        lines.append(
            f"  {json.dumps(key, ensure_ascii=False)}: "
            f"{json.dumps(value, ensure_ascii=False, separators=(',', ': '))},"
        )
    lines.append(f"  {END_MARKER}")
    return "\n".join(lines) + "\n"


def _insert_managed_block(text: str, block: str) -> str:
    closing = text.rfind("}")
    if closing == -1:
        raise ValueError("Omarchy menu extension must be a JSON object")

    before = text[:closing]
    significant = "\n".join(
        line for line in before.splitlines() if not line.lstrip().startswith("//")
    ).rstrip()
    if not significant or significant[-1] not in "{,":
        if not before.endswith("\n"):
            before += "\n"
        before += "  ,\n"
    elif not before.endswith("\n"):
        before += "\n"
    return before + block + text[closing:]


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_mode = (path.stat().st_mode & 0o777) if path.exists() else 0o644
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, existing_mode)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


_LEGACY_MARKERS = (
    ("// BEGIN WINPODX WINDOWS APPS (managed)", BEGIN_MARKER),
    ("// END WINPODX WINDOWS APPS (managed)", END_MARKER),
)


def render_menu(text: str, visible: list[_MenuApp], *, submenu: bool) -> str:
    """Return ``text`` with the managed block present (submenu) or removed."""
    for legacy, current in _LEGACY_MARKERS:
        text = text.replace(legacy, current)
    parsed = _parse_jsonc(text)
    if not isinstance(parsed, dict):
        raise ValueError("Omarchy menu extension must be a JSON object")
    if submenu:
        block = _managed_block(visible)
        updated = _replace_managed_block(text, block)
        if updated is None:
            updated = _insert_managed_block(text, block)
    else:
        updated = _remove_managed_block(text)
        if updated is None:
            return text
    _parse_jsonc(updated)
    return updated


def _default_install_entry(app: Any, *, no_display: bool) -> None:
    from wayseam.desktop.entries import install_desktop_entry

    install_desktop_entry(app, no_display=no_display)


def _default_remove_entry(slug: str) -> None:
    from wayseam.desktop.entries import remove_desktop_entry
    from wayseam.paths import applications_dir

    if (applications_dir() / f"wayseam-{slug}.desktop").exists():
        remove_desktop_entry(slug)


def _default_refresh_database() -> None:
    from wayseam.desktop.entries import update_desktop_database

    update_desktop_database()


def sync_menu(
    *,
    apps: Iterable[_MenuApp] | None = None,
    menu_path: Path | None = None,
    placement: str | None = None,
    install_entry: Callable[..., Any] = _default_install_entry,
    remove_entry: Callable[[str], Any] = _default_remove_entry,
    refresh_database: Callable[[], Any] = _default_refresh_database,
) -> int:
    """Apply the menu placement and return the number of visible apps.

    The managed block is only touched inside its markers; everything else in
    the user's ``omarchy-menu.jsonc`` (entries, comments, ordering) survives.
    A malformed menu is rejected before anything is written.
    """
    placement = placement or get_menu_placement()
    if placement not in PLACEMENTS:
        raise ValueError(
            f"unknown menu placement {placement!r}; expected one of {', '.join(PLACEMENTS)}"
        )
    if apps is None:
        from wayseam.apps import list_available_apps

        apps = list_available_apps()
    apps = list(apps)
    visible = visible_apps(apps)
    submenu = placement in ("windows-apps", "both")
    no_display = placement in ("windows-apps", "none")

    path = menu_path or default_menu_path()
    original = path.read_text(encoding="utf-8") if path.exists() else "{\n}\n"
    updated = render_menu(original, visible, submenu=submenu)
    if updated != original and (submenu or path.exists()):
        _atomic_write(path, updated)

    for app in visible:
        install_entry(app, no_display=no_display)
    for app in apps:
        if app.hidden:
            remove_entry(app.name)
    refresh_database()
    return len(visible)
