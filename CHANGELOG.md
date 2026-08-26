# Changelog

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
