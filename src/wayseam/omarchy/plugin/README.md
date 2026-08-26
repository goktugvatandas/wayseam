# Windows (Wayseam) bar widget for the Omarchy shell

Plugin id: `goktugvatandas.wayseam`. One bar icon (`󰖳`, dimmed while the
Windows VM is stopped) and one panel driven entirely by the `wayseam` CLI.

## What the panel does

- **Hero** — `VM STOPPED`, `AGENT OFFLINE`, `WAYSEAM MODE · N WINDOWS`, or
  `DESKTOP MODE`, plus the guest agent version. Falls back to
  `WAYSEAM UNAVAILABLE` when the CLI is missing or prints non-JSON.
- **Mode** — Wayseam / Desktop chips → `wayseam mode set <mode>`.
  Disabled with an explanation while the VM is not running.
- **In Omarchy menu** — Apps / Windows Apps / Both / Hidden →
  `wayseam menu placement set <apps|windows-apps|both|none>`.
- **Apps** — every discovered app with its icon, a visibility switch
  (`wayseam apps visibility --show|--hide <slug>`), a launch button
  (`wayseam run <slug>`), and a dot on apps that currently have a Wayseam
  Window on screen. `All` / `None` map to `--all` / `--none`. The search field
  filters by name or slug.
- **Footer** — `Refresh apps` runs `wayseam apps refresh` and shows a spinner until the state reloads; `Open desktop`
  switches to Desktop Mode.

State comes from `wayseam state --json`: read on open, every
`refreshIntervalSec` while open, after every action, and on a slow background
poll while closed so the bar icon stays honest. The last good state is kept
across transient failures.

## Keyboard

| Key                | Action                                              |
|--------------------|-----------------------------------------------------|
| `j` / `k`, arrows  | Move between sections and app rows                  |
| `h` / `l`          | Move inside a chip row; switch ↔ launch on an app   |
| `Enter` / `Space`  | Activate the highlighted control                    |
| `/`                | Focus the search field (`Esc` clears, then leaves)  |
| `r`                | Re-read state                                       |
| `Tab` / `Shift+Tab`| Jump to the neighbouring bar panel                  |
| `Esc`              | Close                                               |

Mouse: left click on the bar icon toggles the panel, middle click refreshes.

## IPC

```
omarchy-shell goktugvatandas.wayseam open|close|show|hide|toggle
omarchy-shell goktugvatandas.wayseam refresh    # re-read state, returns ok
omarchy-shell goktugvatandas.wayseam status     # current hero status text
omarchy-shell goktugvatandas.wayseam version    # plugin version (code-identity probe)
```

## Settings (`omarchy bar set goktugvatandas.wayseam <key> <value>`)

| Key                  | Default   | Meaning                                              |
|----------------------|-----------|------------------------------------------------------|
| `refreshIntervalSec` | `15`      | Poll interval while the panel is open (5–3600)       |
| `wayseamCommand`     | `wayseam` | Executable for every call; resolved via `bash -lc`   |

## Install

`wayseam plugin install` symlinks this directory to
`~/.config/omarchy/plugins/goktugvatandas.wayseam`, rescans, and enables it.
By hand:

```
ln -sfn "$(pwd)" ~/.config/omarchy/plugins/goktugvatandas.wayseam
omarchy-shell shell rescanPlugins
omarchy plugin enable goktugvatandas.wayseam
```

Editing the plugin after it has been loaded needs `omarchy-restart-shell`:
Quickshell 0.3.0 exposes no `Qt.clearComponentCache`, so the shell's
"Local plugin changed" reload re-instantiates the widget but keeps the
already-compiled component. (The inotify watcher also does not follow the
symlink, so only re-creating the link with `ln -sfn` even triggers it.)

## Files

- `manifest.json` — plugin manifest (bar-widget, settings schema)
- `Panel.qml` — bar icon, popup, cursor model, CLI processes
- `AppRow.qml` — one app row (icon, name, switch, launch)
- `Model.js` — JSON parsing, state normalization, command construction
  (pure JS; `node -e 'require("./Model.js")'` works for quick checks)
