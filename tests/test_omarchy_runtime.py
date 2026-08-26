# SPDX-License-Identifier: MIT
from __future__ import annotations

import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wayseam.config import Config
from wayseam.guest.agent import AgentClient
from wayseam.vm.backend import ensure_compose_override


class TestOmarchyRuntime(unittest.TestCase):
    def test_override_adds_agent_forward_without_editing_omarchy_compose(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "docker-compose.yml"
            base.write_text("services:\n  windows: {}\n", encoding="utf-8")
            cfg = Config()
            cfg.pod.backend = "omarchy"
            cfg.pod.agent_port = 8767

            with patch("wayseam.vm.backend.config_dir", return_value=root / "wayseam"):
                override = ensure_compose_override(cfg)

            self.assertEqual(base.read_text(encoding="utf-8"), "services:\n  windows: {}\n")
            text = override.read_text(encoding="utf-8")
            self.assertIn('USER_PORTS: "8765"', text)
            self.assertIn('AUDIO: "Y"', text)
            self.assertIn('127.0.0.1:8767:8765/tcp', text)
            self.assertEqual(stat.S_IMODE(override.stat().st_mode), 0o600)

    def test_agent_client_uses_configured_omarchy_host_port(self) -> None:
        cfg = Config()
        cfg.rdp.ip = "127.0.0.1"
        cfg.pod.agent_port = 8767

        self.assertEqual(AgentClient(cfg).base_url, "http://127.0.0.1:8767")


if __name__ == "__main__":
    unittest.main()
