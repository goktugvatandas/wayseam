# SPDX-License-Identifier: MIT
"""Icon installation and cache management."""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path

from wayseam.paths import bundle_dir, icons_dir

log = logging.getLogger(__name__)


def bundled_data_path(*parts: str) -> Path | None:
    """Resolve a file under the bundled ``data/`` tree."""
    for base in (bundle_dir() / "data",):
        candidate = base.joinpath(*parts)
        if not candidate.exists():
            continue
        # Symlink escape guard: prevent leaking files outside the data dir via copy.
        try:
            resolved = candidate.resolve(strict=True)
            base_resolved = base.resolve(strict=True)
        except (OSError, RuntimeError):
            log.warning("Rejecting unresolvable data candidate: %s", candidate)
            continue
        if not resolved.is_relative_to(base_resolved):
            log.warning(
                "Rejecting symlink escape in data candidate: %s -> %s",
                candidate,
                resolved,
            )
            continue
        return candidate
    return None


def install_wayseam_icon() -> bool:
    """Install the Wayseam project icon (all bundled hicolor sizes) for the user."""
    root = bundled_data_path("icons", "hicolor")
    if root is None:
        log.warning("Bundled icon set not found in any known data location")
        return False
    installed = 0
    for src in sorted(root.glob("*/apps/wayseam.png")):
        if src.is_symlink():
            continue
        size_dir = src.parent.parent.name  # e.g. "48x48"
        dest_dir = icons_dir() / size_dir / "apps"
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest_dir / "wayseam.png", follow_symlinks=False)
        installed += 1
    if installed:
        refresh_icon_cache()
        log.info("Installed wayseam icon in %d sizes", installed)
    return installed > 0


def install_gui_launcher_desktop() -> bool:
    """Install the wayseam GUI launcher ``.desktop`` into ~/.local/share/applications.

    install.sh handles this for curl installs; deb / rpm / aur ship it under
    /usr/share/applications. ``wayseam setup`` invoked manually (pip, dev
    checkout, or a curl install where the launcher copy got lost) needs to
    register it explicitly so the GUI shows up in the app menu without
    re-running install.sh.

    Skips when /usr/share/applications/wayseam.desktop already exists -- that
    means a package install owns it, and dropping a user-level copy would
    shadow the package version with a stale Exec= path on later upgrades.
    """
    if Path("/usr/share/applications/wayseam.desktop").is_file():
        log.debug("System wayseam.desktop already present; skipping user copy")
        return False

    src = bundled_data_path("wayseam.desktop")
    if src is None:
        log.warning("Bundled wayseam.desktop not found")
        return False

    if src.is_symlink():
        log.warning("Refusing to install .desktop from symlink: %s", src)
        return False

    dest_dir = Path.home() / ".local" / "share" / "applications"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "wayseam.desktop"

    shutil.copy2(src, dest, follow_symlinks=False)
    log.info("Installed wayseam GUI launcher: %s", dest)
    return True


def _ensure_index_theme(icon_dir: Path) -> None:
    """Ensure index.theme exists so gtk cache and KDE Plasma can discover icons."""
    index = icon_dir / "index.theme"
    if index.exists():
        return

    system_index = Path("/usr/share/icons/hicolor/index.theme")
    if system_index.exists():
        icon_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(system_index, index)
        log.info("Copied system index.theme to %s", index)
        return

    icon_dir.mkdir(parents=True, exist_ok=True)
    index.write_text(
        "[Icon Theme]\n"
        "Name=Hicolor\n"
        "Comment=Fallback icon theme\n"
        "Hidden=true\n"
        "Directories=scalable/apps\n"
        "\n"
        "[scalable/apps]\n"
        "Size=64\n"
        "MinSize=1\n"
        "MaxSize=512\n"
        "Context=Applications\n"
        "Type=Scalable\n",
        encoding="utf-8",
    )
    log.info("Created minimal index.theme at %s", index)


def _register_fixed_app_dirs(icon_dir: Path) -> None:
    """Register exact-size app directories created from Windows resources."""
    index = icon_dir / "index.theme"
    try:
        text = index.read_text(encoding="utf-8")
    except OSError:
        return

    discovered: list[tuple[str, int]] = []
    for path in icon_dir.glob("*x*/apps"):
        match = re.fullmatch(r"(\d+)x(\d+)/apps", path.relative_to(icon_dir).as_posix())
        if not match or match.group(1) != match.group(2):
            continue
        discovered.append((path.relative_to(icon_dir).as_posix(), int(match.group(1))))
    if not discovered:
        return

    directory_match = re.search(r"(?m)^Directories=(.*)$", text)
    if directory_match is None:
        return
    listed = directory_match.group(1).split(",")
    additions = [
        name
        for name, _size in sorted(discovered, key=lambda item: item[1])
        if name not in listed
    ]
    sections: list[str] = []
    for name, size in sorted(discovered, key=lambda item: item[1]):
        if f"[{name}]" not in text:
            sections.append(
                f"[{name}]\nSize={size}\nContext=Applications\nType=Fixed\n"
            )
    if not additions and not sections:
        return

    if additions:
        replacement = "Directories=" + ",".join(listed + additions)
        text = text[: directory_match.start()] + replacement + text[directory_match.end() :]
    if sections:
        text = text.rstrip() + "\n\n" + "\n".join(sections)
    index.write_text(text, encoding="utf-8")


def refresh_icon_cache() -> None:
    """Refresh the system icon cache after installing one or more icons.

    Safe to call once after a batch of icon installs (e.g. after
    ``persist_discovered`` has written N app icons). Runs the gtk-update-icon-cache,
    xdg-icon-resource, and Plasma sycoca rebuild steps in sequence; each is
    bounded by a 30s timeout. Missing tools are skipped.

    For single-icon workflows, this is also safe to call per icon, but callers
    installing many icons at once should invoke this exactly once at the end
    of the batch to avoid redundant cache rebuilds.
    """
    _do_refresh_icon_cache()


def update_icon_cache() -> None:
    """Backward-compatible alias for :func:`refresh_icon_cache`."""
    _do_refresh_icon_cache()


def _do_refresh_icon_cache() -> None:
    icon_dir = Path.home() / ".local/share/icons/hicolor"
    _ensure_index_theme(icon_dir)
    _register_fixed_app_dirs(icon_dir)
    try:
        result = subprocess.run(
            ["gtk-update-icon-cache", "-f", "-t", str(icon_dir)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            log.debug("gtk-update-icon-cache: %s", result.stderr.strip())
    except FileNotFoundError:
        log.debug("gtk-update-icon-cache not found, skipping")
    except subprocess.TimeoutExpired:
        log.warning("gtk-update-icon-cache timed out after 30s (corrupt cache?)")

    try:
        result = subprocess.run(
            ["xdg-icon-resource", "forceupdate"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            log.warning("xdg-icon-resource failed: %s", result.stderr.strip())
    except FileNotFoundError:
        log.debug("xdg-icon-resource not found, skipping")
    except subprocess.TimeoutExpired:
        log.warning("xdg-icon-resource forceupdate timed out after 30s")

    # KDE Plasma sycoca rebuild; surface failures at debug/warning for diagnosis.
    for cmd in ("kbuildsycoca6", "kbuildsycoca5"):
        if shutil.which(cmd):
            try:
                result = subprocess.run(
                    [cmd, "--noincremental"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if result.returncode != 0:
                    log.warning(
                        "%s exited %d: %s",
                        cmd,
                        result.returncode,
                        result.stderr.strip(),
                    )
            except FileNotFoundError:
                log.debug("%s not found after shutil.which - race or PATH change", cmd)
            except subprocess.TimeoutExpired:
                log.warning("%s timed out after 30s (sycoca rebuild stuck?)", cmd)
            break
