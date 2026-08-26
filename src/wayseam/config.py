# SPDX-License-Identifier: MIT
"""Wayseam configuration.

A deliberately small TOML config: the VM connection (``rdp``) and the pod
(``pod``). Ported from Wayseam's ``core.config`` (see upstream/PROVENANCE)
with everything Wayseam does not use removed.
"""

from __future__ import annotations

import logging
import os
import tempfile
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from wayseam.paths import config_dir
from wayseam.tomlio import dumps as toml_dumps

SCHEMA_VERSION = 1
log = logging.getLogger(__name__)


@dataclass
class RDPConfig:
    """How the host reaches the Windows VM (also used by Desktop Mode)."""

    user: str = ""
    password: str = ""
    password_updated: str = ""
    password_max_age: int = 7
    askpass: str = ""
    domain: str = ""
    ip: str = "127.0.0.1"
    port: int = 3389
    scale: int = 100
    dpi: int = 0
    extra_flags: str = ""
    freerdp_source: str = "auto"
    multimon: str = "span"

    def __post_init__(self) -> None:
        self.ip = (self.ip or "127.0.0.1").strip()
        self.port = int(self.port) if 0 < int(self.port) < 65536 else 3389
        self.scale = int(self.scale) if int(self.scale) in (100, 140, 180) else 100
        self.multimon = self.multimon if self.multimon in ("span", "multimon", "off") else "span"


@dataclass
class PodConfig:
    """The Omarchy-managed Windows container."""

    backend: str = "omarchy"
    vm_name: str = "RDPWindows"
    container_name: str = "omarchy-windows"
    win_version: str = "11"
    cpu_cores: int = 4
    ram_gb: int = 6
    vnc_port: int = 8006
    agent_port: int = 8765
    auto_start: bool = False
    idle_timeout: int = 0
    boot_timeout: int = 600
    home_share: str = ""
    image: str = ""

    def __post_init__(self) -> None:
        self.backend = self.backend if self.backend in ("omarchy", "docker", "manual") else "omarchy"
        self.boot_timeout = max(60, int(self.boot_timeout))


def _apply(target: Any, data: Any) -> None:
    if not isinstance(data, dict):
        return
    names = {f.name for f in fields(target)}
    for key, value in data.items():
        if key in names:
            setattr(target, key, value)


@dataclass
class Config:
    schema_version: int = SCHEMA_VERSION
    rdp: RDPConfig = field(default_factory=RDPConfig)
    pod: PodConfig = field(default_factory=PodConfig)

    @classmethod
    def path(cls) -> Path:
        return config_dir() / "wayseam.toml"

    @classmethod
    def load(cls) -> Config:
        """Load the config, falling back to defaults."""
        cfg = cls()
        path = cls.path()
        if not path.exists():
            return cfg
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError, PermissionError) as exc:
            log.warning("Corrupted config %s, using defaults: %s", path, exc)
            return cfg
        _apply(cfg.rdp, data.get("rdp", {}))
        _apply(cfg.pod, data.get("pod", {}))
        cfg.rdp.__post_init__()
        cfg.pod.__post_init__()
        return cfg

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": int(self.schema_version),
            "rdp": {f.name: getattr(self.rdp, f.name) for f in fields(self.rdp)},
            "pod": {f.name: getattr(self.pod, f.name) for f in fields(self.pod)},
        }

    def save(self) -> None:
        """Write the config atomically with 0600 permissions (it holds a password)."""
        path = self.path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".wayseam-", suffix=".toml")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(toml_dumps(self.to_dict()))
            os.replace(tmp, path)
        finally:
            Path(tmp).unlink(missing_ok=True)
