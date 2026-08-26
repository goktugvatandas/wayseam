# SPDX-License-Identifier: MIT
from __future__ import annotations

import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from wayseam.vm.backend import stage_oem_archive


class TestOmarchyOemStaging(unittest.TestCase):
    def test_stages_archive_with_live_token_without_touching_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = root / "bundle"
            oem = bundle / "guest" / "oem"
            oem.mkdir(parents=True)
            (oem / "install.bat").write_text("@echo off\n", encoding="utf-8")
            (oem / "agent.ps1").write_text("Write-Output ok\n", encoding="utf-8")
            agent_dir = oem / "agent"
            agent_dir.mkdir()
            (agent_dir / "wayseam_wgc.cs").write_text("public class Wgc {}\n", encoding="utf-8")
            cfg = SimpleNamespace(
                pod=SimpleNamespace(backend="omarchy", container_name="omarchy-windows")
            )
            captured: dict[str, object] = {}

            def fake_run(command, **kwargs):
                if command[:2] == ["docker", "cp"]:
                    archive = Path(command[2])
                    captured["archive_mode"] = archive.stat().st_mode & 0o777
                    with tarfile.open(archive, "r:gz") as tar:
                        captured["names"] = set(tar.getnames())
                        token_file = tar.extractfile("oem/agent_token.txt")
                        assert token_file is not None
                        captured["token"] = token_file.read()
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            with (
                patch("wayseam.vm.backend.oem_dir", return_value=oem),
                patch("wayseam.vm.backend.ensure_agent_token", return_value="live-token"),
                patch("wayseam.vm.backend.subprocess.run", side_effect=fake_run) as run,
            ):
                destination = stage_oem_archive(cfg)

            self.assertEqual(destination, "/tmp/wayseam-recover/oem.tar.gz")
            self.assertEqual(captured["archive_mode"], 0o600)
            self.assertEqual(captured["token"], b"live-token")
            self.assertIn("oem", captured["names"])
            self.assertIn("oem/install.bat", captured["names"])
            self.assertIn("oem/agent.ps1", captured["names"])
            self.assertIn("oem/agent/wayseam_wgc.cs", captured["names"])
            self.assertFalse((oem / "agent_token.txt").exists())
            self.assertEqual(run.call_count, 3)

    def test_rejects_non_omarchy_backend(self) -> None:
        cfg = SimpleNamespace(pod=SimpleNamespace(backend="docker", container_name="windows"))
        with self.assertRaises(ValueError):
            stage_oem_archive(cfg)


if __name__ == "__main__":
    unittest.main()
