"""Admin firmware upgrade, over either of the DUT's two upload paths (P72b).

    gui (default)  POST https://<dut>:443/submit.cgi
                   multipart/form-data, binary=<signed .sig image>
    api            PUT  https://<dut>:10443/ap/systemctl/sysFwUpgrade
                   Content-Type: application/octet-stream
                   body = the encrypted -encrypt_*.bin image

**The two are not interchangeable** -- each accepts only its own image type, per
the vendor. That is the whole reason both exist here; see the TRANSPORT_* block
below for how the gui contract was read off the device rather than guessed.

An earlier draft had the DUT fetch the image itself over serial, because the
only DUT API calls in the repo (scripts/sysMon.sh) target
`https://127.0.0.1:10443` -- which only reaches the DUT from scripts running ON
the DUT. Both real endpoints are reachable at the DUT's own LAN address, so the
backend uploads the body directly and serial is used only to confirm the flash
actually began.

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

# Two transports, because the DUT accepts a different IMAGE on each and the
# vendor confirmed (2026-07-29) they are not interchangeable:
#
#   api  the /ap/* management API. Takes the *encrypted* image
#        (`ubi_kernel_AP6_*-encrypt_*.bin`). Feeding it a signed .sig is what
#        produced `Can't open FW.signauture.st1` -- the transport was fine, the
#        image type was wrong.
#   gui  the same upload the web UI performs. Takes the *signed* image (.sig).
#
# The gui contract was read off the device itself (AP6_840E), not guessed:
# /www/html/fwupdate.html carries
#   <form enctype="multipart/form-data" method="POST" action="submit.cgi">
#     <input type="hidden" name="submitpg" value="fwupdate_pc.html">
#     <input type="hidden" name="CSRFToken" value="0">
#     <input type="hidden" name="decodepwd">
#     <input type="file" maxlength="31" name="binary">
# and /www/mongoose.config sets `document_root /www/html` with a rewrite
# `/submit.cgi=/www/cgi-bin/submit.cgi`, so the relative action resolves to
# /submit.cgi at the origin. Mongoose -- not nginx -- serves it on 443s, with
# `authentication_domain localhost`, which is the Digest realm both ports use.
TRANSPORT_API = "api"
TRANSPORT_GUI = "gui"
TRANSPORTS = (TRANSPORT_API, TRANSPORT_GUI)

UPGRADE_PATH = "/ap/systemctl/sysFwUpgrade"

GUI_UPLOAD_PATH = "/submit.cgi"
GUI_SUBMIT_PAGE = "fwupdate_pc.html"
GUI_FILE_FIELD = "binary"
# The web UI harvests its token from GET /cgi-bin/common.cgi?csrftoken=1 and
# reads SET_INFO.CSRFToken (see /www/html/csrf.htm). When a build has
# HTTP_SUPPORT_CSRF off, fwupdate.html *disables* the field so nothing is sent --
# so a missing token is a legal state and not automatically an error.
#
# BUT: that endpoint answers anonymous callers with 200 and a token-less body,
# which is indistinguishable from "CSRF is off". A captured browser upload proved
# CSRF is very much ON here (`CSRFToken: 1490240`), so the token fetch has to be
# authenticated -- see _prime_digest.
GUI_CSRF_PATH = "/cgi-bin/common.cgi"
# The page that hosts the upload form. Sent as Referer to mirror the browser
# request that works; CGI CSRF checks commonly look at it alongside the token.
GUI_REFERER_PAGE = "/fwupdate.html"
# The web server -- a vendor-patched Mongoose, NOT cgi_box -- holds a busy lock
# for ~3.5 minutes after any web-UI submit and answers /submit.cgi with
# `301 -> /busy.html` before the CGI ever runs. Nothing is received in that
# state. Measured on AP6_840E; after a flash and reboot the window is much
# longer (still locked 13 min later). Two upgrade attempts in quick succession
# hit this routinely, so it is a normal state, not an edge case.
GUI_BUSY_PAGE = "busy.html"

# The /ap/* API family answers on 10443, not 443. Verified on AP6_840E: :443
# serves the web UI and 404s every /ap path, while :10443 returns 200 -- and
# 10443 is also the only port the repo's own scripts/sysMon.sh ever calls. A
# management address given without a port therefore gets this one, not https's
# default 443, which would silently 404 at flash time. The gui transport is the
# web UI, so it defaults to 443 instead.
DEFAULT_MGMT_PORT = 10443
DEFAULT_GUI_PORT = 443

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


class FirmwareBusy(FirmwareError):
    """The DUT's web UI was locked and never looked at the image.

    Distinct from a refusal because the operator's next action is simply to
    wait. Reporting it as success is the dangerous case: the response looks
    like the accepted one, so an operator would be told a flash is under way
    when the device received nothing at all.
    """


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


def normalise_mgmt_url(value: str, transport: str = TRANSPORT_API) -> str:
    """Accept `1.2.3.4`, `1.2.3.4:10443`, or a full base URL; return an origin.

    Scheme defaults to https (both ports are TLS-only, with a self-signed cert --
    hence verify=False below). The default port depends on the transport: the
    /ap/* API lives on 10443, the web UI on 443. An explicit port is always kept,
    so one stored address can serve both transports if the device is unusual.
    """
    cleaned = (value or "").strip().rstrip("/")
    if not cleaned:
        return ""
    if "://" not in cleaned:
        cleaned = f"https://{cleaned}"
    parsed = urlsplit(cleaned)
    if parsed.port is None:
        port = DEFAULT_GUI_PORT if transport == TRANSPORT_GUI else DEFAULT_MGMT_PORT
        cleaned = urlunsplit(
            (parsed.scheme, f"{parsed.hostname}:{port}", parsed.path, "", "")
        ).rstrip("/")
    return cleaned


def normalise_mgmt_host(value: str) -> str:
    """Normalise an address for STORAGE: scheme filled in, port left alone.

    Deliberately does not default the port, unlike normalise_mgmt_url. One stored
    address has to serve both transports, which listen on different ports, so
    baking either one in at save time would send the other transport to the wrong
    port. An explicit port is still honoured -- writing it out is how an operator
    pins an unusual device.
    """
    cleaned = (value or "").strip().rstrip("/")
    if not cleaned:
        return ""
    if "://" not in cleaned:
        cleaned = f"https://{cleaned}"
    parsed = urlsplit(cleaned)
    host = f"{parsed.hostname}:{parsed.port}" if parsed.port else str(parsed.hostname)
    return urlunsplit((parsed.scheme, host, parsed.path, "", "")).rstrip("/")


def image_kind(filename: str) -> str:
    """Classify an image by name: 'signed', 'encrypted', or 'unknown'."""
    lowered = (filename or "").lower()
    if lowered.endswith(".sig"):
        return "signed"
    if "-encrypt_" in lowered and lowered.endswith(".bin"):
        return "encrypted"
    return "unknown"


def check_image_for_transport(filename: str, transport: str) -> None:
    """Refuse a known-wrong pairing before anything reaches the DUT.

    The vendor's rule is absolute: the API takes the encrypted image, the web UI
    takes the signed one. Sending the other is not a soft failure -- it cost a
    whole real-DUT session to diagnose as `Can't open FW.signauture.st1` -- so it
    is rejected up front with the reason. An unrecognised name is allowed
    through: this heuristic must not block an image the vendor names differently.
    """
    kind = image_kind(filename)
    if transport == TRANSPORT_GUI and kind == "encrypted":
        raise FirmwareError(
            f"'{filename}' looks like an encrypted image, which only the management"
            " API accepts. The web-UI transport needs the signed .sig image."
        )
    if transport == TRANSPORT_API and kind == "signed":
        raise FirmwareError(
            f"'{filename}' is a signed image, which only the web UI accepts. The"
            " management API needs the encrypted '-encrypt_*.bin' image."
        )


def _prime_digest(client: httpx.Client, origin: str, auth: httpx.Auth) -> None:
    """Make one request that DOES challenge, so `auth` learns the Digest nonce.

    httpx's DigestAuth is challenge-driven: it sends a request unauthenticated
    and only adds Authorization after a 401. `common.cgi` never challenges -- it
    answers 200 to anonymous callers -- so a token fetch on a fresh client is
    silently ANONYMOUS, and the DUT then returns a token-less body that looks
    exactly like "this build has CSRF disabled". That misreading is what made the
    first real flash fail with 577.

    `/` does challenge, and after one round trip DigestAuth reuses the cached
    challenge pre-emptively on later requests from the same auth object. Failure
    here is not fatal: the caller still tries, and the upload itself challenges
    normally.
    """
    try:
        client.get(origin + "/", auth=auth)
    except httpx.HTTPError:
        pass


def _fetch_csrf_token(client: httpx.Client, origin: str, auth: httpx.Auth) -> str | None:
    """Token for the web-UI upload, or None when this build really has CSRF off.

    Mirrors what the page itself does. **The request must be authenticated** --
    see _prime_digest for why that is not automatic here. A failure to read one
    is not fatal: the upload is attempted without it, and the DUT is the
    authority on whether that is acceptable -- guessing a token would be worse
    than omitting it.
    """
    _prime_digest(client, origin, auth)
    try:
        response = client.get(
            urljoin(origin + "/", GUI_CSRF_PATH.lstrip("/")),
            params={"csrftoken": 1},
            auth=auth,
        )
        if response.status_code in (401, 403):
            raise FirmwareAuthError(
                f"The DUT rejected the credentials while fetching a CSRF token"
                f" ({response.status_code})."
            )
        if response.status_code >= 400:
            return None
        token = (response.json() or {}).get("SET_INFO", {}).get("CSRFToken")
    except FirmwareAuthError:
        raise
    except (httpx.HTTPError, ValueError, AttributeError, TypeError):
        return None
    if token in (None, "", 0, "0"):
        return None
    return str(token)


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
    transport: str = TRANSPORT_GUI,
) -> dict:
    """Verify the image, then upload it to the DUT. Runs on a worker thread.

    Checksum first, deliberately: an upload that starts before the bytes are
    verified has already put a bad image on the wire.

    `transport` picks which of the DUT's two upload paths to use, and they take
    different images -- see the TRANSPORT_* notes at the top of this module. It
    defaults to the web-UI route because that is the one the signed images we
    actually hold will flash.
    """
    if transport not in TRANSPORTS:
        raise FirmwareError(f"Unknown firmware transport: {transport}")
    dry = is_dry_run() if dry_run is None else dry_run
    origin = normalise_mgmt_url(mgmt_url, transport)
    path = file_row["filepath"]

    on_progress({"stage": "verifying", "detail": file_row["filename"], "dry_run": dry})
    check_image_for_transport(file_row["filename"], transport)
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

    upload_path = GUI_UPLOAD_PATH if transport == TRANSPORT_GUI else UPGRADE_PATH
    url = urljoin(origin + "/", upload_path.lstrip("/"))
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
            "transport": transport,
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
            # Digest, not Basic: the DUT answers an unauthenticated request with
            # `WWW-Authenticate: Digest qop="auth"`, and Basic credentials are
            # simply rejected (verified on AP6_840E, on both ports).
            auth = httpx.DigestAuth(user, password)
            if transport == TRANSPORT_GUI:
                token = _fetch_csrf_token(client, origin, auth)
                # Field order mirrors the DOM order of form_pc_firmup in
                # /www/html/fwupdate_sec.html -- submitpg, CSRFToken, decodepwd,
                # then the file. /submit.cgi is a C multi-call binary (cgi_box,
                # via cgi_multi_recv) that segfaults on a bare GET, so it is
                # worth matching the browser part for part rather than assuming
                # it parses multipart order-independently.
                fields = {"submitpg": GUI_SUBMIT_PAGE}
                if token is not None:
                    fields["CSRFToken"] = token
                fields["decodepwd"] = ""
                response = client.post(
                    url,
                    data=fields,
                    # Mirrors the browser request that the DUT accepts. Origin and
                    # Referer travel with any real form post, and a CGI CSRF check
                    # that inspects them would otherwise see a request that looks
                    # cross-site.
                    headers={
                        "Origin": origin,
                        "Referer": urljoin(origin + "/", GUI_REFERER_PAGE.lstrip("/")),
                    },
                    # httpx builds the multipart body and its own Content-Type
                    # boundary; setting that header by hand would break it.
                    files={
                        GUI_FILE_FIELD: (
                            file_row["filename"],
                            _read_image(path),
                            "application/octet-stream",
                        )
                    },
                    auth=auth,
                )
            else:
                response = client.put(
                    url,
                    content=_read_image(path),
                    # No Expect header at all. curl adds `Expect: 100-continue`
                    # for large bodies, which is why the documented curl passes
                    # `-H "Expect:"` to REMOVE it -- but httpx never adds it, and
                    # an empty string here is sent as a literal empty header,
                    # which the DUT answers with 417 (seen on AP6_840E).
                    headers={"Content-Type": "application/octet-stream"},
                    auth=auth,
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
            f"The DUT rejected the credentials ({response.status_code})."
            " Check whether this device is still on its expected defaults."
        )
    if response.status_code >= 400:
        raise FirmwareError(
            f"The DUT refused the upgrade ({response.status_code}): {response.text[:200]}"
        )

    if transport == TRANSPORT_GUI:
        # A busy redirect is a 3xx, so it sails past the >= 400 check above and
        # would otherwise be reported as an accepted upgrade.
        target = response.headers.get("location", "").rsplit("/", 1)[-1].split("?", 1)[0].lower()
        if target == GUI_BUSY_PAGE:
            raise FirmwareBusy(
                f"The DUT's web UI is busy and did not take the image (it answered"
                f" {response.status_code} and redirected to {GUI_BUSY_PAGE}). It locks for a"
                " few minutes after any web-UI submit, and for longer after a flash — wait"
                " and try again."
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
        "transport": transport,
        "sha256": actual,
        "size": file_row["size"],
        "status": response.status_code,
        "flash_started": started,
        "detail": detail,
    }
