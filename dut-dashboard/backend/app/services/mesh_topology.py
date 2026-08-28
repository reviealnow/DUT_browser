"""The DUT's own view of the mesh, read from its management API.

`GET /ap/info/wireless/mesh` answers with every member of the mesh at once --
the root and each node, carrying that member's hop count, LAN address and the
signal it hears its parent at.

That is a different question from the one `fleet_api.capture_rssi` asks, and
this module does not replace it. The capture measures ONE console's two link
directions with `iwconfig` and `wlanconfig`, so the fleet only ever learns about
DUTs this dashboard holds a console for: a mesh member nobody registered is
invisible to it. It was invisible in the UI too, while being plainly listed on
the device's own console -- which is the gap this exists to close. What the
capture still has and this does not is per-child RSSI and SNR measured off the
local radio; what this has is the topology as the DUT itself believes it,
members we cannot reach included.

The DUT-side plumbing is reused from `firmware_service` rather than restated:
the /ap/* port, digest auth, the self-signed certificate, and the credentials
(settings, then env, never source). Those helpers are management-API plumbing
rather than anything firmware-specific and would sit better in a shared module.
That extraction is worth doing on its own -- not as a side effect of adding a
read to the transport whose failures are the most expensive in this codebase.
"""

from __future__ import annotations

import json
from urllib.parse import urljoin

import httpx

from app.services import firmware_service
from app.services.wifi_clients import signal_band

MESH_PATH = "/ap/info/wireless/mesh"

# The operator's own curl asks for this, and the vendor API family is documented
# that way; it answers JSON regardless. Sent so this request is the one that was
# verified by hand rather than a near-relative of it.
ACCEPT = "application/octet-stream"

# Short by design. This is a read the Fleet view waits on, not a flash: an
# unreachable address must fail while someone is still looking at the screen.
FETCH_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=15.0, pool=5.0)

# --------------------------------------------------------------------------
# The second transport: the DUT's own console.
#
# The HTTP transport above needs two things an admin has to have set -- a
# management address and API credentials -- and neither exists on a DUT somebody
# has just cabled up. That is precisely the moment this question is worth
# asking, so the probe asks the device to call its own API over loopback, where
# it answers WITHOUT credentials (verified by hand on AP6_420E, 2026-08-24: the
# operator's curl carried no -u and returned the table).
#
# One operation, two transports, one parser -- the shape `firmware_service`
# already uses for its api/gui upload paths. `parse_mesh_payload` below is
# shared: only the fetching differs, so the two can never disagree about what a
# root's signal means.
MESH_CONSOLE_COMMAND = (
    f"curl -k -s -w '\\n' -X GET"
    f" 'https://127.0.0.1:{firmware_service.DEFAULT_MGMT_PORT}{MESH_PATH}'"
    f" -H 'accept: {ACCEPT}'"
)

# `-s` is not cosmetic: without it curl draws a progress meter whenever stdout
# is not a tty, and on a captured console that lands in the middle of the body.
#
# `-w '\n'` terminates the body, which this API does not: its reply carries no
# trailing newline, which is why the operator's own capture shows the next shell
# prompt glued to the closing brace. That once cost the whole reply --
# `capture_command` dropped any line holding its sentinel, and an unterminated
# body shares that line. The root cause is fixed in `SerialWorker`, which now
# splits the marker off instead, so this flag is no longer what makes the parse
# work. It stays because the DEVICE's output should be well formed on the wire:
# the session log and the console buffer record the raw line, and a body fused
# to a shell marker is worth less to whoever reads that log later.
#
# Both were measured on AP6840E-PD1005VMG3KJH9C, 2026-08-25: curl exited 0 with
# HTTP 200 and the body intact in a file, while the console capture came back
# empty and the probe reported "could not tell" about a healthy device.

# Longer than the 6s default. This is a TLS handshake plus a JSON build on the
# DUT's own small CPU, and a probe that times out on a healthy device would be
# read as "this DUT has no mesh".
CONSOLE_TIMEOUT_SEC = 12.0


class MeshError(RuntimeError):
    """The mesh table could not be read. The message is shown to the operator."""


class MeshNotConfigured(MeshError):
    """Refused before the DUT was contacted, because this side is not set up.

    Separate from the rest so the API can answer 400 rather than 502: nothing is
    wrong with the DUT, and the operator's next step is a settings page, not the
    bench.
    """


class MeshAuthError(MeshError):
    """The DUT rejected the credentials.

    Raised rather than retried, for the reason `FirmwareAuthError` gives: a
    refusal means the device is not on the expected credentials, and that is a
    finding to report rather than something to work around.
    """


def _as_int(value: object) -> int | None:
    """An int, or None. Never raises -- a missing or junk field is not an error.

    `bool` is excluded on purpose: it is an `int` subclass in Python, and a
    `true` where a hop count belongs would otherwise silently become 1.
    """
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _as_str(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _role(mesh_type: object) -> str | None:
    """`mesh_type` as this repo's vocabulary, or None for anything unrecognised.

    Deliberately not a passthrough: `backhaul.role` elsewhere in the fleet is
    lowercase "root"/"node", and a card comparing against the DUT's "Root" would
    silently never match. Unknown values become None rather than being guessed
    at, and the raw string is kept on the member so nothing is lost.
    """
    text = (mesh_type or "").casefold().strip() if isinstance(mesh_type, str) else ""
    return text if text in ("root", "node") else None


def _member(entry: dict) -> dict:
    role = _role(entry.get("mesh_type"))
    rssi = _as_int(entry.get("signal"))
    # The root reports `signal: 0`. That is the field being inapplicable -- a
    # root has no parent to hear -- and not a measurement of zero. Rendered as a
    # number it reads as an extraordinarily strong link, the exact opposite of
    # what it means, so it becomes "no reading" here rather than in each caller.
    if rssi == 0:
        rssi = None
    mac = _as_str(entry.get("mac_address"))
    return {
        "mac": mac.upper() if mac else None,
        # The DUT's own label for the member ("0", "1"), kept as the string it
        # sends, alongside the number it also sends. Publishing one derived from
        # the other would hide it when they stop agreeing -- and they do:
        # measured 2026-08-28, the node's own reply carries `node: "0"` with
        # `node_number: 1` for itself and `node: "1"` with `node_number: 1` for
        # the root. Both members, one `node_number`. Asked the root minutes
        # later, the same mesh answered 0 and 1 with `node` and `node_number`
        # matching throughout. So `node_number` tracks neither `node` nor the
        # device, and nothing here should be built on it.
        "node": _as_str(entry.get("node")),
        "node_number": _as_int(entry.get("node_number")),
        # **Relative to the DUT that was asked, not to the mesh.** The device
        # answering always reports itself as `node: "0"`, `hop: 0`, and counts
        # outward from there. Measured from both ends of the same two-device
        # mesh on 2026-08-28: asked the root, the root is hop 0 and the node is
        # hop 1; asked the node, those are exactly reversed.
        #
        # Kept verbatim rather than re-based onto the root, which would mean
        # inventing a topology out of one device's account of it. Callers that
        # show a hop count therefore have to say which DUT was asked -- the
        # mesh table does, in the line above it.
        "hop": _as_int(entry.get("hop")),
        "role": role,
        # What the device actually said, for a value `_role` did not recognise.
        "mesh_type": _as_str(entry.get("mesh_type")),
        "ip": _as_str(entry.get("ip_address")),
        "rssi": rssi,
        # The same wording the backhaul card uses for an RSSI, from the same
        # helper, so two views of one link cannot disagree about "near".
        "rssi_band": signal_band(rssi),
    }


def parse_mesh_payload(payload: object) -> list[dict]:
    """Members from one `/ap/info/wireless/mesh` body, in the order sent.

    Not re-sorted. The order is the device's own and puts the root first here;
    imposing one would be this module inventing a fact about the mesh.
    """
    if not isinstance(payload, dict):
        raise MeshError("The DUT's mesh endpoint did not answer with a JSON object.")
    code = _as_int(payload.get("error_code"))
    if code is not None and code != 0:
        message = _as_str(payload.get("error_msg"))
        raise MeshError(
            f"The DUT refused the mesh query: {message or f'error_code {code}'}"
        )
    data = payload.get("data")
    entries = data.get("mesh_info_list") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        # Distinct from an empty list, which is a legal answer meaning "this
        # device is in no mesh". This one means the reply was not the shape the
        # endpoint documents, and reporting it as an empty mesh would be a
        # confident wrong answer.
        raise MeshError("The DUT's mesh reply carried no mesh_info_list.")
    return [_member(entry) for entry in entries if isinstance(entry, dict)]


def extract_json_object(text: str) -> str | None:
    """The first complete ``{...}`` in console output, or None.

    Needed because the DUT does not terminate the body with a newline -- its
    next shell prompt follows on the same line::

        ...,"error_code":0,"error_msg":""}AP6_420E# pwd

    So "take the line" and "take everything up to the last brace" both swallow
    the prompt, and any command the operator typed after it. Scanning for the
    brace that matches the first one stops exactly where the object ends.

    Braces inside strings are skipped rather than counted: this payload carries
    SSIDs and error messages, and a single ``{`` in an SSID would otherwise cut
    the object short and produce a parse error on a perfectly good reply.
    """
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return None  # truncated: the capture timed out mid-body


def probe_mesh_over_console(worker, timeout: float = CONSOLE_TIMEOUT_SEC) -> dict:
    """Ask a DUT about its own mesh, over the console it is already attached to.

    Answers three states, and keeping them apart is the whole value:

      * ``mesh=True``  -- the device listed members.
      * ``mesh=False`` -- the device answered properly with an EMPTY list. That
        is a real "this DUT is in no mesh", and the only branch entitled to say
        so.
      * ``mesh=None``  -- we learned nothing. No JSON came back, or the device
        reported an error. Both are "could not tell", not "no mesh", and
        `detail` carries what it actually said.

    The empty-list branch is now evidence, not a guess: AP6840E-PD1005VMG3KJH9C
    answers `{"mesh_info_list":[],"total_size":0,"error_code":0}` when it is in
    no mesh (measured 2026-08-25). A device with nothing to report uses a normal
    reply and an empty list, NOT an error code -- so an error_code still means
    something went wrong rather than "no mesh", and still reports "could not
    tell". That remains the conservative branch, and it is the one that kept
    this honest when the console transport was silently losing the body.

    Never raises for what the DUT said; only for the console being unusable,
    which is a different problem and the caller's to report.
    """
    try:
        raw = worker.capture_command(MESH_CONSOLE_COMMAND, timeout=timeout)
    except RuntimeError as exc:
        raise MeshError(f"Could not run the mesh probe on the console: {exc}") from exc

    body = extract_json_object(raw)
    if body is None:
        # No JSON at all: curl may be absent, the API may not be listening, or
        # the reply was still draining when the capture window closed.
        return {
            "probed": True, "mesh": None, "members": [],
            "detail": "The console returned no JSON; the mesh API may not be running here.",
        }
    try:
        payload = json.loads(body)
    except ValueError:
        return {
            "probed": True, "mesh": None, "members": [],
            "detail": "The console returned something that is not valid JSON.",
        }
    try:
        members = parse_mesh_payload(payload)
    except MeshError as exc:
        return {"probed": True, "mesh": None, "members": [], "detail": str(exc)}
    return {
        "probed": True,
        "mesh": bool(members),
        "members": members,
        "detail": "" if members else "The device answered with an empty mesh list.",
    }


def fetch_mesh(mgmt_url: str, client_factory=None) -> dict:
    """Read one DUT's mesh table. Raises `MeshError` with what to tell the user."""
    origin = firmware_service.normalise_mgmt_url(mgmt_url, firmware_service.TRANSPORT_API)
    if not origin:
        raise MeshNotConfigured(
            "No management address configured for this DUT."
            " Set it in the Firmware section before reading the mesh."
        )
    if not firmware_service.has_credentials():
        raise MeshNotConfigured(
            "No DUT API credentials configured. Set them in the Firmware section"
            " before reading the mesh."
        )
    user, password = firmware_service.get_credentials()
    url = urljoin(origin + "/", MESH_PATH.lstrip("/"))

    # verify=False for the reason firmware_service gives: the DUT serves this
    # API with a self-signed certificate, which is why scripts/sysMon.sh uses
    # `curl -k` against the same port.
    build = client_factory or (lambda: httpx.Client(verify=False, timeout=FETCH_TIMEOUT))
    try:
        with build() as client:
            # Digest, not Basic: verified on AP6_840E on both ports, Basic is
            # simply rejected. No `_prime_digest` dance is needed here -- unlike
            # `common.cgi`, the /ap/* family does challenge, so httpx's
            # challenge-driven DigestAuth authenticates on the retry.
            response = client.get(
                url,
                auth=httpx.DigestAuth(user, password),
                headers={"Accept": ACCEPT},
            )
    except httpx.HTTPError as exc:
        raise MeshError(f"Could not reach the DUT's management API at {origin}: {exc}") from exc

    if response.status_code in (401, 403):
        raise MeshAuthError(
            f"The DUT rejected the credentials while reading the mesh"
            f" ({response.status_code})."
        )
    if response.status_code >= 400:
        raise MeshError(f"The DUT's mesh endpoint answered {response.status_code}.")
    try:
        payload = response.json()
    except ValueError as exc:
        raise MeshError("The DUT's mesh endpoint did not answer with JSON.") from exc
    return {"mgmt_url": origin, "members": parse_mesh_payload(payload)}
