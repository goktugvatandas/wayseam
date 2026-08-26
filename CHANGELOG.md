# Changelog

## 0.1.1 — first week of fixes (2026-08-27)

Everything found by using the release for a day. Guest agent 0.2.44.

### Fixed
- File Explorer opened as a grey window: the launcher treated the desktop
  (`Progman`) as Explorer's window; shell surfaces are never candidates now.
- Windows stayed transparent/blank when a presenter attached after the first
  full frame was superseded (apps that animate on open); the ring reader now
  asks for a base frame and the pump fetches one.
- Stale regions that only refreshed on a full repaint: the ring overwrote
  unconsumed publishes and a queued capture frame was dropped. The host now
  acknowledges applied frames and the guest carries unacknowledged damage in
  every publish.
- One window's frames showing up in another tile after an agent restart
  (presenters now follow slot reassignment); stale "shm timed out" titles;
  zombie presenters holding window claims; watcher reaps exited presenters.
- Desktop Mode opened a second Windows session instead of the one running the
  apps (`fSingleSessionPerUser`, enforced by the agent and install.bat).
- App icons were filed under non-standard hicolor sizes and never resolved.
- The bar applet kept running its previous code after a re-link; plugin
  install now restarts the shell.

### Added
- Packaged (UWP) apps — Settings, Calculator, Store apps — are presented via
  display-crop capture and kept topmost in the guest while presented.
- Unowned popups on the app's UI thread ("Show more options", tooltips) are
  overlaid like owned windows.
- Applet: VM Start / Stop / Restart with inline confirmation; hidden apps under
  a collapsible group; per-app ⋯ menu with eye-icon Hide/Show; per-app glyphs
  in the Windows Apps submenu.
- `wayseam pod restart`, `wayseam pod start --no-wait`; `pod stop`/`restart`
  retire presenters first.

## 0.1.0 — first public shape (2026-08-27)

The alpha that runs the author's daily Windows apps as native Omarchy windows.

- Per-window Windows Graphics Capture in the guest agent, damage-compressed
  frames over an IVSHMEM shared-memory ring, input over a second ring;
  ~26 ms median keypress-to-pixels.
- GTK4 presenter per window: in-frame cursor (also over dialogs), contrast rim
  for white cursors, pixel-exact pointer mapping with continuous compositor
  reconcile, wheel routing by hovered window, caption-strip clicks, keyboard,
  clipboard sync, close forwarding.
- Host tile layout mirrored 1:1 onto the guest desktop; square corners, no
  guest border; Windows Terminal first-frame fix.
- Parsec virtual display at 3840×2160@240 with 100% DPI, owned by the agent
  and restored automatically after a guest reboot.
- Omarchy integration: Wayseam/Desktop mode switch, menu placement, Quickshell
  applet, `wayseam setup` one-command install, automatic window watcher with
  atomic per-window presenter claims.
- Standalone `wayseam` package dissected from the WinPodX fork with per-file
  provenance and an upstream drift tool.
