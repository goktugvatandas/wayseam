# SPDX-License-Identifier: MIT
"""Tests for adopting Omarchy's distribution-provided Windows VM."""

from __future__ import annotations

import json
import subprocess

import pytest

from wayseam.config import Config
from wayseam.vm.backend import OmarchyConfigError, adopt_config, inspect_installation


def _completed(model: dict) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, stdout=json.dumps(model), stderr="")


def test_inspect_installation_reads_resolved_compose(monkeypatch, tmp_path):
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services: {}\n")
    shared = tmp_path / "Windows"
    model = {
        "services": {
            "windows": {
                "container_name": "omarchy-windows",
                "environment": {"USERNAME": "Docker", "PASSWORD": "secret"},
                "ports": [
                    {"target": 3389, "published": "3389", "protocol": "tcp"},
                    {"target": 8006, "published": "8006", "protocol": "tcp"},
                ],
                "volumes": [{"type": "bind", "source": str(shared), "target": "/shared"}],
            }
        }
    }
    monkeypatch.setattr("wayseam.vm.backend.subprocess.run", lambda *a, **k: _completed(model))

    installation = inspect_installation(compose)

    assert installation.username == "Docker"
    assert installation.password == "secret"
    assert installation.rdp_port == 3389
    assert installation.shared_directory == shared


def test_adopt_config_uses_omarchy_vm_without_password_rotation(monkeypatch, tmp_path):
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services: {}\n")
    shared = tmp_path / "Windows"
    model = {
        "services": {
            "windows": {
                "container_name": "custom-omarchy-windows",
                "environment": ["USERNAME=Omarchy", "PASSWORD=keep-me"],
                "ports": [{"target": 3389, "published": 3391, "protocol": "tcp"}],
                "volumes": [{"source": str(shared), "target": "/shared"}],
            }
        }
    }
    monkeypatch.setattr("wayseam.vm.backend.subprocess.run", lambda *a, **k: _completed(model))
    cfg = Config()

    adopt_config(cfg, inspect_installation(compose))

    assert cfg.pod.backend == "omarchy"
    assert cfg.pod.agent_port == 8767
    assert cfg.pod.container_name == "custom-omarchy-windows"
    assert cfg.pod.home_share == str(shared)
    assert (cfg.rdp.ip, cfg.rdp.port) == ("127.0.0.1", 3391)
    assert (cfg.rdp.user, cfg.rdp.password) == ("Omarchy", "keep-me")
    assert cfg.rdp.password_max_age == 0


def test_inspect_installation_rejects_missing_credentials(monkeypatch, tmp_path):
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services: {}\n")
    model = {"services": {"windows": {"environment": {"USERNAME": "Docker"}}}}
    monkeypatch.setattr("wayseam.vm.backend.subprocess.run", lambda *a, **k: _completed(model))

    with pytest.raises(OmarchyConfigError, match="USERNAME/PASSWORD"):
        inspect_installation(compose)


def test_compose_override_cold_plugs_ivshmem_and_preserves_base_arguments(
    monkeypatch, tmp_path
):
    from wayseam.vm import backend as omarchy

    base = tmp_path / "docker-compose.yml"
    base.write_text(
        'services:\n  windows:\n    environment:\n'
        '      ARGUMENTS: "-rtc base=utc -custom flag"\n'
    )
    shared = tmp_path / "Windows"
    monkeypatch.setattr(omarchy, "compose_file", lambda: base)
    monkeypatch.setattr(
        omarchy, "compose_override_file", lambda: tmp_path / "override.yaml"
    )
    cfg = Config()
    cfg.pod.backend = "omarchy"
    cfg.pod.agent_port = 8767
    cfg.pod.home_share = str(shared)

    path = omarchy.ensure_compose_override(cfg)
    content = path.read_text()

    # Compose replaces `environment` keys wholesale, so our ARGUMENTS must
    # carry Omarchy's original value plus the IVSHMEM cold-plug arguments.
    assert (
        'ARGUMENTS: "-rtc base=utc -custom flag '
        "-object memory-backend-file,id=wayseam-shm,size=64M,"
        "mem-path=/shared/wayseam-ivshmem.bin,share=on "
        '-device ivshmem-plain,id=wayseam-ivshmem,memdev=wayseam-shm"'
    ) in content
    assert 'USER_PORTS: "8765"' in content
    assert '"127.0.0.1:8767:8765/tcp"' in content

    backing = shared / "wayseam-ivshmem.bin"
    assert backing.stat().st_size == 64 * 1024 * 1024
    assert (backing.stat().st_mode & 0o777) == 0o600


def test_compose_override_falls_back_to_default_arguments(monkeypatch, tmp_path):
    from wayseam.vm import backend as omarchy

    monkeypatch.setattr(omarchy, "compose_file", lambda: tmp_path / "missing.yml")
    monkeypatch.setattr(
        omarchy, "compose_override_file", lambda: tmp_path / "override.yaml"
    )
    cfg = Config()
    cfg.pod.backend = "omarchy"
    cfg.pod.home_share = str(tmp_path / "Windows")

    content = omarchy.ensure_compose_override(cfg).read_text()

    assert '-rtc base=localtime,clock=host,driftfix=slew -object ' in content


def test_ivshmem_backing_is_resized_and_locked_down(tmp_path):
    from wayseam.vm import backend as omarchy

    cfg = Config()
    cfg.pod.home_share = str(tmp_path / "Windows")
    backing = tmp_path / "Windows" / "wayseam-ivshmem.bin"
    backing.parent.mkdir(parents=True)
    backing.write_bytes(b"stale")
    backing.chmod(0o666)

    result = omarchy.ensure_ivshmem_backing(cfg)

    assert result == backing
    assert backing.stat().st_size == 64 * 1024 * 1024
    assert (backing.stat().st_mode & 0o777) == 0o600
