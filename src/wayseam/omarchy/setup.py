# SPDX-License-Identifier: MIT
"""One-command orchestrator for adopting the Omarchy Windows VM.

``run_setup`` wraps the existing host-side steps (adopt → start → guest
bootstrap → discover → menu → applet) into a single idempotent, resumable
flow. Every external side-effect is injected as a keyword-defaulted callable
so the whole thing can be unit-tested without touching docker, the guest
agent, or the filesystem.
"""

from __future__ import annotations

import shutil
import time
from collections.abc import Callable
from typing import Any

from wayseam.guest.agent import AgentAuthError, AgentClient, AgentError


def _default_health(cfg: Any) -> dict[str, Any]:
    from wayseam.guest.agent import AgentClient

    return AgentClient(cfg).health()


def _default_compose_exists() -> bool:
    from wayseam.vm.backend import compose_file

    return compose_file().is_file()


def _step(name: str, status: str, detail: str = "") -> dict[str, str]:
    return {"name": name, "status": status, "detail": detail}


def run_setup(
    *,
    cfg: Any = None,
    timeout: int = 300,
    install_applet: bool = True,
    refresh_timeout: int = 180,
    poll_interval: float = 3.0,
    docker_which: Callable[[str], Any] = shutil.which,
    compose_exists: Callable[[], bool] = _default_compose_exists,
    inspect: Callable[..., Any] | None = None,
    adopt: Callable[..., Any] | None = None,
    ensure_override: Callable[..., Any] | None = None,
    ensure_token: Callable[[], Any] | None = None,
    load_config: Callable[[], Any] | None = None,
    start: Callable[[Any], Any] | None = None,
    health: Callable[[Any], dict[str, Any]] = _default_health,
    auth_probe: Callable[[Any], Any] | None = None,
    sleep: Callable[[float], Any] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    recover: Callable[[], Any] | None = None,
    discover: Callable[..., Any] | None = None,
    persist: Callable[..., Any] | None = None,
    sync: Callable[..., int] | None = None,
    applet: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Take an Omarchy user from an installed Dockur Windows VM to Wayseam.

    Returns a result dict. ``ok`` is True only on full success. An
    ``awaiting_bootstrap`` result (ok False) is a success-ish pause: the
    guest needs the one-time noVNC paste before the run can continue.
    """
    # Bind the real building blocks lazily so importing this module stays
    # cheap and tests can inject fakes without the imports firing.
    from wayseam.vm.backend import (
        OmarchyConfigError,
        adopt_config,
        ensure_compose_override,
        inspect_installation,
    )

    inspect = inspect or inspect_installation
    adopt = adopt or adopt_config
    ensure_override = ensure_override or ensure_compose_override

    steps: list[dict[str, str]] = []
    warnings: list[str] = []

    def fail(stage: str, error: str, **extra: Any) -> dict[str, Any]:
        return {"ok": False, "stage": stage, "error": error, "steps": steps, **extra}

    # --- 1. Preflight -------------------------------------------------------
    if not docker_which("docker"):
        steps.append(_step("preflight", "failed", "docker not found on PATH"))
        return fail("preflight", "docker is not installed or not on PATH")
    if not compose_exists():
        steps.append(_step("preflight", "failed", "Omarchy compose file missing"))
        return fail(
            "preflight",
            "Omarchy Windows VM not found — run `omarchy-windows-vm install` first",
        )
    steps.append(_step("preflight", "ok", "docker present, Omarchy compose found"))

    # --- 2. Adopt -----------------------------------------------------------
    if load_config is None:
        from wayseam.config import Config

        load_config = Config.load
    if ensure_token is None:
        from wayseam.token import ensure_agent_token

        ensure_token = ensure_agent_token

    if cfg is None:
        cfg = load_config()
    try:
        installation = inspect()
    except OmarchyConfigError as exc:
        steps.append(_step("adopt", "failed", str(exc)))
        return fail("preflight", f"Cannot read the Omarchy Windows VM: {exc}")

    cfg.pod.backend = "omarchy"
    adopt(cfg, installation)
    ensure_override(cfg)
    ensure_token()
    cfg.save()
    steps.append(_step("adopt", "ok", "adopted Omarchy VM as the wayseam backend"))

    # --- 3. Start + wait for the guest agent --------------------------------
    if start is None:
        from wayseam.vm.lifecycle import start_pod

        start = start_pod
    from wayseam.vm.lifecycle import PodState

    status = start(cfg)
    state = getattr(status, "state", None)
    if state == PodState.ERROR:
        detail = getattr(status, "error", "") or "unknown error"
        steps.append(_step("start", "failed", detail))
        return fail("start", f"Failed to start the pod: {detail}")
    steps.append(_step("start", "ok", f"pod state: {getattr(state, 'value', state)}"))

    agent_ok = False
    deadline = clock() + max(0, timeout)
    while True:
        try:
            payload = health(cfg)
            if payload.get("ok", True):
                agent_ok = True
                break
        except AgentError:
            pass
        if clock() >= deadline:
            break
        sleep(poll_interval)

    # A healthy agent may still hold an older token (host reinstalled, guest
    # kept its C:\OEM\agent_token.txt): /health is unauthenticated, so probe
    # an authenticated call before trusting it. A 401 means the guest needs
    # the bootstrap again, which re-stages the OEM payload with this token.
    bootstrap_reason = "guest agent is not reachable"
    if agent_ok and auth_probe is not None:
        try:
            auth_probe(cfg)
        except AgentAuthError:
            agent_ok = False
            bootstrap_reason = "guest agent rejects this host's token (re-bootstrap)"
        except AgentError:
            pass

    # --- 4. Guest bootstrap gate --------------------------------------------
    if not agent_ok:
        if recover is None:

            from wayseam.vm.recovery import recover_oem

            recover = recover_oem
        try:
            recover()
        except Exception as exc:  # noqa: BLE001 — best-effort; still pause
            warnings.append(f"guest bootstrap helper failed: {exc}")
        steps.append(
            _step(
                "guest-bootstrap",
                "action-needed",
                f"{bootstrap_reason}: paste the one-time PowerShell in the noVNC console",
            )
        )
        return {
            "ok": False,
            "stage": "guest-bootstrap",
            "awaiting_bootstrap": True,
            "steps": steps,
            "warnings": warnings,
            "message": (
                "Complete the one-time guest bootstrap: open the Windows console at "
                "http://127.0.0.1:8006, paste the PowerShell printed above, then "
                "re-run `wayseam setup`."
            ),
        }
    steps.append(_step("wait-agent", "ok", "guest agent is healthy"))

    # --- 5. Discover + menu + applet ----------------------------------------
    if discover is None or persist is None:
        from wayseam.guest.discovery import discover_apps as _discover_apps
        from wayseam.guest.discovery import persist_discovered as _persist_discovered

        discover = discover or _discover_apps
        persist = persist or _persist_discovered
    from wayseam.guest.discovery import DiscoveryError

    try:
        apps = discover(cfg, timeout=refresh_timeout)
    except DiscoveryError as exc:
        steps.append(_step("discover", "failed", str(exc)))
        return fail("discover", f"App discovery failed: {exc}")
    persist(apps)
    app_count = len(apps)
    steps.append(_step("discover", "ok", f"discovered {app_count} app(s)"))

    if sync is None:
        from wayseam.omarchy.menu import sync_menu

        sync = sync_menu
    visible = sync()
    from wayseam.desktop.icons import install_wayseam_icon

    install_wayseam_icon()
    steps.append(_step("menu", "ok", f"synced {visible} app(s) to the Omarchy menu"))

    if install_applet:
        if applet is None:
            from wayseam.omarchy.plugin import install_plugin

            applet = install_plugin
        from wayseam.omarchy.plugin import PluginError

        try:
            plugin_result = applet()
        except PluginError as exc:
            warnings.append(f"applet install skipped: {exc}")
            steps.append(_step("applet", "warning", str(exc)))
        else:
            for warning in plugin_result.get("warnings", []):
                warnings.append(warning)
            enabled = plugin_result.get("enabled")
            steps.append(
                _step("applet", "ok", "enabled" if enabled else "linked (enable manually)")
            )
    else:
        steps.append(_step("applet", "skipped", "--no-applet"))

    return {
        "ok": True,
        "stage": "done",
        "steps": steps,
        "apps": app_count,
        "warnings": warnings,
    }


def default_auth_probe(cfg: Any) -> None:
    """Raise ``AgentAuthError`` when the guest agent rejects our token."""
    AgentClient(cfg).exec("exit 0", timeout=20)
