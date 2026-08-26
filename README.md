<p align="center"><img src="assets/wayseam-icon.png" alt="Wayseam" width="160"></p>

# Wayseam

**Windows applications as native Wayland windows on [Omarchy](https://omarchy.org).**

Wayseam takes the Windows 11 VM that Omarchy already manages and makes each
Windows application window appear as an ordinary Hyprland window: tiled,
moved, focused, scrolled and closed like any Linux window, launched from the
Omarchy menu, with the Windows desktop itself out of sight. It is the
Parallels Coherence / VMware Unity idea, built for Omarchy.

> Status: **alpha, in daily use by the author**. It works well for the
> conventional desktop apps it was built against (Affinity, Notepad, Windows
> Terminal, Explorer, Office-style apps). Known gaps are listed below; they are
> being worked on in the open.

---

## The idea

Running Windows software on Linux usually means choosing between a
full-screen VM window (a desktop inside your desktop) and remote-application
protocols (RDP RemoteApp/RAIL), which flicker, lag, and fight the compositor.
Wayseam takes a third route:

1. **Capture each window where it lives.** A small agent inside the Windows
   guest captures every presented top-level window with *Windows Graphics
   Capture* — the same API the Windows screen recorder uses — so the app
   keeps rendering natively, at full fidelity, with all its own behaviours.
2. **Move pixels through memory, not a network.** Frames travel host ↔ guest
   over a shared-memory ring (an IVSHMEM PCI device exposed by QEMU), so a
   host-visible frame costs microseconds, not a protocol round trip. Input
   goes back the same way.
3. **Present each window as a real Wayland window.** A per-window presenter
   on the host renders the frame in a GTK4 window with Wayseam's own app id,
   so Hyprland tiles it, the menu launches it, and the Omarchy theme applies
   to it. The guest desktop is arranged to mirror the host tiling 1:1, so
   focus, overlap and scroll routing inside Windows match what you see.
4. **Keep the cursor honest.** The guest cursor is drawn inside the frame at
   its exact hotspot (even over dialogs), the host pointer is continuously
   reconciled with the compositor's position, and white/inverting cursors get
   a contrast rim so they never vanish over text.

The result on the author's machine: ~26 ms median from keypress to visible
pixels, crisp 100% DPI output on a 4K virtual display, and windows that feel
like they belong to the desktop.

## What you get

- `wayseam run <app>` (and the Omarchy menu) opens a Windows app as a Wayseam
  window; closing the window closes the app window.
- **Wayseam Mode / Desktop Mode** — switch from the bar applet or
  `wayseam mode set …`: Desktop Mode shows the conventional full Windows
  desktop (FreeRDP) for anything Wayseam can't present.
- **Omarchy menu integration** — Windows apps under *Apps*, under a
  *Windows Apps* submenu, both, or hidden; per-app visibility.
- **Bar applet** (Quickshell) — mode switch, menu placement, app visibility,
  launch buttons, presence dots for windows currently presented.
- **Automatic presentation** — a watcher notices new top-level windows the
  app opens (child windows, second documents) and presents them too.
- **Reboot-safe guest** — the agent owns the virtual display, its refresh
  keepalive and DPI, and restores everything after a guest reboot.
- Clipboard sync (text), scroll wheel, keyboard incl. Unicode, caption-strip
  controls (tabs, back buttons), audio through Omarchy's VM audio device.

## Requirements

Host (Omarchy):

- Omarchy with the Windows VM installed (`omarchy-windows-vm install`) —
  Wayseam adopts that VM and never rewrites Omarchy's compose file or launcher.
- `docker`, `python-gobject`, `gtk4`; `freerdp` for Desktop Mode. All ship
  with or are one `pacman -S` away on Omarchy.
- A Hyprland session (Wayseam talks to Hyprland's IPC for tile geometry).

Guest (Windows 11, inside the Omarchy VM) — **two drivers, installed once**:

- **Parsec Virtual Display Driver** (`parsec-vdd`) — the high-refresh virtual
  display Wayseam renders through. Install the official driver
  (nomi-san/parsec-vdd, v0.45) in the guest.
- **IVSHMEM driver** — the shared-memory device driver from virtio-win
  (documented by the Looking Glass project). Install it for the "IVSHMEM
  Device" that appears once the VM is started through Wayseam.

Automating these two installs is on the roadmap; today they are a one-time
manual step in the guest.

## Install

```bash
git clone https://github.com/goktugvatandas/wayseam ~/Projects/wayseam
cd ~/Projects/wayseam
uv venv && uv pip install -e .            # or: pip install -e .
ln -s "$PWD/.venv/bin/wayseam" ~/.local/bin/wayseam
wayseam setup
```

`wayseam setup` is one idempotent, resumable command. It adopts the Omarchy
VM, adds the shared-memory device and the agent port to a private compose
override, starts the VM, bootstraps the guest agent (on first run it prints a
short PowerShell paste for the noVNC console at http://127.0.0.1:8006 and
stops with exit code 3 — paste it, then re-run `setup`), discovers your
Windows apps, syncs the Omarchy menu, and installs the bar applet.

Then install the two guest drivers above, reboot the guest once
(`wayseam pod stop && wayseam pod start`), and check:

```bash
wayseam agent status     # agent version, Parsec display primary at 4K@240, DPI 100%
wayseam state            # mode, VM, agent, presenters, apps
```

## Daily use

```bash
wayseam run notepad              # or click it in the Omarchy menu
wayseam apps list                # discovered apps and their slugs
wayseam apps refresh             # re-scan the guest after installing something
wayseam apps visibility --hide xbox --show paint
wayseam menu placement set windows-apps   # apps | windows-apps | both | none
wayseam mode set desktop         # full Windows desktop; `wayseam mode set wayseam` to return
wayseam pod start|stop|status    # the VM
wayseam agent deploy             # push the bundled agent after editing it
```

Presenters log to `$XDG_RUNTIME_DIR/wayseam/`.

## How it works

```
 Omarchy / Hyprland                                Windows 11 guest (Dockur/QEMU)
 ┌───────────────────────────────┐                 ┌────────────────────────────────┐
 │ wayseam.present.window (GTK4) │◄── frame ring ──│ agent.ps1 + wayseam_wgc.cs     │
 │   one per Windows window      │   IVSHMEM 64MiB │   Windows Graphics Capture     │
 │   in-frame cursor, input      │── input ring ──►│   per HWND, damage deltas      │
 │ wayseam.present.watch         │◄── HTTP ───────►│   control plane: windows,      │
 │   presents new windows        │   :8767→8765    │   cursor, clicks, clipboard,   │
 │ wayseam.omarchy.*  applet     │                 │   resize/placement, display    │
 └───────────────────────────────┘                 └────────────────────────────────┘
```

- **Capture**: `guest/oem/agent/wayseam_wgc.cs` runs a WGC session per
  presented window, diffs frames into damage rectangles, and publishes them
  into a 3-slot seqlock ring in the shared memory. Oversized frames fall back
  to one HTTP fetch; if the device is missing, everything falls back to HTTP.
- **Input**: pointer moves, wheel and keys go through a second ring consumed
  by a 1 kHz guest thread; clicks stay on HTTP because the guest hit-tests
  them (caption-strip controls pass through, window-management hits do not).
- **Placement**: each presenter reports its tile position to the guest, which
  places the real window at the same desktop coordinates with the invisible
  frame borders compensated, so the tile shows exactly the window's visible
  rectangle and Windows' own routing (wheel by z-order, hit tests) matches.
- **Display**: the agent keeps the Parsec display alive (driver needs a
  <100 ms ping), makes it the primary at the configured mode, and forces 100%
  DPI through the display-config API; all of it re-applies at agent start.
- **Modes**: Desktop Mode launches FreeRDP against the same VM and hands the
  console back with `tscon` when returning to Wayseam Mode.

Omarchy integration details: `docs/OMARCHY.md`.
## Known gaps (alpha)

- Guest drivers (parsec-vdd, IVSHMEM) are installed manually once.
- Packaged (UWP) apps — Settings, Calculator, Store apps — are not presented
  yet: per-window capture sees no content inside ApplicationFrameHost frames.
  A display-crop capture path is planned; use Desktop Mode for them meanwhile.
- WinUI apps with Mica/Acrylic backdrops capture without the backdrop (the
  in-window "Save changes?" prompt of the new Notepad shows only its buttons).
- One host monitor at scale 1 is what is exercised; multi-monitor and
  fractional scaling are untested.
- Latency floor is the app's own render + capture delivery (~16–25 ms); going
  lower needs a real GPU in the guest (VFIO), which is planned.
- Some apps enforce minimum window sizes larger than small tiles.

## Development

```bash
uv venv && uv pip install -e ".[dev]"
.venv/bin/pytest -q                       # 320 tests
.venv/bin/ruff check src tests scripts
scripts/deploy_agent.py --apply           # push guest/oem/agent into the running guest
scripts/upstream_drift.py                 # ported files changed upstream since our base
```

Layout: `src/wayseam/` (`cli`, `config`, `apps`, `guest/`, `vm/`, `present/`,
`omarchy/`, `desktop/`, `desktop_mode/`), `guest/oem/` (payload staged into
the VM), `guest/scripts/`, `scripts/`, `upstream/` (provenance), `docs/`.

## Credits

Wayseam stands on other people's work, gratefully:

- **[WinPodX](https://github.com/kernalix7/winpodx)** by Kim DaeHyun — Wayseam
  began as a WinPodX fork, and its guest-agent transport, app discovery,
  desktop-entry integration, container backend and FreeRDP launcher are
  ported from it (MIT). Every ported file is listed with its upstream origin
  in `upstream/PROVENANCE.toml`, and `scripts/upstream_drift.py` keeps track
  of upstream improvements worth carrying over.
- **[Looking Glass](https://looking-glass.io)** — the proof that a
  shared-memory (IVSHMEM) path between a KVM guest and its host can carry
  frames with negligible latency, and the documentation of the IVSHMEM
  driver setup. Wayseam's ring transport is a per-window take on that idea.
- **[parsec-vdd](https://github.com/nomi-san/parsec-vdd)** — the standalone
  Parsec virtual display driver and its control protocol.
- **[Dockur Windows](https://github.com/dockur/windows)** and **Omarchy** —
  the Windows VM Wayseam adopts, and the desktop it integrates into.
- **[rdprrap](https://github.com/kernalix7/rdprrap)** — multi-session RDP for
  the guest, bundled in the OEM payload (see `THIRD_PARTY_NOTICES.md`).

License: MIT — see `LICENSE`.
