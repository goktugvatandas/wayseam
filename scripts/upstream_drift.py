#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Report upstream WinPodX changes to the files Wayseam ported.

Reads ``upstream/PROVENANCE.toml`` (file -> upstream path @ base commit),
fetches the upstream repository into a cache, and lists every ported file
whose upstream source changed since the recorded base. Use it to decide
which upstream fixes are worth carrying over::

    scripts/upstream_drift.py            # summary
    scripts/upstream_drift.py --diff src/wayseam/guest/agent.py
    scripts/upstream_drift.py --bump     # record the current upstream HEAD as base
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "upstream" / "PROVENANCE.toml"
CACHE = Path.home() / ".cache" / "wayseam" / "upstream-winpodx"


def _git(*args: str, cwd: Path = CACHE) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _ensure_clone(repo: str) -> None:
    if (CACHE / ".git").is_dir():
        _git("fetch", "-q", "origin")
        return
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "-q", repo, str(CACHE)], check=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--diff", metavar="WAYSEAM_PATH", help="show the upstream diff for one ported file")
    parser.add_argument("--bump", action="store_true", help="record origin/HEAD as the new base")
    parser.add_argument("--ref", default="origin/HEAD", help="upstream ref to compare against")
    args = parser.parse_args()

    manifest = tomllib.loads(MANIFEST.read_text())
    upstream = manifest["upstream"]
    _ensure_clone(upstream["repo"])
    base = upstream["base"]
    head = _git("rev-parse", args.ref)
    files = manifest.get("file", [])

    if args.diff:
        entry = next((f for f in files if f["wayseam"] == args.diff), None)
        if entry is None:
            print(f"{args.diff} is not a ported file", file=sys.stderr)
            return 2
        print(_git("diff", f"{base}..{head}", "--", entry["upstream"]))
        return 0

    changed = _git("diff", "--name-only", f"{base}..{head}").splitlines()
    changed_set = set(changed)
    drifted = [f for f in files if f["upstream"] in changed_set]
    print(f"upstream {upstream['repo']}: base {base[:9]} -> {args.ref} {head[:9]}")
    print(f"{len(drifted)} of {len(files)} ported files changed upstream")
    for f in drifted:
        stat = _git("diff", "--shortstat", f"{base}..{head}", "--", f["upstream"])
        print(f"  {f['wayseam']:<45} <- {f['upstream']}  ({stat.strip()})")
    if args.bump:
        text = MANIFEST.read_text().replace(f'base = "{base}"', f'base = "{head}"', 1)
        MANIFEST.write_text(text)
        print(f"base bumped to {head[:9]} — port the changes above before committing")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
