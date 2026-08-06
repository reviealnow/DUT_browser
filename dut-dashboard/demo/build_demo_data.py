#!/usr/bin/env python3
"""Rebuild the demo kit's baked-in data from a real Download-DUT-Log bundle.

The demo pages are hand-written, self-contained HTML: one file per screen, no
server, no CDN, openable by double-click and small enough to email. Their data
is real — captured from an actual DUT session — but every identifier is
replaced before it is committed, because a neighbour scan sweeps up the SSIDs
and BSSIDs of everyone within radio range, not just ours.

This script does the replacing and injects the result. It rewrites only the
``<script id="demo-data" type="application/json">`` block of each page, so the
markup stays hand-editable and regenerating data never clobbers a layout tweak.

Usage:
    python3 build_demo_data.py --bundle /path/to/dut-session-<ts> [--page overview.html]

Anonymisation rules, all deterministic (a given real value always maps to the
same fake one, so the demo reads consistently across regenerations) and all
distribution-preserving (counts per channel, per band and per timestamp are
untouched — only labels change):

* SSID      -> a name from a fixed pool; the DUT's own VAPs and neighbouring
               networks draw from separate pools so the screens still read
               correctly
* MAC/BSSID -> ``02:``-prefixed. 02 is the locally-administered bit: these are
               visibly not real vendor addresses, which is the honest signal
* IP        -> 198.51.100.0/24 (RFC 5737 TEST-NET-2, reserved for documentation)
* vendor    -> a fixed pool, mapped consistently with the MAC it belongs to

Model/DUT names (AP6_840E and friends) are deliberately NOT anonymised: they
are the product being demonstrated.
"""

from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent

DATA_BLOCK_RE = re.compile(
    r'(<script id="demo-data" type="application/json">)(.*?)(</script>)', re.S
)

OWN_SSIDS = [
    "DemoAP-Corp", "DemoAP-Guest", "DemoAP-IoT", "DemoAP-Lab",
    "DemoAP-Voice", "DemoAP-Legacy", "DemoAP-Test", "DemoAP-Mgmt",
]
NEIGHBOUR_SSIDS = [
    "Cafe-Guest", "OfficeNet", "HomeWiFi", "FiberLink", "SkyBroadband",
    "MetroHotspot", "TowerNet", "GreenLeaf", "BlueHarbour", "CityFibre",
    "StudioNet", "RiverView", "NorthGate", "SunsetWiFi", "Parkside",
    "Lighthouse", "OrchardNet", "Redwood", "StoneBridge", "HarbourPoint",
]
VENDORS = [
    "Acme Devices", "Northwind Systems", "Contoso Networks", "Fabrikam Inc.",
    "Private (randomized)", "Litware Hardware",
]


def _bucket(value: str, size: int, salt: str) -> int:
    digest = hashlib.sha256(f"{salt}:{value}".encode()).digest()
    return int.from_bytes(digest[:4], "big") % size


class Anonymiser:
    """Deterministic, distribution-preserving identifier replacement."""

    def __init__(self) -> None:
        self._ssid: dict[str, str] = {}
        self._mac: dict[str, str] = {}
        self._ip: dict[str, str] = {}
        self._vendor: dict[str, str] = {}

    def ssid(self, value: str | None, *, own: bool = False) -> str | None:
        if not value:
            return value
        if value not in self._ssid:
            pool = OWN_SSIDS if own else NEIGHBOUR_SSIDS
            index = _bucket(value, len(pool), "ssid")
            name = pool[index]
            # Keep names unique so two real networks never collapse into one.
            suffix = 2
            while name in self._ssid.values():
                name = f"{pool[index]}-{suffix}"
                suffix += 1
            self._ssid[value] = name
        return self._ssid[value]

    def mac(self, value: str | None) -> str | None:
        if not value:
            return value
        key = value.lower()
        if key not in self._mac:
            digest = hashlib.sha256(f"mac:{key}".encode()).digest()[:5]
            tail = ":".join(f"{b:02x}" for b in digest)
            self._mac[key] = f"02:{tail}"
        out = self._mac[key]
        return out.upper() if value == value.upper() else out

    def ip(self, value: str | None) -> str | None:
        if not value:
            return value
        if value not in self._ip:
            self._ip[value] = f"198.51.100.{_bucket(value, 254, 'ip') + 1}"
        return self._ip[value]

    def vendor(self, value: str | None) -> str | None:
        if not value:
            return value
        if value not in self._vendor:
            self._vendor[value] = VENDORS[_bucket(value, len(VENDORS), "vendor")]
        return self._vendor[value]


def _downsample(rows: list, limit: int = 120) -> list:
    step = max(1, len(rows) // limit)
    return rows[::step][:limit]


def _labels(items: list[str], count: int = 4) -> list[dict]:
    if not items:
        return []
    spots = sorted({int(i * (len(items) - 1) / (count - 1)) for i in range(count)})
    return [{"i": i, "text": items[i][5:16]} for i in spots]


def build(bundle: Path, anon: Anonymiser, survey_bundle: Path | None = None) -> dict:
    """Assemble the Overview page's data from real session bundles.

    ``bundle`` supplies the time-series (CPU, associated clients).
    ``survey_bundle`` supplies the neighbour scan, and exists because no single
    capture on this bench has both halves: the long run that carries a rich
    time-series predates the context fix, and the run with a real
    2438-neighbour survey had no station associated for its ten cycles. Rather
    than stitch together a bundle that never existed, both sources are named
    and printed in the page's own provenance line.
    """
    cpu_csv = next(bundle.glob("*_cpu_usage.csv"), None)
    if cpu_csv is None:
        raise SystemExit(f"no *_cpu_usage.csv in {bundle} — is this a Download bundle?")
    rows = _downsample(list(csv.DictReader(cpu_csv.open())))
    cpu = {
        "c0": [round(float(r["CPU0_UsagePct"]), 1) for r in rows],
        "c1": [round(float(r["CPU1_UsagePct"]), 1) for r in rows],
        "labels": _labels([r["Timestamp"] for r in rows]),
    }

    clients_csv = next(bundle.glob("*_wifi_clients.csv"), None)
    if clients_csv is not None:
        client_rows = list(csv.DictReader(clients_csv.open()))
        per_ts = collections.Counter(r["ts"] for r in client_rows)
        stamps = _downsample(sorted(per_ts))
        clients = {"values": [per_ts[t] for t in stamps], "labels": _labels(stamps)}
        roamer = _dual_radio_client(client_rows, anon)
    else:                                   # a session with no associated clients
        clients = {"values": [0] * len(cpu["c0"]), "labels": cpu["labels"]}
        roamer = None

    survey = _newest_json((survey_bundle or bundle) / "context" / "site-survey")
    bands = _bands(survey) if survey else []

    latest_clients = clients["values"][-1] if clients["values"] else 0
    kpis = [
        {"label": "DUT status", "value": "Streaming", "unit": "",
         "foot": f"serial open · {len(rows)} snapshots plotted"},
        {"label": "Latest CPU", "value": f"{cpu['c0'][-1]:g}", "unit": "%",
         "foot": f"2 cores · peak {max(max(cpu['c0']), max(cpu['c1'])):g}%"},
        {"label": "Wi-Fi clients", "value": str(latest_clients), "unit": "",
         "foot": f"peak {max(clients['values']) if clients['values'] else 0} over the session"},
        {"label": "Neighbours seen", "value": str(sum(
            c["count"] for b in bands for c in b["channels"])), "unit": "",
         "foot": f"{len(bands)} band(s) scanned"},
    ]

    sources = bundle.name if survey_bundle is None else f"{bundle.name} + {survey_bundle.name}"
    return {
        "generatedFrom": sources,
        "scannedAt": (survey or {}).get("captured_at", "")[:16].replace("T", " "),
        "cpu": cpu,
        "clients": clients,
        "bands": bands,
        "roamer": roamer,
        "kpis": kpis,
    }


def _newest_json(kind_dir: Path) -> dict | None:
    files = sorted(kind_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return json.loads(files[0].read_text()) if files else None


def _bands(survey: dict) -> list[dict]:
    """Bar heights are RAW neighbour counts, matching SiteSurveyCard.tsx.

    The recommendation's `occupancy` map is a signal-weighted interference
    score — a different measurement — so it travels alongside as a number, never
    as a bar height.
    """
    out = []
    for rec in survey.get("recommendations", []):
        band = rec["band"]
        counts = collections.Counter(
            int(n["channel"]) for n in survey.get("neighbors", [])
            if n.get("band") == band and n.get("channel") is not None
        )
        occupancy = {int(k): v for k, v in (rec.get("occupancy") or {}).items()}
        channels = sorted(set(counts) | set(occupancy))
        out.append({
            "band": band,
            "current": rec.get("current_channel"),
            "recommended": rec.get("recommended_channel"),
            "scoreCurrent": round(occupancy.get(rec.get("current_channel"), 0)),
            "scoreBest": round(occupancy.get(rec.get("recommended_channel"), 0)),
            "channels": [{"channel": c, "count": counts.get(c, 0)} for c in channels],
        })
    return out


def _dual_radio_client(rows: list[dict], anon: Anonymiser) -> dict | None:
    """The most interesting thing in a long capture: one device, both radios.

    Worth surfacing because a point-in-time client table can never show it.
    """
    radios = collections.defaultdict(set)
    for r in rows:
        if r.get("mac_address") and r.get("radio"):
            radios[r["mac_address"]].add(r["radio"])
    for mac, seen in radios.items():
        if len(seen) > 1:
            mine = [r for r in rows if r["mac_address"] == mac]
            return {
                "mac": anon.mac(mac),
                "radios": sorted(seen),
                # A client's SSID is one the DUT itself broadcasts, not a neighbour's.
                "ssid": anon.ssid(mine[-1].get("ssid_name"), own=True),
                "lastSeen": mine[-1]["ts"],
            }
    return None


def inject(page: Path, payload: dict) -> None:
    html = page.read_text(encoding="utf-8")
    if not DATA_BLOCK_RE.search(html):
        raise SystemExit(f"{page.name} has no <script id='demo-data'> block")
    blob = json.dumps(payload, separators=(",", ":"))
    page.write_text(
        DATA_BLOCK_RE.sub(lambda m: m.group(1) + blob + m.group(3), html, count=1),
        encoding="utf-8",
    )
    print(f"{page.name}: injected {len(blob):,} bytes of data")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bundle", required=True, type=Path,
                    help="an extracted dut-session-<ts> directory")
    ap.add_argument("--survey-bundle", type=Path, default=None,
                    help="take the neighbour scan from a second bundle when --bundle "
                         "has no usable one (both sources are recorded on the page)")
    ap.add_argument("--page", default="overview.html",
                    help="demo page to rewrite (default: overview.html)")
    args = ap.parse_args()

    anon = Anonymiser()
    payload = build(args.bundle, anon, args.survey_bundle)
    fixed = json.loads((HERE / "demo-fixtures.json").read_text(encoding="utf-8"))
    payload.update(fixed)                   # fleet / KPI / crash copy, see README
    inject(HERE / args.page, payload)
    print(f"anonymised: {len(anon._ssid)} SSIDs, {len(anon._mac)} MACs, {len(anon._ip)} IPs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
