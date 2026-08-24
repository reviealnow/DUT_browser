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
        # sends, alongside the number it also sends. They agree on this bench;
        # publishing one derived from the other would hide it if they stopped.
        "node": _as_str(entry.get("node")),
        "node_number": _as_int(entry.get("node_number")),
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
