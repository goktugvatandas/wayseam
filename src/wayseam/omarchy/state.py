# SPDX-License-Identifier: MIT
"""Persisted Omarchy integration state (presentation mode, menu placement).

The state lives in ``~/.config/wayseam/omarchy.json``. It is tiny, written
atomically with mode 0600, and every reader tolerates a missing or corrupt
file by falling back to defaults (Wayseam Mode, apps placement).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Final

from wayseam.paths import config_dir

SCHEMA: Final = 1
MODES: Final = ("wayseam", "desktop")
PLACEMENTS: Final = ("apps", "windows-apps", "both", "none")
DEFAULT_MODE: Final = "wayseam"
DEFAULT_PLACEMENT: Final = "apps"


def state_path() -> Path:
    """Return the state file path (``~/.config/wayseam/omarchy.json``)."""
    return config_dir() / "omarchy.json"


def default_state() -> dict[str, object]:
    return {"schema": SCHEMA, "mode": DEFAULT_MODE, "menu_placement": DEFAULT_PLACEMENT}


def _normalize(raw: object) -> dict[str, object]:
    state = default_state()
    if not isinstance(raw, dict):
        return state
    mode = raw.get("mode")
    if isinstance(mode, str) and mode in MODES:
        state["mode"] = mode
    placement = raw.get("menu_placement")
    if isinstance(placement, str) and placement in PLACEMENTS:
        state["menu_placement"] = placement
    return state


def load_state(path: Path | None = None) -> dict[str, object]:
    """Load the persisted state, falling back to defaults on any problem."""
    target = path or state_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return default_state()
    return _normalize(raw)


def save_state(state: dict[str, object], path: Path | None = None) -> dict[str, object]:
    """Atomically persist ``state`` (validated) with mode 0600 and return it."""
    target = path or state_path()
    normalized = _normalize(state)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(normalized, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return normalized


def get_mode(path: Path | None = None) -> str:
    return str(load_state(path)["mode"])


def set_mode(mode: str, path: Path | None = None) -> dict[str, object]:
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; expected one of {', '.join(MODES)}")
    state = load_state(path)
    state["mode"] = mode
    return save_state(state, path)


def get_menu_placement(path: Path | None = None) -> str:
    return str(load_state(path)["menu_placement"])


def set_menu_placement(placement: str, path: Path | None = None) -> dict[str, object]:
    if placement not in PLACEMENTS:
        raise ValueError(
            f"unknown menu placement {placement!r}; expected one of {', '.join(PLACEMENTS)}"
        )
    state = load_state(path)
    state["menu_placement"] = placement
    return save_state(state, path)
