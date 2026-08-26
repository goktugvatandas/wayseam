# SPDX-License-Identifier: MIT
"""OEM recovery for a guest whose first-boot ``/oem`` copy failed.

Ported from Wayseam ``cli.pod._recover_oem`` (see upstream/PROVENANCE).
"""

from __future__ import annotations

import sys

from wayseam.config import Config
from wayseam.paths import guest_scripts_dir


def tr(text: str) -> str:  # i18n dropped in the port
    return text


def recover_oem() -> None:
    """Recover from a failed dockur OEM-copy by re-staging C:\\OEM\\ manually.

    Workaround for #287: in some host environments dockur's first-boot
    ``/oem -> C:\\OEM\\`` copy silently fails, leaving the guest with
    no ``install.bat``, no agent, and no rdprrap. The host then sees
    port 8765 RST and ``wayseam pod wait-ready`` times out.

    This command:

    1. Verifies the container is running and ``/oem/install.bat`` is
       present inside the container (i.e. host-side OEM mount is OK
       and only the guest-side copy is what failed).
    2. Tars ``/oem`` to ``/storage/oem.tar.gz`` inside the container.
    3. Starts a one-shot Python HTTP server on container port 8766
       (reachable from the Windows guest via QEMU's NAT gateway
       ``10.0.2.2``).
    4. Prints the exact PowerShell commands the user must paste into
       the noVNC console to download, extract, and run ``install.bat``.

    We do not push to the guest automatically because the failure mode
    of #287 leaves the agent dead -- there is no working host->guest
    channel. noVNC PowerShell paste is the only reliable path until the
    agent is up.
    """
    import shutil as _shutil
    import subprocess
    import time

    cfg = Config.load()
    container = cfg.pod.container_name
    backend_name = cfg.pod.backend

    if backend_name == "omarchy":
        recover_omarchy_oem(cfg)
        return

    if backend_name not in ("podman", "docker"):
        print(
            tr(
                "Error: recover-oem only supports podman/docker backends "
                "(current: {backend}). For the manual backend, "
                "copy /oem into the guest manually."
            ).format(backend=backend_name)
        )
        sys.exit(1)

    cmd = backend_name
    if not _shutil.which(cmd):
        print(tr("Error: {cmd} not found on PATH.").format(cmd=cmd))
        sys.exit(1)

    print(
        tr("[Wayseam] Checking container '{container}' is running...").format(container=container)
    )
    try:
        result = subprocess.run(
            [
                cmd,
                "ps",
                "--filter",
                f"name={container}",
                "--filter",
                "status=running",
                "--format",
                "{{.Names}}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        print(tr("Error: '{cmd} ps' timed out (10s).").format(cmd=cmd))
        sys.exit(1)
    if container not in result.stdout:
        print(
            tr(
                "Error: container '{container}' not running."
                " Start it first with 'wayseam pod start'."
            ).format(container=container)
        )
        sys.exit(1)

    print(tr("[Wayseam] Verifying /oem/install.bat exists inside container..."))
    try:
        check = subprocess.run(
            [cmd, "exec", container, "sh", "-c", "test -f /oem/install.bat"],
            capture_output=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        print(tr("Error: container exec timed out (10s)."))
        sys.exit(1)
    if check.returncode != 0:
        print(
            tr(
                "Error: /oem/install.bat not found inside container. "
                "The host-side OEM mount itself is missing -- recreate "
                "the pod with 'wayseam pod recreate' before retrying."
            )
        )
        sys.exit(1)

    # Tar /oem into a DEDICATED serve dir (not /storage) so the HTTP
    # server below exposes only oem.tar.gz -- never /storage/data.img
    # (the multi-GB Windows disk) or anything else in /storage. The OEM
    # tarball contains agent_token.txt; the guest already holds that
    # token (it's at C:\OEM\agent_token.txt), so this doesn't cross a
    # new trust boundary, but serving the whole /storage would.
    serve_dir = "/tmp/wayseam-recover"
    print(
        tr("[Wayseam] Tarring /oem into {serve_dir}/oem.tar.gz inside container...").format(
            serve_dir=serve_dir
        )
    )
    try:
        subprocess.run(
            [
                cmd,
                "exec",
                container,
                "sh",
                "-c",
                f"rm -rf {serve_dir} && mkdir -p {serve_dir} && cd / && "
                f"tar czf {serve_dir}/oem.tar.gz oem && ls -la {serve_dir}/oem.tar.gz",
            ],
            check=True,
            timeout=60,
        )
    except subprocess.CalledProcessError as e:
        print(tr("Error: tar failed (rc={rc}).").format(rc=e.returncode))
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print(tr("Error: tar timed out (60s)."))
        sys.exit(1)

    # Port 8766: one above the wayseam agent port (8765) so anyone
    # debugging with `lsof -i :876*` sees both. Container-internal only;
    # not forwarded to the host. Reachable from the Windows guest via
    # QEMU's NAT gateway 10.0.2.2:8766. Serves only the dedicated
    # recover dir (one file), not /storage.
    print(tr("[Wayseam] Starting HTTP server on container port 8766..."))
    # Best-effort cleanup of any prior server on 8766.
    subprocess.run(
        [
            cmd,
            "exec",
            container,
            "sh",
            "-c",
            "pkill -f 'http.server 8766' 2>/dev/null; true",
        ],
        capture_output=True,
        timeout=5,
    )
    time.sleep(1)
    # `-d` detaches the exec so the server keeps running after we return.
    subprocess.run(
        [
            cmd,
            "exec",
            "-d",
            container,
            "sh",
            "-c",
            f"cd {serve_dir} && nohup python3 -m http.server 8766 "
            ">/tmp/recover-oem-http.log 2>&1 &",
        ],
        timeout=10,
    )
    time.sleep(2)

    print()
    print("=" * 70)
    print(tr("Paste these commands into the Windows guest via noVNC PowerShell:"))
    print()
    print("  noVNC URL: http://127.0.0.1:8007/")
    print()
    print(tr("  # Download OEM bundle from the container via the guest's default"))
    print(tr("  # gateway (QEMU slirp = 10.0.2.2, podman bridge = 10.89.0.1, etc.)"))
    print(
        "  $gw = (Get-NetRoute -DestinationPrefix '0.0.0.0/0' | "
        "Sort-Object RouteMetric | Select-Object -First 1).NextHop"
    )
    print('  Invoke-WebRequest -UseBasicParsing "http://${gw}:8766/oem.tar.gz" -OutFile C:\\oem.tar.gz')
    print()
    print(tr("  # Extract to C:\\OEM\\ (Windows 10/11 ships bsdtar in System32)"))
    print("  cd C:\\")
    print("  tar -xzf C:\\oem.tar.gz")
    print()
    print(tr("  # Verify: should list install.bat, agent\\, rdprrap\\, scripts\\, etc."))
    print("  dir C:\\OEM")
    print()
    print(tr("  # Run install.bat -- guest will reboot at the end"))
    print("  C:\\OEM\\install.bat")
    print()
    print("=" * 70)
    print()
    print(tr("After the post-install reboot, on this host:"))
    print("  wayseam pod wait-ready")
    print("  wayseam doctor")
    print()
    print(tr("To stop the HTTP server when finished:"))
    print(f"  {cmd} exec {container} pkill -f 'http.server 8766'")


def recover_omarchy_oem(cfg) -> None:  # type: ignore[no-untyped-def]
    """Stage the OEM bundle for an adopted Omarchy VM without editing it."""
    import hashlib
    import shutil
    import subprocess

    from wayseam.vm.backend import OEM_ARCHIVE_PATH, OEM_SERVE_DIR, stage_oem_archive

    container = cfg.pod.container_name
    if not shutil.which("docker"):
        print(tr("Error: docker not found on PATH."))
        sys.exit(1)

    running = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", container],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if running.returncode != 0 or running.stdout.strip() != "true":
        print(
            tr(
                "Error: container '{container}' not running."
                " Start it first with 'wayseam pod start'."
            ).format(container=container)
        )
        sys.exit(1)

    print(tr("[Wayseam] Staging a private OEM bundle in the Omarchy container..."))
    stage_oem_archive(cfg)

    archive = subprocess.run(
        ["docker", "exec", container, "cat", OEM_ARCHIVE_PATH],
        check=True,
        capture_output=True,
        timeout=60,
    ).stdout
    digest = hashlib.sha256(archive).hexdigest()
    subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            container,
            "sh",
            "-c",
            f"cat > {OEM_ARCHIVE_PATH}.sha256",
        ],
        input=f"{digest}  oem.tar.gz\n".encode("ascii"),
        check=True,
        timeout=10,
    )

    bootstrap = guest_scripts_dir() / "omarchy_bootstrap.ps1"
    if not bootstrap.is_file():
        print(tr("Error: Omarchy guest bootstrap script is missing."))
        sys.exit(1)
    subprocess.run(
        ["docker", "cp", str(bootstrap), f"{container}:{OEM_SERVE_DIR}/bootstrap.ps1"],
        check=True,
        timeout=30,
    )

    subprocess.run(
        ["docker", "exec", container, "pkill", "-f", "python3 -m http.server 8766"],
        capture_output=True,
        timeout=5,
    )
    subprocess.run(
        [
            "docker",
            "exec",
            "-d",
            container,
            "python3",
            "-m",
            "http.server",
            "8766",
            "--directory",
            OEM_SERVE_DIR,
        ],
        check=True,
        timeout=10,
    )

    print()
    print("=" * 70)
    print(tr("In the Windows noVNC console (http://127.0.0.1:8006/),"))
    print(tr("open Administrator PowerShell and paste:"))
    print()
    print(
        "  $gw=(Get-NetRoute -DestinationPrefix '0.0.0.0/0' | "
        "Sort-Object RouteMetric | Select-Object -First 1).NextHop; "
        'iex((New-Object Net.WebClient).DownloadString("http://${gw}:8766/bootstrap.ps1"))'
    )
    print()
    print(tr("The script verifies the archive hash before running install.bat."))
    print("=" * 70)


