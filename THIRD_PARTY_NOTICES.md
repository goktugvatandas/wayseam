# Third-party notices

Wayseam is MIT-licensed (see `LICENSE`). It includes or depends on the
following third-party work.

## Ported source: WinPodX (MIT)

Copyright (c) 2026 Kim DaeHyun (kernalix7@kodenet.io).
https://github.com/kernalix7/winpodx

Files under `src/wayseam/` that were ported from WinPodX are listed with
their upstream path and base commit in `upstream/PROVENANCE.toml`. The
guest payload under `guest/oem/` (install.bat and helper scripts) and
`guest/scripts/discover_apps.ps1` also derive from WinPodX.

## Bundled binary: rdprrap 0.3.0 (MIT)

`guest/oem/rdprrap-0.3.0-windows-x64.zip` — multi-session RDP for the guest.
Copyright (c) 2026 Kim DaeHyun, MIT. https://github.com/kernalix7/rdprrap
rdprrap itself vendors work from RDP Wrapper (Apache-2.0,
https://github.com/stascorp/rdpwrap), TermWrap (MIT,
https://github.com/llccd/TermWrap) and RDPWrapOffsetFinder (MIT,
https://github.com/llccd/RDPWrapOffsetFinder); the full texts ship inside
the archive (`LICENSE`, `NOTICE`, `THIRD_PARTY_LICENSES.txt`).

## Not bundled, installed by the user in the guest

- **parsec-vdd** — Parsec Virtual Display Driver packaging and protocol,
  https://github.com/nomi-san/parsec-vdd (the driver binary is Parsec's).
- **IVSHMEM driver** — from virtio-win (Red Hat), as documented by the
  Looking Glass project, https://looking-glass.io.

## Runtime dependencies (not bundled)

Dockur Windows (MIT) via Omarchy, Docker, GTK4 / PyGObject, FreeRDP.
