"""Admin firmware upgrade: hand the DUT a URL and tell it to flash (P72b).

Transport, decided 2026-07-28 after the roadmap's original plan turned out not
to work: the backend has no network path to the DUT -- every interaction goes
over the serial console -- and the `https://127.0.0.1:10443` calls in
scripts/sysMon.sh only reach the DUT because that script RUNS ON the DUT. From
the dashboard host, 127.0.0.1 is the dashboard host.

So the upgrade is driven the same way as every other DUT command: the image is
published on the dashboard's own HTTP port behind a single-use token, and the
DUT is told over serial to fetch and flash it. Serial itself is far too slow to
carry a multi-megabyte image at 115200 baud.

The flash command is NOT hardcoded. `upgrade_command_template` comes from
settings/env and is empty by default, because guessing an upgrade endpoint on
real hardware risks bricking it. Empty means the real flash is refused; dry-run
still works, so the whole UI is exercisable without a configured DUT.
"""

from __future__ import annotations

import os
import secrets
import socket
import threading
import time
from typing import Callable

from app.db import workspace

# settings key + env fallback, mirroring the passcode idiom in auth_service.
_TEMPLATE_KEY = "firmware_upgrade_cmd"
_TEMPLATE_ENV = "DUT_FIRMWARE_UPGRADE_CMD"
_HOST_IP_ENV = "DUT_HOST_LAN_IP"
_DRY_RUN_ENV = "DUT_FIRMWARE_DRY_RUN"

# How long the image URL stays fetchable. Generous: a slow LAN pull on a busy AP
# is normal, and the token is single-DUT, single-upgrade anyway.
IMAGE_TOKEN_TTL_SECONDS = 30 * 60

# The flash itself can outlast any sane serial RPC, so the command is issued
# with a long ceiling; the DUT usually resets before it returns.
FLASH_TIMEOUT_SECONDS = 600.0

# Ordered stages, so the UI can render a determinate bar without inventing one.
STAGES = ("preparing", "publishing", "instructing", "flashing", "done")

_image_tokens: dict[str, dict] = {}
_lock = threading.Lock()


class FirmwareError(RuntimeError):
    """Anything that stops an upgrade before the DUT is touched."""


# --------------------------------------------------------------------------
# Configuration


def get_upgrade_template() -> str:
    """Shell command run ON the DUT, with `{url}` substituted.

    Empty means no upgrade endpoint has been configured, and the real flash is
    refused rather than guessed -- a wrong command on real hardware bricks it.
    """
    row = workspace.query_one("SELECT value FROM settings WHERE key = ?", (_TEMPLATE_KEY,))
    if row is not None:
        return str(row["value"])
    return os.getenv(_TEMPLATE_ENV, "")


def set_upgrade_template(template: str) -> None:
    workspace.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (_TEMPLATE_KEY, template),
    )


def is_dry_run() -> bool:
    """Dry run streams the real stages but never issues the flash command."""
    return os.getenv(_DRY_RUN_ENV, "").strip() not in ("", "0", "false", "False")


def host_lan_ip() -> str:
    """The dashboard's LAN address, as the DUT would have to reach it.

    The UDP connect never sends a packet; it just asks the routing table which
    local address would be used, which is the one a device on the LAN can reach.
    """
    override = os.getenv(_HOST_IP_ENV, "").strip()
    if override:
        return override
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


# --------------------------------------------------------------------------
# Single-use image tokens


def publish_image(path: str, ttl: float = IMAGE_TOKEN_TTL_SECONDS) -> str:
    """Make `path` fetchable once, via an unguessable token.

    The DUT's curl carries no session cookie, so the image cannot sit behind the
    engineer gate; a single-use expiring token is what stands in for that.
    """
    token = secrets.token_urlsafe(24)
    with _lock:
        _image_tokens[token] = {"path": path, "expires_at": time.time() + ttl, "used": False}
    return token


def claim_image(token: str) -> str | None:
    """Resolve a token to its path exactly once; None if unknown/expired/used."""
    now = time.time()
    with _lock:
        entry = _image_tokens.get(token)
        if entry is None or entry["used"] or entry["expires_at"] <= now:
            return None
        entry["used"] = True
        return str(entry["path"])


def revoke_image(token: str) -> None:
    with _lock:
        _image_tokens.pop(token, None)


def _purge_expired(now: float | None = None) -> None:
    stamp = time.time() if now is None else now
    with _lock:
        for token in [t for t, e in _image_tokens.items() if e["expires_at"] <= stamp]:
            del _image_tokens[token]


# --------------------------------------------------------------------------
# The upgrade itself


def run_upgrade(
    worker,
    file_row: dict,
    port: int,
    on_progress: Callable[[dict], None],
    dry_run: bool | None = None,
) -> dict:
    """Publish the image, tell the DUT to flash it, and stream stage events.

    Runs on a worker thread: the flash command occupies the serial link for
    minutes, and `on_progress` is the same emit-from-thread bridge the site
    survey uses.
    """
    dry = is_dry_run() if dry_run is None else dry_run
    template = get_upgrade_template()
    if not dry and not template:
        raise FirmwareError(
            "No upgrade command configured. Set the DUT's upgrade endpoint in"
            f" settings ({_TEMPLATE_KEY}) or {_TEMPLATE_ENV} before flashing."
        )

    _purge_expired()
    on_progress({"stage": "preparing", "detail": file_row["filename"], "dry_run": dry})

    token = publish_image(file_row["filepath"])
    url = f"http://{host_lan_ip()}:{port}/api/firmware/image/{token}"
    on_progress({"stage": "publishing", "detail": url, "dry_run": dry})

    try:
        command = template.replace("{url}", url) if template else ""
        on_progress({"stage": "instructing", "detail": command or "(dry run)", "dry_run": dry})

        if dry:
            # Stream the shape of a real run without touching the DUT, so the
            # UI is verifiable on hardware nobody wants to risk.
            on_progress({"stage": "flashing", "detail": "dry run — no command sent", "dry_run": True})
            time.sleep(0.1)
            on_progress({"stage": "done", "detail": "dry run complete", "dry_run": True})
            return {"ok": True, "dry_run": True, "url": url, "command": command}

        on_progress({"stage": "flashing", "detail": "command sent — do not power off", "dry_run": False})
        output = worker.capture_command(command, timeout=FLASH_TIMEOUT_SECONDS)
        on_progress({"stage": "done", "detail": "DUT reported completion", "dry_run": False})
        return {"ok": True, "dry_run": False, "url": url, "command": command, "output": output}
    finally:
        # One upgrade, one fetch: the token dies with the attempt either way.
        revoke_image(token)
