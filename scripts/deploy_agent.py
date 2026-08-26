#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Deploy the bundled guest agent: ``wayseam agent deploy`` equivalent."""

from __future__ import annotations

import argparse

from wayseam.config import Config
from wayseam.guest.deploy import deploy_agent


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.apply:
        parser.error("deployment requires --apply")
    return deploy_agent(Config.load())


if __name__ == "__main__":
    raise SystemExit(main())
