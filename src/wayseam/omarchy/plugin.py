# SPDX-License-Identifier: MIT
"""Install the Wayseam applet into the Omarchy shell as a symlinked plugin."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

PLUGIN_ID = "goktugvatandas.wayseam"
_SHELL_TIMEOUT = 10
_ENABLE_TIMEOUT = 20


class PluginError(RuntimeError):
    """Raised when the plugin cannot be linked into the Omarchy plugin dir."""


def plugin_source_dir() -> Path:
    """Return the package-shipped plugin directory (``wayseam/omarchy/plugin``)."""
    import wayseam.omarchy

    return Path(wayseam.omarchy.__file__).resolve().parent / "plugin"


def plugins_dir() -> Path:
    return Path.home() / ".config" / "omarchy" / "plugins"


def plugin_link_path() -> Path:
    return plugins_dir() / PLUGIN_ID


def _run_tool(
    cmd: list[str],
    *,
    timeout: float,
    run: Callable[..., Any] = subprocess.run,
) -> tuple[bool, str]:
    """Run an Omarchy helper, tolerating a missing binary or a hung shell."""
    try:
        completed = run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError:
        return False, f"{cmd[0]} is not installed"
    except (subprocess.SubprocessError, OSError) as exc:
        return False, f"{cmd[0]} failed: {exc}"
    output = (completed.stdout or "").strip()
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip() or output or f"exit {completed.returncode}"
        return False, f"{' '.join(cmd)}: {detail}"
    return True, output


def _link_target(link: Path) -> Path | None:
    try:
        return Path(os.readlink(link))
    except OSError:
        return None


def _points_to(link: Path, source: Path) -> bool:
    target = _link_target(link)
    if target is None:
        return False
    try:
        return target.resolve() == source.resolve()
    except OSError:
        return False


def ensure_plugin_link(source: Path | None = None, link: Path | None = None) -> bool:
    """Create or repair the plugin symlink. Returns True when it was changed."""
    source = source or plugin_source_dir()
    link = link or plugin_link_path()
    if not source.is_dir():
        raise PluginError(f"plugin source directory does not exist yet: {source}")
    if link.is_symlink():
        if _points_to(link, source):
            return False
        link.unlink()
    elif link.exists():
        raise PluginError(f"{link} exists and is not a symlink; remove it first")
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(str(source), str(link))
    return True


def install_plugin(
    *,
    source: Path | None = None,
    link: Path | None = None,
    run: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """Link the plugin, rescan the shell's plugins and enable the applet."""
    source = source or plugin_source_dir()
    link = link or plugin_link_path()
    changed = ensure_plugin_link(source, link)
    result: dict[str, Any] = {
        "id": PLUGIN_ID,
        "link": str(link),
        "source": str(source),
        "link_changed": changed,
        "rescanned": False,
        "enabled": False,
        "warnings": [],
    }
    ok, detail = _run_tool(
        ["omarchy-shell", "shell", "rescanPlugins"], timeout=_SHELL_TIMEOUT, run=run
    )
    result["rescanned"] = ok
    if not ok:
        result["warnings"].append(detail)
    ok, detail = _run_tool(
        ["omarchy-plugin-enable", PLUGIN_ID], timeout=_ENABLE_TIMEOUT, run=run
    )
    result["enabled"] = ok
    if not ok:
        result["warnings"].append(detail)
    return result


def _parse_plugin_list(output: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(output)
    except ValueError:
        return []
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def plugin_status(
    *,
    source: Path | None = None,
    link: Path | None = None,
    run: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """Report the symlink state and whether the shell lists the plugin."""
    source = source or plugin_source_dir()
    link = link or plugin_link_path()
    target = _link_target(link)
    result: dict[str, Any] = {
        "id": PLUGIN_ID,
        "link": str(link),
        "source": str(source),
        "source_exists": source.is_dir(),
        "linked": link.is_symlink() and _points_to(link, source),
        "target": str(target) if target is not None else None,
        "listed": None,
        "enabled": None,
        "warnings": [],
    }
    ok, detail = _run_tool(
        ["omarchy-shell", "shell", "listPlugins"], timeout=_SHELL_TIMEOUT, run=run
    )
    if not ok:
        result["warnings"].append(detail)
        return result
    result["listed"] = False
    for item in _parse_plugin_list(detail):
        if item.get("id") == PLUGIN_ID:
            result["listed"] = True
            result["enabled"] = bool(item.get("enabled", False))
            break
    return result
