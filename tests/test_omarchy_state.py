# SPDX-License-Identifier: MIT
from __future__ import annotations

import json
import stat

import pytest

from wayseam.omarchy import state as omarchy_state
from wayseam.omarchy.state import (
    get_menu_placement,
    get_mode,
    load_state,
    save_state,
    set_menu_placement,
    set_mode,
    state_path,
)


def test_defaults_when_file_missing() -> None:
    assert not state_path().exists()
    assert load_state() == {"schema": 1, "mode": "wayseam", "menu_placement": "apps"}
    assert get_mode() == "wayseam"
    assert get_menu_placement() == "apps"


def test_state_path_lives_under_wayseam_config_dir() -> None:
    assert state_path().name == "omarchy.json"
    assert state_path().parent.name == "wayseam"


def test_save_state_is_private_and_json() -> None:
    saved = save_state({"mode": "desktop", "menu_placement": "both"})

    path = state_path()
    assert saved == {"schema": 1, "mode": "desktop", "menu_placement": "both"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text(encoding="utf-8")) == saved
    assert not list(path.parent.glob(".omarchy.json.*")), "temp file leaked"


def test_corrupt_or_foreign_values_fall_back_to_defaults() -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    path.write_text("{not json", encoding="utf-8")
    assert load_state() == omarchy_state.default_state()

    path.write_text(json.dumps([1, 2]), encoding="utf-8")
    assert load_state() == omarchy_state.default_state()

    path.write_text(
        json.dumps({"schema": 1, "mode": "coherence", "menu_placement": "everywhere"}),
        encoding="utf-8",
    )
    assert load_state() == omarchy_state.default_state()


def test_setters_round_trip_and_preserve_the_other_field() -> None:
    set_mode("desktop")
    assert get_mode() == "desktop"
    assert get_menu_placement() == "apps"

    set_menu_placement("windows-apps")
    assert get_menu_placement() == "windows-apps"
    assert get_mode() == "desktop"

    set_mode("wayseam")
    assert load_state() == {"schema": 1, "mode": "wayseam", "menu_placement": "windows-apps"}


def test_setters_reject_unknown_values_without_writing() -> None:
    with pytest.raises(ValueError):
        set_mode("rail")
    with pytest.raises(ValueError):
        set_menu_placement("sidebar")
    assert not state_path().exists()
