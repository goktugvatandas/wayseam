# SPDX-License-Identifier: MIT
"""Omarchy backend for adopting the distribution-provided Windows VM.

Omarchy owns the compose file and VM storage.  Wayseam only reads that
configuration and uses Docker Compose for headless lifecycle operations; it
never rewrites the package/user-owned compose file or invokes the desktop RDP
launcher.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wayseam.config import Config
from wayseam.paths import config_dir, oem_dir
from wayseam.token import ensure_agent_token
from wayseam.vm.backend_docker import DockerBackend
from wayseam.vm.hostenv import host_env

DEFAULT_CONTAINER_NAME = "omarchy-windows"
DEFAULT_RDP_PORT = 3389
OEM_SERVE_DIR = "/tmp/wayseam-recover"
OEM_ARCHIVE_PATH = f"{OEM_SERVE_DIR}/oem.tar.gz"
OMARCHY_AGENT_HOST_PORT = 8767
OMARCHY_AGENT_GUEST_PORT = 8765
# Wayseam's shared-memory transport: a 64 MiB IVSHMEM PCI device backed by a
# file inside Omarchy's /shared bind mount, so the same bytes are guest RAM
# (PCI BAR2) and a host-mappable file. Cold-plugged via dockur's ARGUMENTS
# passthrough because q35's root bus rejects hotplug.
IVSHMEM_SIZE_MB = 64
IVSHMEM_FILE_NAME = "wayseam-ivshmem.bin"
IVSHMEM_GUEST_PATH = f"/shared/{IVSHMEM_FILE_NAME}"
DEFAULT_QEMU_ARGUMENTS = "-rtc base=localtime,clock=host,driftfix=slew"


class OmarchyConfigError(RuntimeError):
    """Raised when the installed Omarchy VM cannot be adopted safely."""


@dataclass(frozen=True)
class OmarchyInstallation:
    compose_file: Path
    username: str
    password: str
    container_name: str = DEFAULT_CONTAINER_NAME
    rdp_port: int = DEFAULT_RDP_PORT
    shared_directory: Path | None = None


def compose_file() -> Path:
    """Return Omarchy's compose path, with a test/operator override."""
    override = os.environ.get("WAYSEAM_OMARCHY_COMPOSE", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "windows" / "docker-compose.yml"


def _environment_map(value: Any) -> dict[str, str]:
    if isinstance(value, dict):
        return {str(key): str(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        result: dict[str, str] = {}
        for item in value:
            if not isinstance(item, str) or "=" not in item:
                continue
            key, raw = item.split("=", 1)
            result[key] = raw
        return result
    return {}


def _published_rdp_port(service: dict[str, Any]) -> int:
    for port in service.get("ports", []):
        if not isinstance(port, dict):
            continue
        if str(port.get("target", "")) != "3389":
            continue
        if str(port.get("protocol", "tcp")).lower() != "tcp":
            continue
        try:
            return int(port.get("published", DEFAULT_RDP_PORT))
        except (TypeError, ValueError):
            break
    return DEFAULT_RDP_PORT


def _shared_directory(service: dict[str, Any]) -> Path | None:
    for volume in service.get("volumes", []):
        if not isinstance(volume, dict) or volume.get("target") != "/shared":
            continue
        source = volume.get("source")
        if isinstance(source, str) and source:
            return Path(source).expanduser()
    return None


def inspect_installation(path: Path | None = None) -> OmarchyInstallation:
    """Read the resolved Omarchy Compose model without printing secrets."""
    selected = path or compose_file()
    if not selected.is_file():
        raise OmarchyConfigError(f"Omarchy Windows compose file not found: {selected}")

    try:
        result = subprocess.run(
            ["docker", "compose", "-f", str(selected), "config", "--format", "json"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
            env=host_env(),
        )
    except FileNotFoundError as exc:
        raise OmarchyConfigError("Docker is required to read the Omarchy Windows VM") from exc
    except subprocess.TimeoutExpired as exc:
        raise OmarchyConfigError(
            "Timed out while reading the Omarchy compose configuration"
        ) from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "docker compose config failed").strip()
        raise OmarchyConfigError(detail) from exc

    try:
        model = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise OmarchyConfigError("Docker returned invalid Compose JSON") from exc

    services = model.get("services", {}) if isinstance(model, dict) else {}
    if not isinstance(services, dict) or not services:
        raise OmarchyConfigError("Omarchy compose configuration has no services")
    service = services.get("windows")
    if not isinstance(service, dict):
        service = next(
            (
                candidate
                for candidate in services.values()
                if isinstance(candidate, dict)
                and candidate.get("container_name") == DEFAULT_CONTAINER_NAME
            ),
            None,
        )
    if not isinstance(service, dict):
        raise OmarchyConfigError("Could not find the Omarchy Windows service")

    environment = _environment_map(service.get("environment"))
    username = environment.get("USERNAME", "").strip()
    password = environment.get("PASSWORD", "")
    if not username or not password:
        raise OmarchyConfigError("Omarchy compose configuration has no USERNAME/PASSWORD")

    return OmarchyInstallation(
        compose_file=selected,
        username=username,
        password=password,
        container_name=str(service.get("container_name") or DEFAULT_CONTAINER_NAME),
        rdp_port=_published_rdp_port(service),
        shared_directory=_shared_directory(service),
    )


def adopt_config(cfg: Config, installation: OmarchyInstallation) -> None:
    """Apply an inspected Omarchy installation to a Wayseam config."""
    cfg.pod.backend = "omarchy"
    cfg.pod.container_name = installation.container_name
    cfg.pod.agent_port = OMARCHY_AGENT_HOST_PORT
    cfg.rdp.ip = "127.0.0.1"
    cfg.rdp.port = installation.rdp_port
    cfg.rdp.user = installation.username
    cfg.rdp.password = installation.password
    cfg.rdp.password_max_age = 0
    if installation.shared_directory is not None:
        cfg.pod.home_share = str(installation.shared_directory)
    cfg.rdp.__post_init__()
    cfg.pod.__post_init__()


class OmarchyBackend(DockerBackend):
    """Headless lifecycle adapter over Omarchy's existing compose project."""

    def _compose_file(self) -> str:
        return str(compose_file())

    def _compose_cmd(self) -> list[str]:
        command = ["docker", "compose", "-f", self._compose_file()]
        override = compose_override_file()
        if override.is_file():
            command.extend(["-f", str(override)])
        return command


def compose_override_file() -> Path:
    """Project-owned Compose additions for the adopted Omarchy VM."""
    return config_dir() / "omarchy-compose.override.yaml"


def _base_qemu_arguments(compose_path: Path | None = None) -> str:
    """Return the ARGUMENTS value from Omarchy's own compose file.

    Compose merges ``environment`` per key, so our override *replaces*
    ``ARGUMENTS`` — it must therefore carry Omarchy's original value too.
    The Omarchy installer writes the key in a fixed, single-line shape; if it
    is absent or unreadable, fall back to the value Omarchy has always used.
    """
    selected = compose_path or compose_file()
    try:
        text = selected.read_text(encoding="utf-8")
    except OSError:
        return DEFAULT_QEMU_ARGUMENTS
    match = re.search(r"^\s*ARGUMENTS:\s*\"([^\"]*)\"\s*$", text, re.MULTILINE)
    return match.group(1) if match else DEFAULT_QEMU_ARGUMENTS


def ivshmem_backing_file(cfg: Config) -> Path:
    """Host path of the IVSHMEM backing file inside the shared bind mount."""
    return Path(cfg.pod.home_share or str(Path.home() / "Windows")).expanduser() / (
        IVSHMEM_FILE_NAME
    )


def ensure_ivshmem_backing(cfg: Config) -> Path:
    """Create (or right-size) the private shared-memory backing file."""
    backing = ivshmem_backing_file(cfg)
    backing.parent.mkdir(parents=True, exist_ok=True)
    size = IVSHMEM_SIZE_MB * 1024 * 1024
    if not backing.exists() or backing.stat().st_size != size:
        with backing.open("a+b") as handle:
            handle.truncate(size)
    backing.chmod(0o600)
    return backing


def ensure_compose_override(cfg: Config) -> Path:
    """Atomically add Wayseam's agent forwarding, audio, and IVSHMEM device.

    The distribution-owned Compose file remains untouched. The override is
    deliberately tiny and can be removed without affecting the VM disk. The
    IVSHMEM device takes effect at the next VM start (cold plug only).
    """
    if cfg.pod.backend != "omarchy":
        raise ValueError("Compose override is only valid for the Omarchy backend")

    ensure_ivshmem_backing(cfg)
    arguments = (
        f"{_base_qemu_arguments()} "
        f"-object memory-backend-file,id=wayseam-shm,size={IVSHMEM_SIZE_MB}M,"
        f"mem-path={IVSHMEM_GUEST_PATH},share=on "
        "-device ivshmem-plain,id=wayseam-ivshmem,memdev=wayseam-shm"
    )
    path = compose_override_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "services:\n"
        "  windows:\n"
        "    environment:\n"
        f'      USER_PORTS: "{OMARCHY_AGENT_GUEST_PORT}"\n'
        '      AUDIO: "Y"\n'
        f'      ARGUMENTS: "{arguments}"\n'
        "    ports:\n"
        f'      - "127.0.0.1:{cfg.pod.agent_port}:{OMARCHY_AGENT_GUEST_PORT}/tcp"\n'
    )
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        return path
    finally:
        temporary.unlink(missing_ok=True)


def stage_oem_archive(cfg: Config) -> str:
    """Stage a self-contained Wayseam OEM archive in the Omarchy container.

    Omarchy's compose project does not mount Wayseam's ``config/oem`` tree.
    Build a private temporary archive instead, inject the live agent token in
    memory, copy only that archive into the running container, and remove the
    host temporary file. The source checkout and Omarchy compose file remain
    untouched.
    """
    if cfg.pod.backend != "omarchy":
        raise ValueError("OEM archive staging is only valid for the Omarchy backend")

    source = oem_dir()
    if not (source / "install.bat").is_file():
        raise OmarchyConfigError(f"Wayseam OEM bundle is incomplete: {source}")

    token = ensure_agent_token().encode("ascii")
    fd, temporary_name = tempfile.mkstemp(prefix="wayseam-omarchy-oem-", suffix=".tar.gz")
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        os.close(fd)
        with tarfile.open(temporary, "w:gz") as archive:
            root_info = tarfile.TarInfo("oem")
            root_info.type = tarfile.DIRTYPE
            root_info.mode = 0o755
            archive.addfile(root_info)
            for child in sorted(source.iterdir(), key=lambda path: path.name):
                if child.name == "agent_token.txt":
                    continue
                archive.add(child, arcname=f"oem/{child.name}", recursive=True)

            token_info = tarfile.TarInfo("oem/agent_token.txt")
            token_info.mode = 0o600
            token_info.size = len(token)
            archive.addfile(token_info, io.BytesIO(token))

        container = cfg.pod.container_name
        common = {"check": True, "capture_output": True, "text": True, "env": host_env()}
        subprocess.run(
            ["docker", "exec", container, "sh", "-c", f"mkdir -p {OEM_SERVE_DIR}"],
            timeout=10,
            **common,
        )
        subprocess.run(
            ["docker", "cp", str(temporary), f"{container}:{OEM_ARCHIVE_PATH}"],
            timeout=60,
            **common,
        )
        subprocess.run(
            ["docker", "exec", container, "sh", "-c", f"test -s {OEM_ARCHIVE_PATH}"],
            timeout=10,
            **common,
        )
        return OEM_ARCHIVE_PATH
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise OmarchyConfigError("Could not stage the Wayseam OEM archive") from exc
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
