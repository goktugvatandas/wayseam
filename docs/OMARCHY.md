# Omarchy integration (Wayseam)

Wayseam adopts Omarchy's existing Dockur
Windows VM instead of provisioning a second VM, and a **Wayseam** presentation
layer that shows Windows apps as native, tiling Hyprland windows.

## Quick start

```bash
wayseam setup
```

One idempotent, resumable command: it adopts the existing VM, starts it, walks
you through the **one-time guest bootstrap** (a short PowerShell paste in the
noVNC console at <http://127.0.0.1:8006>, because the guest agent is not up
yet), discovers your Windows apps, syncs the Omarchy menu, and installs the
shell applet. If the guest still needs bootstrapping it prints the exact
commands and stops with exit code 3; complete the paste and re-run
`wayseam setup` to finish. Flags: `--timeout <seconds>` (agent wait,
default 300) and `--no-applet` (skip the bar applet).

The equivalent individual steps are:

```bash
wayseam pod start          # start the Omarchy VM and wait for it
wayseam setup              # adopt + compose override + guest bootstrap (resumable)
wayseam apps refresh       # discover Windows apps
wayseam menu sync          # write the desktop entries / Omarchy menu block
wayseam plugin install     # link and enable the bar applet
```

The adapter reads `~/.config/windows/docker-compose.yml` through
`docker compose config --format json`, imports the existing RDP account, uses
the published RDP port, and maps Omarchy's `/shared` bind as Wayseam's scoped
home share. It never rewrites the Omarchy compose file or the package-owned
`/usr/bin/omarchy-windows-vm` launcher. A private 0600 override at
`~/.config/wayseam/omarchy-compose.override.yaml` adds the guest-agent forward
on host port 8767 and Dockur's virtual audio device. This avoids conflicts with
local services on Wayseam's usual 8765 port and leaves Omarchy's own Compose
file untouched.

The first two commands are host-only. `recover-oem` prints the one-time guest
bootstrap procedure required to install Wayseam's RemoteApp helpers in an
already-created Windows guest. Until that guest bootstrap is complete, the
normal Omarchy desktop launcher remains the recovery path.

## Presentation modes

Each VM has exactly one active presentation mode. The last
selected mode persists in `~/.config/wayseam/omarchy.json` (mode 0600).

```bash
wayseam mode get
wayseam mode set desktop    # Wayseam presenters + watcher stop, RDP desktop opens
wayseam mode set wayseam    # RDP desktop disconnects, console session is
                                    # reattached through the guest agent, watcher restarts
```

Switching never stops Windows applications: presenters receive SIGTERM (their
signal path does not forward a close into Windows) and terminating the desktop
client only disconnects the RDP session. Wayseam never calls
`omarchy-windows-vm launch`, because that launcher stops the VM when its RDP
client exits. In Desktop Mode, `wayseam run <slug>` (and therefore every
menu row and `.desktop` entry) starts the app inside the Windows desktop
instead of presenting a Wayseam Window; the window watcher exits quietly while
the mode is `desktop`.

## Windows apps in the Omarchy menu

`wayseam menu sync` places the visible apps from Wayseam's app database
according to the persisted **menu placement**:

| placement      | `omarchy-menu.jsonc` block | `.desktop` entries (Apps list) |
| -------------- | -------------------------- | ------------------------------ |
| `apps` (default) | removed                  | visible                        |
| `windows-apps` | "Windows Apps" submenu     | `NoDisplay=true`               |
| `both`         | "Windows Apps" submenu     | visible                        |
| `none`         | removed                    | `NoDisplay=true`               |

```bash
wayseam apps refresh
wayseam menu placement get
wayseam menu placement set windows-apps   # also runs the sync
wayseam apps visibility --hide xbox game-bar # wayseam app hide + sync
wayseam apps visibility --all | --none | --show SLUG...
```

The sync only rewrites the marked managed block in
`~/.config/omarchy/extensions/omarchy-menu.jsonc`; entries, comments and
ordering outside it are preserved, and a malformed file is rejected without
being overwritten. Omarchy 4's menu model only honours
`icon/label/action/target/provider/aliases/when/checked/description`, so every
generated row is an `action` row that launches the app's desktop entry exactly
like the shell's own Apps list (`uwsm-app -- gtk-launch wayseam-<slug>.desktop`).
Hidden apps never get a row and their desktop entry is removed.

## Status and shell applet

`wayseam state [--json]` returns a fast, secret-free snapshot for the
Omarchy shell applet: mode, placement, VM container state (`docker inspect`,
5 s timeout), guest-agent health, the desktop client / presenter / guest
session state, and the app list with per-app `presented` flags. Every probe is
bounded by a timeout and failures land in `errors` instead of aborting.

```bash
wayseam plugin install   # symlink ~/.config/omarchy/plugins/goktugvatandas.wayseam
                                 # -> wayseam/omarchy/plugin, rescan + enable in omarchy-shell
wayseam plugin status
```

While Wayseam Mode has at least one active presenter, a singleton watcher
follows visible top-level windows opened by known guest applications. This
allows browser-based sign-in and app-to-app handoffs to appear without a
second menu launch. Executable identity is matched against the discovered app
catalog; Windows desktop, taskbar, and non-folder Explorer shell surfaces are
explicitly excluded.

For tests and nonstandard installations, set `WINPODX_OMARCHY_COMPOSE` to an
alternate compose file.

## Shared-memory transport (IVSHMEM, staged)

`ensure_compose_override` also cold-plugs a 64 MiB **IVSHMEM** PCI device
(`-object memory-backend-file … -device ivshmem-plain`) backed by
`~/Windows/wayseam-ivshmem.bin` (0600) inside the `/shared` bind mount, so the
same bytes are guest RAM and a host-`mmap`-able file. This is the foundation
for replacing the HTTP frame path with a zero-copy shared-memory transport
(Looking Glass style, but per-HWND). The device only attaches at the next VM
start — q35's root bus rejects hotplug. The guest already carries the Red Hat
IVSHMEM driver and the Looking Glass indirect display driver from earlier
work. `scripts/diagnostics/wayseam_ivshmem_probe.py` verifies guest↔host
coherency and measures raw latency. Measured on 2026-08-26: **1.5 µs median
round-trip** (p95 2.8 µs) and **15.6 GiB/s** guest write bandwidth with 16 MiB
visible host-side within 0.6 ms — versus ~39.5 ms median for the HTTP frame
path it will replace. Note: the Looking Glass IDD display adapter and its
helper services are disabled in the guest (they exclusively attach the
IVSHMEM device); re-coordinate when GPU work begins.

## Current boundaries

- The Omarchy compose file and `/usr/bin/omarchy-windows-vm` remain externally
  owned and are never rewritten.
- Password rotation is disabled for the adopted VM because Omarchy owns that
  account lifecycle.
- FreeRDP receives credentials through `/args-from:stdin`, so the password is
  not visible in the process command line. Certificate trust uses TOFU for
  loopback and remote endpoints; certificate changes fail visibly.
- Wayseam's one-time guest bootstrap still installs its existing RemoteApp and
  multi-session helpers. Treat the guest as disposable and review the upstream
  security notes before using sensitive data.
- Omarchy's `/shared` directory is used as the scoped home share instead of
  exposing the entire Linux home directory.

## Alpha status (2026-08-26)

Works in Wayseam Mode: launching discovered apps as tiling Hyprland windows,
pointer and keyboard input, scroll wheel (mouse notches and touchpad pixels,
delivered to the HWND under the pointer), Windows' own cursor, owned
popups/menus, text clipboard in both directions (host text is pushed on
focus-in; guest text is mirrored while a Wayseam Window is focused and on
focus-out), automatic presentation of new windows from known apps, and the
damage-patch renderer (a hover-sized update costs microseconds on the host
instead of a full-surface copy), and pointer re-sync after keyboard resizes,
float/tile toggles, and mode switches (the guest pointer is re-mapped from the
compositor's real cursor position, so hover state and the app's own cursor no
longer sit at a stale spot until the mouse moves).

Known gaps: only `text/plain` crosses the clipboard (no images/files); no
drag-and-drop between hosts; IME/dead keys are sent as Unicode presses
(shortcuts on non-ASCII keys do not fire); audio depends on the Dockur
`AUDIO=Y` PipeWire bridge and is unverified end to end; the guest agent was
observed to exit once under a duplicate-presenter resize fight (its keepalive
restarted it within a minute, and the watcher now refuses duplicates); the
guest has no GPU, so DWM/app rendering is a latency floor Wayseam cannot fix.

Guest agent deploys (`scripts/diagnostics/wayseam_deploy_agent.py --apply`)
restart only the agent; running presenters reconnect on their own.
