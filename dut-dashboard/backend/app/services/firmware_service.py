"""Admin firmware upgrade against the DUT's management API (P72b).

    PUT https://<dut>/ap/systemctl/sysFwUpgrade
    Content-Type: application/octet-stream
    body = the customer-signed .sig image

An earlier draft had the DUT fetch the image itself over serial, because the
only DUT API calls in the repo (scripts/sysMon.sh) target
`https://127.0.0.1:10443` -- which only reaches the DUT from scripts running ON
the DUT. The real endpoint is reachable at the DUT's own LAN address, so the
backend uploads the body directly and serial is not involved at all.

Two things are deliberately not in this file:
  * credentials -- read from settings/env, never source, and never logged;
  * a default management address -- unset means refuse, because PUTting a
    firmware image at a guessed host is not a mistake worth risking.
"""

from __future__ import annotations

import hashlib
import os
import time
from typing import Callable
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from app.db import workspace

UPGRADE_PATH = "/ap/systemctl/sysFwUpgrade"

# The /ap/* API family answers on 10443, not 443. Verified on AP6_840E: :443
# serves the web UI and 404s every /ap path, while :10443 returns 200 -- and
# 10443 is also the only port the repo's own scripts/sysMon.sh ever calls. A
# management address given without a port therefore gets this one, not https's
# default 443, which would silently 404 at flash time.
DEFAULT_MGMT_PORT = 10443

_USER_KEY = "dut_api_user"
_PASSWORD_KEY = "dut_api_password"
_USER_ENV = "DUT_API_USER"
_PASSWORD_ENV = "DUT_API_PASSWORD"
_DRY_RUN_ENV = "DUT_FIRMWARE_DRY_RUN"

# The DUT writes flash while the request is open, so the read timeout is long;
# connect stays short so an unreachable address fails fast instead of hanging.
UPLOAD_TIMEOUT = httpx.Timeout(connect=10.0, read=900.0, write=900.0, pool=10.0)

STAGES = ("verifying", "connecting", "uploading", "applying", "done")

# The DUT prints this on its serial console when a real upgrade starts. A 200
# from the API only means the request was accepted; this is the only evidence
# the device actually began flashing, so it is checked rather than assumed.
FLASH_STARTED_MARKER = "wifix_downloader.sh"
FLASH_START_WAIT_SECONDS = 30.0
_FLASH_POLL_SECONDS = 1.0

_CHUNK = 1024 * 1024


class FirmwareError(RuntimeError):
    """Refused before the DUT was touched."""


class FirmwareAuthError(FirmwareError):
    """The DUT rejected the credentials.

    Raised rather than retried or fallen back on: per the operator, a refusal
    here means the device is not on the expected defaults, and that is a
    finding to report, not something to work around.
    """


class ChecksumMismatch(FirmwareError):
    """The image on disk is not the image the operator expected."""


class FirmwareRejected(FirmwareError):
    """The DUT received the image and its upgrade handler refused it.

    Distinct from a transport failure because the operator's next step is
    completely different: the image or the device state is wrong, not the
    network. Seen on AP6_840E, which reports such failures as a bare error line
    where an HTTP header belongs, producing a malformed response.
    """


# --------------------------------------------------------------------------
# Configuration


def get_credentials() -> tuple[str, str]:
    """DUT API user/password from settings, falling back to env.

    Never defaulted and never hardcoded: the repo is shared, and a factory
    password committed to it is a password published to everyone who clones it.
    """
    user_row = workspace.query_one("SELECT value FROM settings WHERE key = ?", (_USER_KEY,))
    pass_row = workspace.query_one("SELECT value FROM settings WHERE key = ?", (_PASSWORD_KEY,))
    user = str(user_row["value"]) if user_row is not None else os.getenv(_USER_ENV, "")
    password = str(pass_row["value"]) if pass_row is not None else os.getenv(_PASSWORD_ENV, "")
    return user, password


def set_credentials(user: str, password: str) -> None:
    workspace.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (_USER_KEY, user)
    )
    workspace.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (_PASSWORD_KEY, password)
    )


def has_credentials() -> bool:
    user, password = get_credentials()
    return bool(user and password)


def is_dry_run() -> bool:
    return os.getenv(_DRY_RUN_ENV, "").strip() not in ("", "0", "false", "False")


def normalise_mgmt_url(value: str) -> str:
    """Accept `1.2.3.4`, `1.2.3.4:10443`, or a full base URL; return an origin.

    Scheme defaults to https (the management API is TLS-only, with a self-signed
    cert -- hence verify=False below) and the port to DEFAULT_MGMT_PORT. An
    explicit port is always kept, so a device that really does serve /ap on 443
    can be pointed at by writing it out.
    """
    cleaned = (value or "").strip().rstrip("/")
    if not cleaned:
        return ""
    if "://" not in cleaned:
        cleaned = f"https://{cleaned}"
    parsed = urlsplit(cleaned)
    if parsed.port is None:
        cleaned = urlunsplit(
            (parsed.scheme, f"{parsed.hostname}:{DEFAULT_MGMT_PORT}", parsed.path, "", "")
        ).rstrip("/")
    return cleaned


# --------------------------------------------------------------------------
# Checksum


def file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_checksum(recorded: str | None, path: str, expected: str | None) -> str:
    """Re-hash the bytes on disk and check them against what is expected.

    The stored digest is not trusted on its own: it proves what was uploaded,
    not what is on disk now. `expected` is the customer's published checksum,
    optional but compared when given.
    """
    actual = file_sha256(path)
    if recorded and recorded.lower() != actual:
        raise ChecksumMismatch(
            "The file on disk no longer matches the checksum recorded at upload."
        )
    if expected:
        wanted = expected.strip().lower()
        if wanted != actual:
            raise ChecksumMismatch(
                f"Checksum mismatch: expected {wanted}, file is {actual}."
            )
    return actual


# --------------------------------------------------------------------------
# The upgrade


def wait_for_flash_start(
    console_lines: Callable[[], list[str]],
    timeout: float | None = None,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Watch the serial console for the DUT's own "upgrade starting" line.

    Returns False on timeout rather than raising: the upgrade may still be
    running, and the honest report is "accepted, but not observed starting" --
    not a failure the operator should act on by power-cycling a flashing device.

    `timeout` resolves at call time, not as a default argument: a default would
    bind FLASH_START_WAIT_SECONDS once at import and silently ignore any later
    change to it.
    """
    deadline = now() + (FLASH_START_WAIT_SECONDS if timeout is None else timeout)
    while True:
        for line in console_lines():
            if FLASH_STARTED_MARKER in line:
                return True
        if now() >= deadline:
            return False
        sleep(_FLASH_POLL_SECONDS)


def _read_image(path: str) -> bytes:
    """Load the image into memory for the PUT.

    Deliberately not a streaming generator: Digest auth sends the request twice
    (unauthenticated probe, then the authenticated retry), and a generator body
    is consumed by the first attempt -- which risks handing a device that is
    about to flash itself an empty image. At 32-38 MB, and capped by
    MAX_UPLOAD_BYTES, buying that guarantee with memory is the right trade.
    """
    with open(path, "rb") as handle:
        return handle.read()


def run_upgrade(
    mgmt_url: str,
    file_row: dict,
    on_progress: Callable[[dict], None],
    expected_sha256: str | None = None,
    dry_run: bool | None = None,
    console_lines: Callable[[], list[str]] | None = None,
    client_factory: Callable[[], httpx.Client] | None = None,
) -> dict:
    """Verify the image, then PUT it to the DUT. Runs on a worker thread.

    Checksum first, deliberately: an upload that starts before the bytes are
    verified has already put a bad image on the wire.
    """
    dry = is_dry_run() if dry_run is None else dry_run
    origin = normalise_mgmt_url(mgmt_url)
    path = file_row["filepath"]

    on_progress({"stage": "verifying", "detail": file_row["filename"], "dry_run": dry})
    actual = verify_checksum(file_row.get("sha256"), path, expected_sha256)

    if not origin:
        raise FirmwareError(
            "No management address configured for this DUT. Set it before upgrading."
        )
    if not dry and not has_credentials():
        raise FirmwareError(
            "No DUT API credentials configured. Set them in the Firmware section"
            f" (or {_USER_ENV}/{_PASSWORD_ENV}) before upgrading."
        )

    url = urljoin(origin + "/", UPGRADE_PATH.lstrip("/"))
    on_progress({"stage": "connecting", "detail": url, "dry_run": dry})

    if dry:
        on_progress(
            {"stage": "uploading", "detail": "dry run — nothing sent", "dry_run": True}
        )
        on_progress({"stage": "applying", "detail": "dry run", "dry_run": True})
        on_progress({"stage": "done", "detail": "dry run complete", "dry_run": True})
        return {
            "ok": True,
            "dry_run": True,
            "url": url,
            "sha256": actual,
            "size": file_row["size"],
        }

    user, password = get_credentials()
    on_progress(
        {
            "stage": "uploading",
            "detail": f"{file_row['filename']} → {url} (do not power off)",
            "dry_run": False,
        }
    )

    # verify=False: the DUT serves its management API with a self-signed cert,
    # the same reason scripts/sysMon.sh uses `curl -k`.
    build = client_factory or (lambda: httpx.Client(verify=False, timeout=UPLOAD_TIMEOUT))
    try:
        with build() as client:
            response = client.put(
                url,
                content=_read_image(path),
                # No Expect header at all. curl adds `Expect: 100-continue` for
                # large bodies, which is why the documented curl passes
                # `-H "Expect:"` to REMOVE it -- but httpx never adds it, and an
                # empty string here is sent as a literal empty header, which the
                # DUT answers with 417 Expectation Failed (seen on AP6_840E).
                headers={"Content-Type": "application/octet-stream"},
                # Digest, not Basic: the DUT answers an unauthenticated request
                # with `WWW-Authenticate: Digest qop="auth"`, and Basic
                # credentials are simply rejected (verified on AP6_840E).
                auth=httpx.DigestAuth(user, password),
            )
    except httpx.RemoteProtocolError as exc:
        # The bytes arrived and the DUT answered -- just not with valid HTTP.
        # Its complaint is the only diagnostic there is, so surface it verbatim
        # rather than calling this a connectivity problem, which it is not.
        raise FirmwareRejected(
            f"The DUT received the image but its upgrade handler refused it: {exc}"
        ) from exc
    except httpx.HTTPError as exc:
        raise FirmwareError(f"Could not reach the DUT at {url}: {exc}") from exc

    if response.status_code in (401, 403):
        raise FirmwareAuthError(
            f"The DUT rejected the API credentials ({response.status_code})."
            " Check whether this device is still on its expected defaults."
        )
    if response.status_code >= 400:
        raise FirmwareError(
            f"The DUT refused the upgrade ({response.status_code}): {response.text[:200]}"
        )

    # A 200 only says the DUT accepted the bytes. Confirm it actually started by
    # watching its console for the marker it prints when the flash begins.
    on_progress(
        {"stage": "applying", "detail": "waiting for the DUT to start flashing", "dry_run": False}
    )
    started = wait_for_flash_start(console_lines) if console_lines is not None else None
    if started is False:
        detail = (
            f"upgrade accepted, but '{FLASH_STARTED_MARKER}' was not seen on the console"
            f" within {int(FLASH_START_WAIT_SECONDS)}s — check the DUT before retrying"
        )
    elif started:
        detail = f"DUT is flashing ({FLASH_STARTED_MARKER} seen)"
    else:
        detail = "upgrade accepted (no console attached to confirm)"
    on_progress({"stage": "done", "detail": detail, "dry_run": False})
    return {
        "ok": True,
        "dry_run": False,
        "url": url,
        "sha256": actual,
        "size": file_row["size"],
        "status": response.status_code,
        "flash_started": started,
        "detail": detail,
    }
