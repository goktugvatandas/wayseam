# SPDX-License-Identifier: MIT
"""Pod lifecycle: state types, backend factory, start/stop/status.

Ported from Wayseam ``core.pod`` (see upstream/PROVENANCE), reduced to the
Omarchy container backend.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from wayseam.config import Config
from wayseam.vm.backend_base import Backend
from wayseam.vm.health import check_rdp_port

log = logging.getLogger(__name__)
_UNRESPONSIVE_UPTIME_FLOOR_SECS = 180


class PodState(Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    UNRESPONSIVE = "unresponsive"
    ERROR = "error"


@dataclass
class PodStatus:
    state: PodState
    ip: str = ""
    uptime: str = ""
    cpu_usage: str = ""
    memory_usage: str = ""
    error: str = ""


def get_backend(cfg: Config) -> Backend:
    name = cfg.pod.backend
    if name == "omarchy":
        from wayseam.vm.backend import OmarchyBackend

        return OmarchyBackend(cfg)
    if name == "docker":
        from wayseam.vm.backend_docker import DockerBackend

        return DockerBackend(cfg)
    raise ValueError(f"Unsupported backend: {name}")


def pod_status(cfg: Config) -> PodStatus:
    backend = get_backend(cfg)
    try:
        running = backend.is_running()
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to query pod status: %s", exc)
        return PodStatus(state=PodState.ERROR, error=str(exc))
    if not running:
        return PodStatus(state=PodState.STOPPED)
    try:
        if backend.is_paused():
            return PodStatus(state=PodState.PAUSED, ip=cfg.rdp.ip)
    except Exception as exc:  # noqa: BLE001
        log.debug("is_paused probe failed: %s", exc)
    if check_rdp_port(cfg.rdp.ip, cfg.rdp.port):
        return PodStatus(state=PodState.RUNNING, ip=cfg.rdp.ip)
    uptime = None
    try:
        uptime = backend.uptime_secs()
    except Exception as exc:  # noqa: BLE001
        log.debug("uptime_secs probe failed: %s", exc)
    if uptime is not None and uptime >= _UNRESPONSIVE_UPTIME_FLOOR_SECS:
        return PodStatus(state=PodState.UNRESPONSIVE, ip=cfg.rdp.ip)
    return PodStatus(state=PodState.STARTING, ip=cfg.rdp.ip)


def start_pod(cfg: Config) -> PodStatus:
    """Start the Windows pod and wait up to ``boot_timeout`` for RDP readiness."""
    backend = get_backend(cfg)
    try:
        backend.start()
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to start pod: %s", exc)
        return PodStatus(state=PodState.ERROR, error=str(exc))
    try:
        ready = backend.wait_for_ready(timeout=cfg.pod.boot_timeout)
    except Exception as exc:  # noqa: BLE001
        log.error("wait_for_ready failed: %s", exc)
        return PodStatus(state=PodState.ERROR, error=str(exc))
    state = PodState.RUNNING if ready else PodState.STARTING
    return PodStatus(state=state, ip=cfg.rdp.ip)


def stop_pod(cfg: Config) -> PodStatus:
    backend = get_backend(cfg)
    try:
        backend.stop()
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to stop pod: %s", exc)
        return PodStatus(state=PodState.ERROR, error=str(exc))
    return PodStatus(state=PodState.STOPPED)
