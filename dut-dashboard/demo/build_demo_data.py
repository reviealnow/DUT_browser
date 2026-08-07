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

Anonymisation rules. Aliases are assigned over the **sorted** set of real
values, so the result does not depend on the order rows happen to be walked,
and every assignment probes forward past a taken slot, so the mapping is
injective — distinct networks never collapse into one name. Counts per channel,
per band and per timestamp are untouched; only labels change.

* SSID      -> a generated name; the DUT's own VAPs and neighbouring networks
               draw from separate namespaces so the screens still read correctly
* MAC/BSSID -> ``02:``-prefixed. 02 is the locally-administered bit: these are
               visibly not real vendor addresses, which is the honest signal
* IP        -> RFC 5737 documentation ranges (192.0.2/198.51.100/203.0.113)
* vendor    -> a fixed pool, mapped consistently with the MAC it belongs to

This is pseudonymisation, not encryption. The page cannot be reversed, but a
deterministic unsalted hash over a small candidate space is guessable by anyone
already holding the original capture. The goal is that identifiers are never
published — not that someone with the source bundle is defeated.

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

# The DUT under test broadcasts a lot of VAPs (29 on the reference AP), so its
# own namespace is generated too — and kept visibly distinct from the neighbour
# one, because "ours" versus "everyone else's" is a distinction the screens draw.
_OWN_ROLES = ["Corp", "Guest", "IoT", "Lab", "Voice", "Legacy", "Test", "Mgmt",
              "Sensor", "Kiosk", "Field", "Depot"]
OWN_SSIDS = [f"DemoAP-{role}" for role in _OWN_ROLES] + [
    f"DemoAP-{role}-{n}" for n in (2, 3, 4) for role in _OWN_ROLES
]
# A real scan in an office block turns up a couple of hundred distinct SSIDs, so
# the neighbour namespace is generated rather than listed: word x form gives
# enough room that names stay plausible instead of degrading into Name-2, -3, -4.
_WORDS = [
    "Cafe", "Office", "Home", "Fiber", "Sky", "Metro", "Tower", "Green",
    "Blue", "City", "Studio", "River", "North", "Sunset", "Park", "Light",
    "Orchard", "Redwood", "Stone", "Harbour", "Willow", "Summit", "Amber",
    "Cedar", "Delta", "Falcon", "Granite", "Ivory", "Juniper", "Quartz",
]
_FORMS = ["{w}Net", "{w}-Guest", "{w}-WiFi", "{w}-{n:02d}", "{w}_5G",
          "{w}-Fibre", "{w}Link", "{w}-Public", "{w}-Home-{n}"]
NEIGHBOUR_SSIDS = [
    form.format(w=word, n=(i * 7 % 90) + 10)
    for i, word in enumerate(_WORDS) for form in _FORMS
]
VENDORS = [
    "Acme Devices", "Northwind Systems", "Contoso Networks", "Fabrikam Inc.",
    "Private (randomized)", "Litware Hardware",
]


def _bucket(value: str, size: int, salt: str) -> int:
    digest = hashlib.sha256(f"{salt}:{value}".encode()).digest()
    return int.from_bytes(digest[:4], "big") % size


class Anonymiser:
    """Order-independent, collision-free, distribution-preserving replacement.

    Two properties matter and neither is free:

    *Deterministic regardless of encounter order.* Aliases are assigned in one
    pass over the **sorted** set of real values, not as they are met, so the
    same bundle always yields the same page no matter which screen is built
    first or in what order rows happen to be walked.

    *Injective.* Two distinct real values never share an alias. Collapsing two
    networks into one name would understate how crowded the air is, which is
    the measurement these pages exist to show, so every assignment probes
    forward until it finds a free slot instead of accepting a hash collision.

    This is pseudonymisation, not encryption: the mapping is not reversible
    from the page, but a deterministic unsalted hash over a small candidate
    space can be guessed by anyone who already holds the original capture. The
    goal is that identifiers are never published, not that an attacker with the
    source bundle is defeated.
    """

    def __init__(self) -> None:
        self._maps: dict[str, dict[str, str]] = {
            "ssid": {}, "mac": {}, "ip": {}, "vendor": {},
        }

    # -- assignment ------------------------------------------------------
    def _assign(self, kind: str, values, alias, *, space: int) -> None:
        """Give every value in ``values`` a distinct alias, sorted-order stable."""
        table = self._maps[kind]
        taken = set(table.values())
        for value in sorted(v for v in set(values) if v):
            if value in table:
                continue
            start = _bucket(value, space, kind)
            for step in range(space):
                candidate = alias((start + step) % space)
                if candidate not in taken:
                    break
            else:
                raise SystemExit(
                    f"{kind}: {len(taken)} aliases exhausted a space of {space}; "
                    f"widen the pool in build_demo_data.py"
                )
            table[value] = candidate
            taken.add(candidate)

    def prepare(self, *, ssids=(), own_ssids=(), macs=(), ips=(), vendors=()) -> None:
        # Own VAPs first: a client or neighbour row naming one of our own SSIDs
        # must resolve to the DemoAP-* alias, not to a neighbour name.
        self._assign("ssid", own_ssids, lambda i: OWN_SSIDS[i], space=len(OWN_SSIDS))
        self._assign("ssid", ssids, lambda i: NEIGHBOUR_SSIDS[i], space=len(NEIGHBOUR_SSIDS))
        self._assign("mac", (m.lower() for m in macs), _mac_alias, space=1 << 40)
        self._assign("ip", ips, _ip_alias, space=3 * 254)
        self._assign("vendor", vendors, lambda i: VENDORS[i], space=len(VENDORS))

    # -- lookup ----------------------------------------------------------
    def ssid(self, value: str | None, *, own: bool = False) -> str | None:
        if not value:
            return value                     # hidden SSID: nothing to disclose
        if value not in self._maps["ssid"]:
            self._assign("ssid", [value],
                         (lambda i: OWN_SSIDS[i]) if own else (lambda i: NEIGHBOUR_SSIDS[i]),
                         space=len(OWN_SSIDS) if own else len(NEIGHBOUR_SSIDS))
        return self._maps["ssid"][value]

    def mac(self, value: str | None) -> str | None:
        if not value:
            return value
        key = value.lower()
        if key not in self._maps["mac"]:
            self._assign("mac", [key], _mac_alias, space=1 << 40)
        alias = self._maps["mac"][key]
        return alias.upper() if value == value.upper() else alias

    def ip(self, value: str | None) -> str | None:
        if not value:
            return value
        if value not in self._maps["ip"]:
            self._assign("ip", [value], _ip_alias, space=3 * 254)
        return self._maps["ip"][value]

    def vendor(self, value: str | None) -> str | None:
        if not value:
            return value
        if value not in self._maps["vendor"]:
            self._assign("vendor", [value], lambda i: VENDORS[i], space=len(VENDORS))
        return self._maps["vendor"][value]

    def counts(self) -> str:
        return ", ".join(f"{len(v)} {k}s" for k, v in self._maps.items() if v)


def _mac_alias(index: int) -> str:
    """A full 48-bit address whose first octet sets the locally-administered bit.

    ``02:`` says "not a vendor OUI" at a glance, which is the honest signal; the
    remaining five octets carry the index. Emitting fewer than six octets — an
    earlier bug here — produces something that is not a MAC at all.
    """
    octets = [(index >> shift) & 0xFF for shift in (32, 24, 16, 8, 0)]
    return "02:" + ":".join(f"{o:02x}" for o in octets)


def _ip_alias(index: int) -> str:
    """RFC 5737 documentation ranges — three /24s, so 762 distinct addresses."""
    block = ("192.0.2", "198.51.100", "203.0.113")[index // 254]
    return f"{block}.{index % 254 + 1}"


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

    # Every KPI here is derived from the bundle. A "DUT status" tile would not
    # be — connection state is live UI, absent from a capture — so it lives in
    # demo-fixtures.json with the rest of the synthetic copy rather than being
    # hard-coded here where the provenance line would not account for it.
    latest_clients = clients["values"][-1] if clients["values"] else 0
    kpis = [
        {"label": "Latest CPU", "value": f"{cpu['c0'][-1]:g}", "unit": "%",
         "foot": f"2 cores · peak {max(max(cpu['c0']), max(cpu['c1'])):g}%"},
        {"label": "Wi-Fi clients", "value": str(latest_clients), "unit": "",
         "foot": f"peak {max(clients['values']) if clients['values'] else 0} over the session"},
        {"label": "Neighbours seen", "value": str(sum(
            c["count"] for b in bands for c in b["channels"])), "unit": "",
         "foot": f"{len(bands)} band(s) scanned"},
        {"label": "Snapshots plotted", "value": str(len(rows)), "unit": "",
         "foot": "downsampled from the full session"},
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


def build_survey(bundle: Path, anon: Anonymiser, survey_bundle: Path | None = None) -> dict:
    """Site Survey page: the recommendation, the per-band charts and the table.

    Every neighbour row ships, because the point of this screen is the scale of
    a real scan — a few hundred networks, most of them other people's. That is
    also why the anonymiser runs over the whole set before anything is emitted.
    """
    source = survey_bundle or bundle
    survey = _newest_json(source / "context" / "site-survey")
    if survey is None:
        raise SystemExit(f"no context/site-survey/*.json under {source}")

    neighbours = survey.get("neighbors", [])
    own = {v.get("ssid") for v in survey.get("vaps", []) if v.get("ssid")}
    anon.prepare(
        own_ssids=own,
        ssids=(n.get("ssid") for n in neighbours if n.get("ssid") not in own),
        macs=(n.get("bssid") for n in neighbours if n.get("bssid")),
    )

    securities = sorted({n.get("security") or "—" for n in neighbours})
    generations = sorted({n.get("generation") or "—" for n in neighbours})
    band_names = sorted({n.get("band") for n in neighbours if n.get("band")})
    ssid_pool: list[str] = []
    ssid_index: dict[str, int] = {}

    rows = []
    for n in neighbours:
        raw = n.get("ssid")
        name = anon.ssid(raw, own=raw in own) if raw else "(hidden)"
        if name not in ssid_index:
            ssid_index[name] = len(ssid_pool)
            ssid_pool.append(name)
        rows.append([
            band_names.index(n["band"]) if n.get("band") in band_names else -1,
            n.get("channel"),
            ssid_index[name],
            (anon.mac(n.get("bssid")) or "").replace(":", ""),
            int(n["signal_dbm"]) if n.get("signal_dbm") is not None else None,
            securities.index(n.get("security") or "—"),
            generations.index(n.get("generation") or "—"),
        ])

    return {
        "generatedFrom": source.name,
        "scannedAt": survey.get("captured_at", "")[:16].replace("T", " "),
        "bands": _bands(survey),
        "bandNames": band_names,
        "ssids": ssid_pool,
        "securities": securities,
        "generations": generations,
        "rows": rows,
        "ownRows": sum(1 for n in neighbours if n.get("ssid") in own),
        "ownSsidsInTable": len({n["ssid"] for n in neighbours if n.get("ssid") in own}),
        "distinctBssids": len({r[3] for r in rows}),
    }


def build_clients(bundle: Path, anon: Anonymiser, survey_bundle: Path | None = None) -> dict:
    """Wi-Fi Clients page: the point-in-time table, plus 40 hours behind it.

    An offline bundle and a live scan do not carry the same fields. The table
    the product draws is fed by ``wlanconfig`` + ``apstats``; what a downloaded
    session log carries is the DUT's own REST hooks. Mode, NSS, PER and the
    instantaneous rates are apstats-only, so they are emitted as ``None`` and
    the page renders them as "not in this capture" rather than inventing them —
    which is itself worth showing, because it is true of the product.
    """
    clients_csv = next(bundle.glob("*_wifi_clients.csv"), None)
    if clients_csv is None:
        raise SystemExit(f"no *_wifi_clients.csv in {bundle} — run tools/wifi_timeseries.py first")
    rows = list(csv.DictReader(clients_csv.open()))
    if not rows:
        raise SystemExit(f"{clients_csv.name} has no client rows")

    anon.prepare(
        own_ssids={r["ssid_name"] for r in rows if r.get("ssid_name")},
        macs={r["mac_address"] for r in rows if r.get("mac_address")},
        ips={r["ip_address"] for r in rows if r.get("ip_address")},
        vendors={r["vendor"] for r in rows if r.get("vendor")},
    )

    def num(value, cast=int):
        try:
            return cast(value)
        except (TypeError, ValueError):
            return None

    last_ts = rows[-1]["ts"]
    history: dict[str, list] = collections.defaultdict(list)
    for r in rows:
        if r.get("mac_address"):
            history[r["mac_address"]].append((r["ts"], num(r.get("rssi_dbm"))))

    clients = []
    for r in rows:
        if r["ts"] != last_ts:
            continue
        mac = r["mac_address"]
        series = [v for _, v in history.get(mac, []) if v is not None]
        clients.append({
            "mac": anon.mac(mac),
            "ssid": anon.ssid(r.get("ssid_name"), own=True),
            "band": r.get("radio") or "—",
            "chan": num(r.get("chan")),
            "rssi": num(r.get("rssi_dbm")),
            "signalPct": num(r.get("signal_ratio_pct")),
            "snr": num(r.get("snr_db")),
            "txBytes": num(r.get("tx_bytes")),
            "rxBytes": num(r.get("rx_bytes")),
            "assocSecs": num(r.get("connected_secs")),
            "idleSecs": num(r.get("idle_secs")),
            "widthMhz": num(r.get("chan_width_mhz")),
            "ip": anon.ip(r.get("ip_address")),
            "vendor": anon.vendor(r.get("vendor")),
            "vlan": num(r.get("vlan_id")),
            # apstats-only, absent from a downloaded bundle. Never guessed.
            "mode": None, "nss": None, "per": None, "txRate": None, "rxRate": None,
            "rssiSeries": _downsample(series, 60),
            "samples": len(series),
        })

    # The finding a point-in-time table can never show: one device associated on
    # two radios at once. Kept because it is the reason a time axis exists.
    roamer = _dual_radio_client(rows, anon)
    return {
        "generatedFrom": bundle.name,
        "capturedAt": last_ts,
        "spanFrom": rows[0]["ts"],
        "clients": clients,
        "totalRows": len(rows),
        "roamer": roamer,
    }


def build_downloads(bundle: Path, anon: Anonymiser,
                    survey_bundle: Path | None = None) -> dict:
    """Downloads page: what a real Download DUT Log actually produced.

    This is the one screen whose subject IS the bundle, so it is listed from a
    real one — names, sizes and grouping exactly as they came off disk. The
    only judgement is which of the four cards a file belongs to, and that
    follows the split the API reports: the session log, the analyzer's
    published outputs, the persisted surveys, and the connect-time context.

    One plot is embedded as a data URI so the inline preview is real rather
    than mimed; the rest are listed, because a page that inlined a megabyte of
    PNGs would stop being something you can email.
    """
    import base64

    log = next((p for p in bundle.iterdir() if p.suffix == ".log"), None)
    if log is None:
        raise SystemExit(f"no session log in {bundle}")

    def entry(path: Path, **extra) -> dict:
        stat = path.stat()
        return {"name": path.name, "size": stat.st_size,
                "modified": _stamp(stat.st_mtime), **extra}

    outputs, surveys, context = [], [], []
    for path in sorted(bundle.iterdir()):
        if path.is_file() and path != log and path.suffix in (".csv", ".png", ".txt"):
            outputs.append(entry(path, plot=path.suffix == ".png"))

    context_dir = bundle / "context"
    report = context_dir / "capture-report.txt"
    for kind_dir in sorted(p for p in context_dir.iterdir() if p.is_dir()):
        for path in sorted(kind_dir.iterdir()):
            (surveys if kind_dir.name == "site-survey" else context).append(
                entry(path, kind=kind_dir.name))

    # The peek shows the log's last lines, as the product's does. A serial log
    # is free text, so it cannot be aliased field by field: instead the tail is
    # refused outright if it carries anything identifying. A loud stop beats a
    # silent leak, and the operator can pick another bundle or widen the scrub.
    tail_lines = log.read_text(encoding="utf-8", errors="ignore").splitlines()[-14:]
    tail = "\n".join(tail_lines)
    refuse_if_identifying(tail, "the log tail")

    plots = sorted(bundle.glob("*.png"), key=lambda p: p.stat().st_size)
    preview = plots[0] if plots else None

    return {
        "generatedFrom": bundle.name,
        "sessionLog": entry(log),
        "outputs": outputs,
        "surveys": surveys,
        "context": context,
        "logTail": tail,
        "captureReport": (report.read_text(encoding="utf-8").strip().splitlines()
                          if report.is_file() else []),
        "previewName": preview.name if preview else None,
        "previewData": ("data:image/png;base64,"
                        + base64.b64encode(preview.read_bytes()).decode()) if preview else None,
    }


IDENTIFIER_PATTERNS = (
    (r"(?i)\bssid\b", "an SSID field"),
    (r"\b(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\b", "a MAC address"),
    (r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "an IP address"),
)


def refuse_if_identifying(text: str, what: str) -> str:
    """Free text cannot be aliased field by field, so it is refused instead.

    A serial log is prose plus whatever the DUT printed. There is no schema to
    walk, so the only safe rule is: if it carries an identifier, do not ship it.
    A loud stop beats a silent leak — the operator picks a cleaner excerpt.
    """
    for pattern, found in IDENTIFIER_PATTERNS:
        if re.search(pattern, text):
            raise SystemExit(
                f"{what} carries {found} and would ship as-is; choose a cleaner "
                f"excerpt or widen the scrub in build_demo_data.py"
            )
    return text


def build_console(bundle: Path, anon: Anonymiser,
                  survey_bundle: Path | None = None) -> dict:
    """Serial Console: real monitor output, and an honest note about the terminal.

    The Monitor half is what ConsolePanel shows — a scrolling text view — so it
    is real log lines, taken from the largest identifier-free run in the
    session. The Terminal half is xterm.js over a pty in the product, which a
    single HTML file cannot be; that half replays a recording and says so.
    """
    log = next((p for p in bundle.iterdir() if p.suffix == ".log"), None)
    if log is None:
        raise SystemExit(f"no session log in {bundle}")

    lines = log.read_text(encoding="utf-8", errors="ignore").splitlines()
    # The excerpt is chosen by the guard, not by hoping a fixed offset stays
    # clean: take runs of consecutive identifier-free lines. Among those, prefer
    # one containing a sysMon section header, so the Monitor view reads like the
    # monitoring it is rather than starting mid-command.
    runs, run = [], []
    for line in lines:
        if any(re.search(pattern, line) for pattern, _ in IDENTIFIER_PATTERNS):
            if run:
                runs.append(run)
            run = []
            continue
        run.append(line)
    if run:
        runs.append(run)
    if not runs:
        raise SystemExit(f"{log.name} has no identifier-free run to excerpt")

    def score(candidate: list[str]) -> tuple[int, int]:
        headed = any(line.startswith("=== ") for line in candidate)
        return (1 if headed else 0, len(candidate))

    best = max(runs, key=score)
    start = next((i for i, line in enumerate(best) if line.startswith("=== ")), 0)
    excerpt = refuse_if_identifying("\n".join(best[start:start + 80]), "the console excerpt")

    return {
        "generatedFrom": bundle.name,
        "logName": log.name,
        "lines": excerpt.splitlines(),
        "totalLines": len(lines),
    }


def build_cpu(bundle: Path, anon: Anonymiser,
              survey_bundle: Path | None = None) -> dict:
    """CPU / Memory: the three cards, from the analyzer's own CSVs.

    Nothing here needs anonymising — per-core busy percentages and
    /proc/meminfo counters identify nobody — so this screen is measured
    end to end. The memory card plots EffectiveAvailable_kB, which is what
    the analyzer already computes (MemAvailable minus SUnreclaim); taking it
    from the column rather than recomputing keeps one definition of the
    number.
    """
    cpu_csv = next(bundle.glob("*_cpu_usage.csv"), None)
    mem_csv = next(bundle.glob("*_memory.csv"), None)
    if cpu_csv is None or mem_csv is None:
        raise SystemExit(f"need both *_cpu_usage.csv and *_memory.csv in {bundle}")

    cpu_rows = _downsample(list(csv.DictReader(cpu_csv.open())))
    cores = sorted(c for c in cpu_rows[0] if c.startswith("CPU") and c.endswith("_UsagePct"))
    series = {c[3:-len("_UsagePct")]: [round(float(r[c]), 1) for r in cpu_rows] for c in cores}
    # useDutMonitor derives cpuBusyPct as 100 - mean(idle), which is the mean of
    # the per-core busy figures. The CSV only carries those already rounded to
    # one decimal, so averaging them differs from the product's single-rounding
    # path by under 0.05 — the same quantity, not a different definition. Do not
    # "correct" this to a max or a sum.
    busy = [round(sum(series[c][i] for c in series) / len(series), 1)
            for i in range(len(cpu_rows))]

    mem_rows = _downsample(list(csv.DictReader(mem_csv.open())))
    effective = [int(r["EffectiveAvailable_kB"]) for r in mem_rows]

    return {
        "generatedFrom": bundle.name,
        "cores": series,
        "busy": busy,
        "cpuLabels": _labels([r["Timestamp"] for r in cpu_rows]),
        "latestPerCore": {c: series[c][-1] for c in series},
        "latestBusy": busy[-1],
        "memory": {
            "effectiveKb": effective,
            "availableKb": [int(r["MemAvailable_kB"]) for r in mem_rows],
            "slabKb": [int(r["Slab_kB"]) for r in mem_rows],
            "labels": _labels([r["Timestamp"] for r in mem_rows]),
        },
        "spanFrom": cpu_rows[0]["Timestamp"],
        "spanTo": cpu_rows[-1]["Timestamp"],
        "samples": sum(1 for _ in csv.DictReader(cpu_csv.open())),
    }


def build_ssid(bundle: Path, anon: Anonymiser,
               survey_bundle: Path | None = None) -> dict:
    """SSID Capability: the DUT's own VAPs, reconciled against a host-side scan.

    The capture holds Source A only — hostapd config read over serial. Source B
    is a scan run on the host, which needs SURVEY_WIFI_IFACE set; without it the
    product shows every row as a miss and says "Source B unavailable". That is
    exactly the state of this bundle, so it is the state shown: inventing a
    Source B would mean inventing the reconciliation this card exists to do.
    """
    source = survey_bundle or bundle
    payload = _newest_json(source / "context" / "ssid-capability")
    if payload is None:
        raise SystemExit(f"no context/ssid-capability/*.json under {source}")

    ssids = payload.get("ssids", [])
    anon.prepare(own_ssids={s["ssid"] for s in ssids if s.get("ssid")},
                 macs={s["bssid"] for s in ssids if s.get("bssid")})

    rows = [{
        "iface": s.get("iface"),
        "ssid": anon.ssid(s.get("ssid"), own=True),
        "bssid": anon.mac(s.get("bssid")),
        "band": s.get("band"),
        "channel": s.get("channel"),
        "width": s.get("channel_width"),
        "security": s.get("security"),
        "pmf": s.get("pmf"),
        "generation": s.get("generation"),
        "dot11k": s.get("dot11k"),
        "dot11v": s.get("dot11v"),
        "dot11r": s.get("dot11r"),
        "akm": s.get("akm") or [],
        "pairwise": s.get("pairwise_cipher") or [],
        "freqMhz": s.get("freq_mhz"),
    } for s in sorted(ssids, key=lambda s: (s.get("band") or "", s.get("iface") or ""))]

    return {
        "generatedFrom": source.name,
        "capturedAt": (payload.get("captured_at") or "")[:16].replace("T", " "),
        "rows": rows,
        "sourceBAvailable": False,
    }


def build_static(bundle: Path | None, anon: Anonymiser,
                 survey_bundle: Path | None = None) -> dict:
    """A page whose content is synthetic in full — nothing to derive or replace.

    The workspace screens are content, not measurement: there is no captured
    file list or note board to stay faithful to, and the real ones on this bench
    are test scaffolding carrying colleagues' names. Their fixtures are the
    whole payload, and each page says so in its provenance line.
    """
    return {}


def _stamp(epoch: float) -> str:
    from datetime import datetime
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M")


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
            # `count` is the raw neighbour count the bars draw; `score` is the
            # signal-weighted occupancy WhatIfPreview compares. Different
            # measurements, carried separately so neither can stand in for the
            # other.
            "channels": [{"channel": c, "count": counts.get(c, 0),
                          "score": round(occupancy[c]) if c in occupancy else None}
                         for c in channels],
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
    ap.add_argument("--bundle", type=Path, default=None,
                    help="an extracted dut-session-<ts> directory; not needed for "
                         "pages whose content is synthetic in full")
    ap.add_argument("--survey-bundle", type=Path, default=None,
                    help="take the neighbour scan from a second bundle when --bundle "
                         "has no usable one (both sources are recorded on the page)")
    ap.add_argument("--page", default="overview.html",
                    help="demo page to rewrite: overview.html, site-survey.html "
                         "or wifi-clients.html (default: overview.html)")
    args = ap.parse_args()

    builders = {"overview.html": build, "site-survey.html": build_survey,
                "wifi-clients.html": build_clients,
                "files.html": build_static, "bulletin.html": build_static,
                "downloads.html": build_downloads,
                "serial-console.html": build_console,
                "cpu-memory.html": build_cpu,
                "ssid-capability.html": build_ssid,
                "firmware.html": build_static, "index.html": build_static}
    if args.page not in builders:
        raise SystemExit(f"no builder for {args.page}; known: {', '.join(builders)}")

    if args.bundle is None and builders[args.page] is not build_static:
        raise SystemExit(f"{args.page} is built from a capture — pass --bundle")

    anon = Anonymiser()
    payload = builders[args.page](args.bundle, anon, args.survey_bundle)
    fixed = json.loads((HERE / "demo-fixtures.json").read_text(encoding="utf-8"))
    payload.update(fixed.get(args.page, {}))     # synthetic copy, see README
    inject(HERE / args.page, payload)
    print(f"anonymised: {anon.counts() or 'nothing'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
