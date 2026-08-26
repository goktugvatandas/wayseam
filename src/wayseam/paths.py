# SPDX-License-Identifier: MIT
"""XDG path management for Wayseam."""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "wayseam"

# Directories shipped at the repo root (next to ``src/``) that the runtime
# needs regardless of install mode.
_BUNDLE_MARKERS = ("guest", "scripts")


def bundle_dir() -> Path:
    """Return the directory containing ``guest/`` and ``scripts/``.

    Search order: ``$WAYSEAM_BUNDLE_DIR`` (packaging wrappers), the source
    checkout derived from this file, then ``sys.prefix/share/wayseam``.
    """
    env = os.environ.get("WAYSEAM_BUNDLE_DIR")
    src_guess = Path(__file__).resolve().parents[2]
    candidates = [
        Path(env) if env else None,
        src_guess,
        Path(sys.prefix) / "share" / APP_NAME,
    ]
    for candidate in candidates:
        if candidate is not None and all((candidate / m).is_dir() for m in _BUNDLE_MARKERS):
            return candidate
    return src_guess


def oem_dir() -> Path:
    """Guest OEM payload (agent, install.bat, helpers) staged into the VM."""
    return bundle_dir() / "guest" / "oem"


def guest_scripts_dir() -> Path:
    """PowerShell scripts executed in the guest through the agent."""
    return bundle_dir() / "guest" / "scripts"


def config_dir() -> Path:
    """~/.config/wayseam/"""
    base = os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
    return Path(base) / APP_NAME


def data_dir() -> Path:
    """~/.local/share/wayseam/"""
    base = os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")
    return Path(base) / APP_NAME


def runtime_dir() -> Path:
    """Runtime dir for PID files, claims, and logs."""
    base = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return Path(base) / APP_NAME


def applications_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")
    return Path(base) / "applications"


def icons_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")
    return Path(base) / "icons" / "hicolor"
