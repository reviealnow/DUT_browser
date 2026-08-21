"""What keeps captured identifiers out of the demo kit's published pages.

A neighbour scan sweeps up the SSIDs and BSSIDs of everyone in radio range, so
`dut-dashboard/demo/` may never ship them as captured. Two mechanisms stand
between a bundle and a page, and every property below was a review finding
before it was a test.

*Structured data* goes through ``Anonymiser``, field by field:

* the mapping depended on the order rows happened to be walked, so the same
  bundle could produce two different pages;
* the IP space was one /24 with no collision handling, so two real addresses
  could land on one fake — which understates how many distinct devices were
  seen, and that count is the measurement the page exists to show.

*Free text* — a serial log — has no schema to walk, so it is refused rather
than scrubbed. Its guard had two holes that a page of real neighbours would
have walked straight through:

* the MAC class was lowercase-only, so an uppercase ``AA:BB:CC:DD:EE:FF`` was
  not a MAC as far as the guard was concerned;
* the SSID rule matched the literal word "ssid", which is the field *label* —
  it never looked at an SSID *value*, so a log line naming a real network in
  prose was published.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import random
import shutil
import sys
import tempfile
import re
from pathlib import Path

import pytest

DEMO = Path(__file__).resolve().parents[2] / "demo" / "build_demo_data.py"


def _load():
    spec = importlib.util.spec_from_file_location("build_demo_data", DEMO)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _page_data(page: Path) -> dict:
    """The parsed `demo-data` block of a shipped page."""
    block = re.search(r'<script id="demo-data" type="application/json">(.*?)</script>',
                      page.read_text(encoding="utf-8"), re.S)
    assert block, f"{page.name} has no demo-data block"
    return json.loads(block.group(1))


@pytest.fixture(scope="module")
def anon_module():
    if not DEMO.is_file():
        pytest.skip("demo kit not present")
    return _load()


def _sample():
    return (
        [f"network-{i}" for i in range(200)],
        [f"10.0.{i // 254}.{i % 254}" for i in range(500)],
        [f"aa:bb:cc:{i // 65536:02x}:{i // 256 % 256:02x}:{i % 256:02x}" for i in range(300)],
    )


def _mapping(module, order):
    ssids, ips, macs = _sample()
    anon = module.Anonymiser()
    anon.prepare(ssids=order(ssids), ips=order(ips), macs=order(macs))
    return (
        {v: anon.ssid(v) for v in ssids},
        {v: anon.ip(v) for v in ips},
        {v: anon.mac(v) for v in macs},
    )


def test_the_mapping_does_not_depend_on_encounter_order(anon_module) -> None:
    forward = _mapping(anon_module, list)
    assert _mapping(anon_module, lambda x: list(reversed(x))) == forward
    assert _mapping(anon_module, lambda x: random.sample(x, len(x))) == forward


@pytest.mark.parametrize("index,kind", [(0, "ssid"), (1, "ip"), (2, "mac")])
def test_distinct_values_never_share_an_alias(anon_module, index: int, kind: str) -> None:
    mapping = _mapping(anon_module, list)[index]
    assert len(set(mapping.values())) == len(mapping), f"{kind} aliases collided"


def test_aliases_are_visibly_not_real_identifiers(anon_module) -> None:
    _, ips, macs = _mapping(anon_module, list)
    # 02: is the locally-administered bit — never a vendor OUI. Checking only
    # the prefix let a five-octet "MAC" ship once; the shape is asserted too.
    assert all(re.fullmatch(r"02(:[0-9a-f]{2}){5}", v) for v in macs.values()), \
        "aliases must be full 48-bit locally-administered addresses"
    # RFC 5737 documentation ranges only.
    assert all(v.split(".")[0:3] in ([  "192", "0", "2"], ["198", "51", "100"], ["203", "0", "113"])
               for v in ips.values())


def test_running_out_of_aliases_fails_loudly(anon_module) -> None:
    # Silently reusing a name would misreport how crowded the air is, so the
    # generator must stop rather than degrade.
    anon = anon_module.Anonymiser()
    with pytest.raises(SystemExit, match="exhausted"):
        anon.prepare(vendors=[f"vendor-{i}" for i in range(len(anon_module.VENDORS) + 1)])


# --- the free-text guard -------------------------------------------------
#
# These are about a *different* bundle than the one committed today. The pages
# in the repo are clean; what these pin is that the guard still refuses when
# somebody regenerates them from a capture nobody has looked at yet.


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    """A capture shaped like a real one: several snapshots, several kinds.

    A session writes a context snapshot per connect, so a bundle holds more than
    one capture per kind — the older ones are exactly as identifying as the
    newest, and the log can name a network from any of them.
    """
    root = tmp_path / "dut-session-20260808-101112"
    survey = root / "context" / "site-survey"
    capability = root / "context" / "ssid-capability"
    clients = root / "context" / "wifi-clients"
    for directory in (survey, capability, clients):
        directory.mkdir(parents=True)

    (survey / "site-survey-default-20260808-101112.json").write_text(json.dumps({
        "neighbors": [{"ssid": "HomeNetwork", "bssid": "aa:bb:cc:dd:ee:ff"},
                      {"ssid": "café upstairs", "bssid": "11:22:33:44:55:66"}],
        "vaps": [{"ssid": "OurOwnLabVap"}],
    }), encoding="utf-8")
    # An earlier snapshot of the same kind, holding a network the newest lost.
    (capability / "ssid-capability-default-20260808-090000.json").write_text(json.dumps({
        "ssids": [{"iface": "ath1", "ssid": "OlderSecret", "bssid": "aa:00:11:22:33:44"}],
    }), encoding="utf-8")
    (capability / "ssid-capability-default-20260808-101112.json").write_text(json.dumps({
        "ssids": [{"iface": "ath0", "ssid": "LabGuest", "bssid": "77:88:99:aa:bb:cc"}],
    }), encoding="utf-8")
    # A kind the harvester never named: its MACs have a shape, its SSIDs do not.
    (clients / "wifi-clients-default-20260808-101112.json").write_text(json.dumps({
        "clients": [{"mac_address": "de:ad:be:ef:00:02", "ssid_name": "ClientSideOnly"}],
    }), encoding="utf-8")

    with (root / "08081011_notime_wifi_clients.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["ts", "ssid_name", "mac_address", "ip_address"])
        writer.writeheader()
        writer.writerow({"ts": "2026-08-08T10:11:12", "ssid_name": "OurOwnLabVap",
                         "mac_address": "de:ad:be:ef:00:01", "ip_address": "192.168.7.31"})
    return root


def test_the_guard_learns_every_identifier_in_the_capture(anon_module, bundle: Path) -> None:
    known = anon_module.captured_identifiers(bundle)
    # Neighbours, our own VAPs and the capability report all count: the pages
    # publish an alias for every one of them.
    assert {"homenetwork", "café upstairs", "ourownlabvap", "labguest"} <= known
    assert {"aa:bb:cc:dd:ee:ff", "de:ad:be:ef:00:01", "192.168.7.31"} <= known


def test_an_older_snapshot_is_no_less_identifying_than_the_newest(
    anon_module, bundle: Path,
) -> None:
    """Reading the newest capture per kind published everything before it.

    A session writes a snapshot per connect, and the serial log spans all of
    them. `OlderSecret` lives only in the earlier ssid-capability report.
    """
    known = anon_module.captured_identifiers(bundle)
    assert "oldersecret" in known
    assert anon_module.identifier_in("station joined OlderSecret", known) is not None


def test_a_kind_nobody_named_is_still_read(anon_module, bundle: Path) -> None:
    """`context/wifi-clients/` was never opened, so its SSIDs were free text.

    Its MACs and IPs have a shape and were caught anyway; that is what made the
    gap easy to miss. Harvesting walks the files now, not a list of kinds, so a
    context kind added later needs no change here.
    """
    known = anon_module.captured_identifiers(bundle)
    assert "clientsideonly" in known
    assert anon_module.identifier_in("roamed onto ClientSideOnly", known) is not None


def test_a_capture_free_directory_yields_nothing_rather_than_failing(anon_module, tmp_path) -> None:
    assert anon_module.captured_identifiers(tmp_path) == frozenset()
    assert anon_module.captured_identifiers(None) == frozenset()


def test_a_capture_that_cannot_be_read_stops_the_build(anon_module, bundle: Path) -> None:
    """Skipping a damaged snapshot is fail-open, and silently so.

    If the truncated file was the only structured record of a bare SSID, the
    name never enters the inventory and the guard then waves it through in a
    log line — the exact failure the inventory exists to prevent.
    """
    broken = bundle / "context" / "site-survey" / "site-survey-default-20260808-120000.json"
    broken.write_text('{"neighbors": [{"ssid": "TruncatedSecret"', encoding="utf-8")
    with pytest.raises(SystemExit, match="could not be read"):
        anon_module.captured_identifiers(bundle)
    # The operator has to be told which file, or they cannot act on it.
    with pytest.raises(SystemExit, match=broken.name):
        anon_module.captured_identifiers(bundle)


def test_a_csv_that_cannot_be_decoded_stops_the_build(anon_module, bundle: Path) -> None:
    """Same rule for the other half — and `errors="ignore"` was worse than a skip.

    Replacing an undecodable byte rewrites the value, so the SSID would enter
    the inventory in a form the log's own bytes can never match: fail-open, with
    a full inventory to look at.
    """
    broken = bundle / "context" / "site-survey" / "site-survey-default-20260808-120000.csv"
    broken.write_bytes(b"ssid,bssid\n\xff\xfeCaf\xe9Secret,aa:bb:cc:dd:ee:01\n")
    with pytest.raises(SystemExit, match="could not be read"):
        anon_module.captured_identifiers(bundle)


def test_an_unterminated_quote_stops_the_build(anon_module, bundle: Path) -> None:
    """The default CSV dialect is `strict=False`, which merges fields silently.

    An unterminated quote swallows the rest of the line, so `"OlderSecret` plus
    its BSSID arrive as one value: the bare SSID is then not in the inventory at
    all and passes straight through from a log line. Catching `csv.Error`
    without `strict=True` catches nothing here.
    """
    broken = bundle / "context" / "site-survey" / "site-survey-default-20260808-130000.csv"
    broken.write_text('ssid,bssid\n"QuotedSecret,aa:bb:cc:dd:ee:02\n', encoding="utf-8")
    with pytest.raises(SystemExit, match="could not be read"):
        anon_module.captured_identifiers(bundle)
    with pytest.raises(SystemExit, match=broken.name):
        anon_module.captured_identifiers(bundle)


@pytest.mark.parametrize("label,text,reason", [
    # Every one of these produces rows that pass the field-count check while
    # the value the log could name is simply not in the inventory.
    ("an unnamed column",
     "ts,,mac_address\nnow,OlderSecret,de:ad:be:ef:00:01\n", "no name"),
    ("a repeated column",
     "ssid_name,ssid_name\nOlderSecret,NewPublic\n", "appears twice"),
    ("no header at all",
     "", "no header row"),
    # One column named `ssid;bssid` holding `OlderSecret;aa:bb:…`, so every
    # value merges and none is collected.
    ("a file that is not comma-separated",
     "ssid;bssid\nOlderSecret;aa:bb:cc:dd:ee:ff\n", "not a bare ASCII column name"),
    # The three cases a punctuation blacklist could never have covered, which
    # is why the rule is a grammar: a delimiter nobody listed, a padded name
    # that passed validation and then missed the identifier lookup, and a BOM.
    ("a delimiter nobody listed",
     "ssid:bssid\nOlderSecret:aa:bb:cc:dd:ee:ff\n", "not a bare ASCII column name"),
    ("a padded column name",
     " ssid ,bssid\nOlderSecret,aa:bb:cc:dd:ee:ff\n", "not a bare ASCII column name"),
    ("a byte-order mark",
     "﻿ssid,bssid\nOlderSecret,aa:bb:cc:dd:ee:ff\n", "not a bare ASCII column name"),
])
def test_a_damaged_header_stops_the_build(
    anon_module, bundle: Path, label: str, text: str, reason: str,
) -> None:
    """DictReader addresses the row through the header, so a damaged one drops
    values silently — and the rows still look the right width."""
    broken = bundle / "context" / "site-survey" / "site-survey-default-20260808-150000.csv"
    broken.write_text(text, encoding="utf-8")
    with pytest.raises(SystemExit, match=reason):
        anon_module.captured_identifiers(bundle)
    with pytest.raises(SystemExit, match=broken.name):
        anon_module.captured_identifiers(bundle)


def test_the_header_check_and_the_lookup_agree_on_what_a_column_is_called(
    anon_module, bundle: Path,
) -> None:
    """Two spellings of "the same column" is a fail-open all by itself.

    Validation canonicalised with `name.strip().lower()` and collection looked
    up `name.lower()`, so `" ssid "` was accepted as the column `ssid` and then
    never matched `IDENTIFIER_KEYS`. Neither function was wrong on its own.
    One `_field_key` now serves both, and the grammar means it never has to
    strip anything.
    """
    assert anon_module._field_key(" SSID ") != "ssid", "no strip: the grammar rejects padding"
    assert anon_module._field_key("SSID") == "ssid"
    # Every key the collector looks for must itself be a legal column name, or
    # a capture could never declare one.
    for key in anon_module.IDENTIFIER_KEYS:
        assert anon_module.COLUMN_NAME_RE.fullmatch(key), key
        assert anon_module._field_key(key) == key

    # And a header the check accepts is one the collector then reads.
    fine = bundle / "context" / "site-survey" / "site-survey-default-20260808-170000.csv"
    fine.write_text("SSID,BSSID\nUpperCaseHeader,aa:bb:cc:dd:ee:05\n", encoding="utf-8")
    assert "uppercaseheader" in anon_module.captured_identifiers(bundle)


def test_a_legitimately_empty_value_is_not_a_damaged_row(anon_module, bundle: Path) -> None:
    """The checks must not fire on a capture that simply has a blank field.

    A missing field is None; an empty one is "". Conflating them would stop the
    build on perfectly good bundles, and over-refusal that blocks every capture
    is not fail-closed, it is broken.
    """
    fine = bundle / "context" / "site-survey" / "site-survey-default-20260808-160000.csv"
    fine.write_text("ssid,bssid\n,aa:bb:cc:dd:ee:04\nRealName,\n", encoding="utf-8")
    known = anon_module.captured_identifiers(bundle)
    assert {"realname", "aa:bb:cc:dd:ee:04"} <= known


def test_a_row_that_does_not_match_the_header_stops_the_build(anon_module, bundle: Path) -> None:
    """Strict quoting still does not catch a row with the wrong field count.

    A row that lost its first field shifts every value one column left, so an
    SSID lands under `ts` — not an identifier key — and is never collected,
    while the row still parses without complaint.
    """
    broken = bundle / "context" / "wifi-clients" / "wifi-clients-default-20260808-140000.csv"
    broken.write_text("ts,ssid_name,mac_address\nShiftedSecret,de:ad:be:ef:00:03\n",
                      encoding="utf-8")
    with pytest.raises(SystemExit, match="field count"):
        anon_module.captured_identifiers(bundle)


@pytest.mark.parametrize("text", [
    "peer AA:BB:CC:DD:EE:FF connected",              # the review's own repro
    "peer aa:BB:cc:DD:ee:FF connected",              # mixed case
    "peer aa:bb:cc:dd:ee:ff connected",              # lowercase, always caught
    "wlan0: assoc 11:22:33:44:55:66",
])
def test_a_mac_is_refused_whatever_its_case(anon_module, text: str) -> None:
    assert anon_module.identifier_in(text) == "a MAC address"
    with pytest.raises(SystemExit, match="a MAC address"):
        anon_module.refuse_if_identifying(text, "the excerpt")


@pytest.mark.parametrize("text", [
    "peer AA:BB:CC:DD:EE:FF connected to HomeNetwork",
    "station roamed to HomeNetwork",                  # no shape at all: bare value
    "station roamed to homenetwork",                  # case folded both ways
    "hostapd: interface for café upstairs is down",   # spaces and non-ASCII
    "assoc on OurOwnLabVap",                          # our own VAP is no freer
    "reading config for LabGuest",                    # learned from Source A
])
def test_a_bare_ssid_value_is_refused_once_the_capture_is_known(
    anon_module, bundle: Path, text: str,
) -> None:
    known = anon_module.captured_identifiers(bundle)
    assert anon_module.identifier_in(text, known) is not None
    with pytest.raises(SystemExit):
        anon_module.refuse_if_identifying(text, "the excerpt", known)
    # …and without the capture, the shape patterns alone do NOT save you. This
    # is the hole the review found: fail closed only works if the guard is
    # given something to close on.
    if not re.search(r"(?i)([0-9a-f]{2}:){5}[0-9a-f]{2}", text):
        assert anon_module.identifier_in(text) is None


# --- what may be embedded in a page -------------------------------------
#
# A PNG cannot be aliased and no text scan will flag one, so which images travel
# is decided by an allowlist of plot kinds, and the two artifacts that are tables
# rendered to pixels are redrawn from the aliased snapshot instead.


@pytest.mark.parametrize("name,suffix", [
    ("08081837_notime_cpu_usage_plot.png", "cpu_usage_plot.png"),
    ("08041542_notime_survey_channels_2g4.png", "survey_channels_2g4.png"),
    ("no_prefix_at_all.png", "no_prefix_at_all.png"),
])
def test_the_plot_kind_is_read_without_the_bundle_stamp(anon_module, name, suffix) -> None:
    # The prefix is a per-bundle stamp, so matching whole names would tie the
    # allowlist to one capture.
    assert anon_module._artifact_suffix(name) == suffix


def test_an_unrecognised_plot_kind_is_withheld_not_embedded(anon_module) -> None:
    """The allowlist is positive, so a new plot kind is withheld until judged.

    The generator cannot inspect pixels. Anything not established as
    identifier-free has not been shown to be safe to publish, and a demo that
    embedded it by default would be publishing on the strength of nobody having
    looked.
    """
    assert "wifi_clients_plot.png" in anon_module.EMBEDDABLE_PLOTS
    # The two that are tables of real identifiers are deliberately absent.
    assert "ssid_capability.png" not in anon_module.EMBEDDABLE_PLOTS
    assert "wifi_clients_table.png" not in anon_module.EMBEDDABLE_PLOTS
    assert set(anon_module._REDRAWN_TABLES) == {"ssid_capability.png", "wifi_clients_table.png"}
    # And a kind nobody has classified is on neither list.
    assert "wifi_something_new_plot.png" not in anon_module.EMBEDDABLE_PLOTS
    assert "wifi_something_new_plot.png" not in anon_module._REDRAWN_TABLES


def test_aliasing_a_snapshot_replaces_identifiers_and_keeps_measurements(anon_module) -> None:
    snapshot = {"clients": [{
        "mac": "fe:47:fc:93:bf:ee", "ssid": "AP6_2.4GWPA2", "vendor": "2C:1F:23",
        "ip": "192.168.1.44", "rssi": -39, "snr": 55, "txrate": "130M",
        "assoc_time": "25:24:51", "band": "2.4G", "channel": 11,
    }]}
    anon = anon_module.Anonymiser()
    out = anon_module._alias_snapshot(snapshot, anon)["clients"][0]

    assert out["mac"].startswith("02:") and out["mac"] != snapshot["clients"][0]["mac"]
    assert out["ssid"].startswith("DemoAP-")
    assert out["vendor"] in anon_module.VENDORS
    assert out["ip"] != snapshot["clients"][0]["ip"]
    # The measurement is the point of the artifact and must survive untouched.
    for field in ("rssi", "snr", "txrate", "assoc_time", "band", "channel"):
        assert out[field] == snapshot["clients"][0][field]


def _colliding_pair(anon_module, kind: str, space: int) -> tuple[str, str]:
    """Two values that want the same bucket — where encounter order can matter.

    Found rather than hardcoded: the bucket is a sha256 of the value, so the
    pair is fixed for a given pool size but would silently stop colliding if the
    pool were resized, and a test that quietly stopped testing anything is worse
    than no test.
    """
    seen: dict[int, str] = {}
    for i in range(2000):
        value = f"{kind}-{i}"
        bucket = anon_module._bucket(value, space, kind)
        if bucket in seen:
            return seen[bucket], value
        seen[bucket] = value
    raise AssertionError(f"no {kind} collision found in a space of {space}")


def test_redraw_aliases_do_not_depend_on_the_order_rows_are_walked(anon_module) -> None:
    """The kit's order-independence guarantee has to hold on the redraw path too.

    `Anonymiser` is order-independent only through `prepare()`, which assigns
    over the sorted set. Aliasing values as they are met takes the on-demand
    path, where the first value to reach a bucket keeps it and the next probes
    forward — so two colliding identifiers swap aliases if the rows are
    reordered. Nothing leaks and the mapping stays injective, which is exactly
    why an ordinary example would not catch it.
    """
    first, second = _colliding_pair(anon_module, "vendor", len(anon_module.VENDORS))
    assert (anon_module._bucket(first, len(anon_module.VENDORS), "vendor")
            == anon_module._bucket(second, len(anon_module.VENDORS), "vendor")), \
        "the fixture must actually collide, or this test proves nothing"

    def aliases(tmp: Path, order: list[str]) -> dict[str, str]:
        """Through the real path: a bundle on disk, `_prepare_redraw`, then alias."""
        kind_dir = tmp / "context" / "wifi-clients"
        kind_dir.mkdir(parents=True, exist_ok=True)
        rows = [{"mac": f"aa:bb:cc:00:00:{i:02x}", "vendor": v} for i, v in enumerate(order)]
        (kind_dir / "wifi-clients-default-20260808-101112.json").write_text(
            json.dumps({"clients": rows}), encoding="utf-8")
        anon = anon_module.Anonymiser()
        snapshots = anon_module._prepare_redraw(tmp, ["wifi_clients_table.png"], anon)
        out = anon_module._alias_snapshot(snapshots["wifi_clients_table.png"], anon)["clients"]
        return {row["vendor"]: alias["vendor"] for row, alias in zip(rows, out)}

    with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
        forward = aliases(Path(a), [first, second])
        assert aliases(Path(b), [second, first]) == forward, "row order changed the aliases"
    assert len(set(forward.values())) == 2, "colliding values must still get distinct aliases"


def test_every_redrawn_snapshot_is_prepared_before_any_is_rendered(
    anon_module, bundle: Path,
) -> None:
    """One `prepare()` over both artifacts, not one per artifact.

    Preparing per artifact would make the aliases depend on which table was
    rendered first, which is the same defect one level up.
    """
    anon = anon_module.Anonymiser()
    snapshots = anon_module._prepare_redraw(
        bundle, ["ssid_capability.png", "wifi_clients_table.png"], anon)
    assert set(snapshots) == {"ssid_capability.png", "wifi_clients_table.png"}
    # Every identifier in both snapshots already has an alias, so rendering
    # cannot be the first place one is seen.
    found = {"ssid": set(), "mac": set(), "ip": set(), "vendor": set()}
    for payload in snapshots.values():
        anon_module._identifiers_by_kind(payload, found)
    for kind, values in found.items():
        for value in values:
            key = value.lower() if kind == "mac" else value
            assert key in anon._maps[kind], f"{kind} {value!r} was not prepared"


def test_nothing_aliased_is_still_in_the_identifier_inventory(anon_module, bundle: Path) -> None:
    """The leak test for the redraw path, since its output is pixels.

    Whatever is handed to the renderer must contain no value the inventory knows
    as a real identifier. Checking the input is the only way to check the image:
    once it is a PNG, no scan can read it.
    """
    known = anon_module.captured_identifiers(bundle)
    snapshot = json.loads(
        (bundle / "context" / "ssid-capability"
         / "ssid-capability-default-20260808-101112.json").read_text(encoding="utf-8"))
    assert any(str(v).lower() in known for v in
               [e["ssid"] for e in snapshot["ssids"]]), "fixture must start out identifying"

    aliased = anon_module._alias_snapshot(snapshot, anon_module.Anonymiser())

    def strings(node):
        if isinstance(node, dict):
            for value in node.values():
                yield from strings(value)
        elif isinstance(node, list):
            for item in node:
                yield from strings(item)
        elif isinstance(node, str):
            yield node

    leaked = [s for s in strings(aliased) if s.lower() in known]
    assert not leaked, f"aliased snapshot still carries {leaked}"


# --- the model rename ----------------------------------------------------
#
# Editorial, not confidentiality: the model is public hardware. But the promise
# is that a *regeneration* cannot put it back into a published page, so it has
# to hold for names this bench has not produced yet.


@pytest.mark.parametrize("captured,expected", [
    # Both casings a capture actually writes: the console prints one, the
    # workspace the other. A case-sensitive pass missed the second.
    ("dut-session-AP6_840E.log", "dut-session-DemoDUT-6E.log"),
    ("dut-session-ap6_840e.log", "dut-session-DemoDUT-6E.log"),
    ("ap6-420e-notes.txt", "DemoDUT-5G-notes.txt"),
    ("AP6_lab2", "DemoDUT-lab2"),          # prefix form: no trailing boundary
    ("AP6_840E# ", "DemoDUT-6E# "),        # the console prompt
    ("ubi_kernel_AP6_840E-encrypt_1.10.339.bin",
     "ubi_kernel_DemoDUT-6E-encrypt_1.10.339.bin"),
    # The id alone, as the real session log carries it: `-` before, `_` after.
    ("dut-session-420E_110341-20260806-095724.log",
     "dut-session-DemoDUT5G_110341-20260806-095724.log"),
    # Glued to the word before it. Requiring a boundary here produced the worst
    # result available — the id renamed and the `AP6` left behind, which reads
    # as deliberate rather than as a miss.
    ("dutAP6_840E.log", "dutDemoDUT-6E.log"),
])
def test_the_model_is_renamed_whatever_the_capture_called_it(
    anon_module, captured: str, expected: str,
) -> None:
    assert anon_module.demo_name(captured) == expected


def test_no_rename_ever_leaves_the_model_half_removed(anon_module) -> None:
    """A partial rename is worse than none: it looks like the intended name.

    Whatever `demo_name` returns must not still contain the vendor prefix. The
    one shape that survives untouched is pinned separately below — a clean miss
    is a different failure from a misleading half-result.
    """
    for captured in ("AP6_840E", "ap6-420e-notes.txt", "dutAP6_840E.log",
                     "x_AP6_lab2", "AP6_840E# ", "path/to/AP6_420E/file.csv"):
        assert "ap6" not in anon_module.demo_name(captured).lower(), captured


def test_a_model_name_with_no_separator_is_a_known_miss(anon_module) -> None:
    """`AP6840E` is not a form any capture writes, and it is not renamed.

    Pinned so the gap is a decision on the record rather than a surprise. It is
    a clean miss — nothing is half-renamed — and closing it would mean matching
    `ap6` followed by digits, which starts guessing at names nobody has seen.
    """
    assert anon_module.demo_name("AP6840E") == "AP6840E"


@pytest.mark.parametrize("untouched", [
    # The id is short enough to fall inside words that identify nothing, and
    # Downloads says its listing is a real bundle — rewriting these would
    # falsify a measured filename to solve a naming problem.
    "sha420Eabcd.txt",
    "report-840Errors.csv",
    "08081837_notime_cpu_usage.csv",
    "capture-report.txt",
])
def test_a_name_that_only_looks_like_the_model_is_left_alone(
    anon_module, untouched: str,
) -> None:
    assert anon_module.demo_name(untouched) == untouched


def test_renaming_is_idempotent(anon_module) -> None:
    """Applied twice — regenerating a page already built — nothing shifts."""
    once = anon_module.demo_name("dut-session-420E_110341-20260806.log")
    assert anon_module.demo_name(once) == once


def test_the_front_door_is_refused_by_name_not_by_a_missing_block(anon_module) -> None:
    """index.html has no data block, so it was never regenerable — say that.

    It sat in the builder map, so `--page index.html` always died on the missing
    block, and "every page regenerates" was a claim about a page that has
    nothing to regenerate.
    """
    assert "index.html" in anon_module.HAND_MAINTAINED
    assert "index.html" not in anon_module.PAGE_BUILDERS


def test_clean_monitoring_output_still_passes(anon_module, bundle: Path) -> None:
    known = anon_module.captured_identifiers(bundle)
    text = "=== CPU ===\nCpu(s):  3.4 us,  1.2 sy, 95.0 id\nMemAvailable:   183624 kB"
    assert anon_module.identifier_in(text, known) is None
    assert anon_module.refuse_if_identifying(text, "the excerpt", known) == text


def test_the_excerpt_selector_and_the_refusal_ask_the_same_question(anon_module, bundle) -> None:
    """A selector with a weaker rule hands the refuser a run it must reject.

    build_console splits the log on `identifier_in` and then refuses the joined
    excerpt with `refuse_if_identifying`. If those two ever disagree, either the
    build dies on a run it just chose, or — the dangerous direction — a line the
    selector waved through is published.
    """
    known = anon_module.captured_identifiers(bundle)
    lines = ["=== CPU ===", "peer AA:BB:CC:DD:EE:FF connected", "load average: 0.31",
             "roamed to HomeNetwork", "MemFree: 12 kB", "192.168.7.31 pinged"]
    for line in lines:
        refused = anon_module.identifier_in(line, known) is not None
        raised = False
        try:
            anon_module.refuse_if_identifying(line, "the excerpt", known)
        except SystemExit:
            raised = True
        assert refused == raised, line


# --- the data block's hand-maintained keys ---------------------------------
#
# `build()` fills seven keys and demo-fixtures.json supplies three, which
# together are every key overview.html carries: a whole-block rewrite lost
# nothing in the normal path. The merge protects the case neither side knows
# about — a key added to a page by hand — which is worth keeping because that
# key is a mistake, and one that used to disappear without a word.


def test_a_regeneration_keeps_the_keys_no_builder_produces(anon_module, tmp_path) -> None:
    page = tmp_path / "overview.html"
    page.write_text(
        '<script id="demo-data" type="application/json">'
        '{"cpu":"old","fleet":[{"id":"lab-420"}],"crash":["boom"]}'
        "</script>",
        encoding="utf-8",
    )

    anon_module.inject(page, {"cpu": "fresh"})

    import json as _json
    import re as _re
    block = _re.search(r">(\{.*\})<", page.read_text(encoding="utf-8")).group(1)
    data = _json.loads(block)
    assert data["cpu"] == "fresh"                      # the builder's key wins
    assert data["fleet"] == [{"id": "lab-420"}]        # hand-maintained, kept
    assert data["crash"] == ["boom"]


def test_a_builder_key_still_overwrites(anon_module, tmp_path) -> None:
    page = tmp_path / "overview.html"
    page.write_text(
        '<script id="demo-data" type="application/json">{"cpu":"stale","fleet":[]}</script>',
        encoding="utf-8",
    )
    anon_module.inject(page, {"cpu": "fresh", "fleet": [{"id": "new"}]})
    import json as _json
    import re as _re
    data = _json.loads(_re.search(r">(\{.*\})<", page.read_text(encoding="utf-8")).group(1))
    assert data == {"cpu": "fresh", "fleet": [{"id": "new"}]}


def test_a_malformed_block_is_refused_not_replaced(anon_module, tmp_path) -> None:
    """Swallowing the parse error would merge over nothing and write the
    builder's keys alone — the data loss this merge exists to prevent."""
    page = tmp_path / "overview.html"
    original = '<script id="demo-data" type="application/json">{"fleet":[oops</script>'
    page.write_text(original, encoding="utf-8")

    with pytest.raises(SystemExit) as caught:
        anon_module.inject(page, {"cpu": "fresh"})

    assert "not valid JSON" in str(caught.value)
    assert page.read_text(encoding="utf-8") == original      # untouched


def test_the_regeneration_source_carries_every_fleet_state(anon_module) -> None:
    """The fleet is synthetic, so `demo-fixtures.json` owns it — not the page.
    Hand-editing the page's data block put the remote nodes somewhere the very
    next `build_demo_data.py --page overview.html` would overwrite from a stale
    fixture: the destructive regeneration this suite exists to stop, one level
    below the key merge.

    Asserted by state rather than by count, so adding a card is not a failure
    while losing a state still is.
    """
    demo = DEMO.parent
    fixture = json.loads((demo / "demo-fixtures.json").read_text(encoding="utf-8"))
    fleet = fixture["overview.html"]["fleet"]
    remotes = [f for f in fleet if f.get("remote")]

    assert remotes, "no remote node survives a rebuild"
    assert {f["backhaul"]["applicable"] for f in fleet} == {True, False}, "no standalone AP"
    assert {f["backhaul"]["role"] for f in fleet} >= {"node", "root"}
    assert any(f["backhaul"].get("pendingRead") for f in remotes), "nothing to capture on connect"
    assert any(f.get("remote") and not f["open"] for f in fleet), "no disconnected remote"
    assert any(not f.get("remote") for f in fleet), "no mother-server card"
    # A cabled DUT's backhaul is measured by the same two console commands as a
    # node's, and the strip shows it on the mother-server card. Without one
    # here the kit is back to portraying that measurement as an SSH-only
    # capability, which is what it stopped being.
    assert any(
        not f.get("remote") and f["backhaul"]["captured"] for f in fleet
    ), "no cabled DUT with a backhaul reading"
    assert len({f["status"] for f in fleet}) >= 3, {f["status"] for f in fleet}
    assert any(not f["open"] for f in fleet), "no disconnected card"

    page = _page_data(demo / "overview.html")
    assert page["fleet"] == fleet, "the page and its regeneration source disagree"


def test_the_page_and_its_sources_carry_exactly_the_same_keys(anon_module) -> None:
    """The real account of the defect, pinned so the wrong one cannot come back.

    Nothing was lost in the normal path — the two sources cover the page
    exactly, in both directions. A key on the page that neither produces is
    either synthetic copy that belongs in the fixture or an accident, and used
    to vanish on the next rebuild. A key a source produces that the page lacks
    is the same disagreement seen from the other side: the committed page is
    already out of step with what regenerating it would write.
    """
    import ast

    demo = DEMO.parent
    tree = ast.parse(DEMO.read_text(encoding="utf-8"))
    build = next(n for n in tree.body
                 if isinstance(n, ast.FunctionDef) and n.name == "build")
    returned = next(n for n in ast.walk(build)
                    if isinstance(n, ast.Return) and isinstance(n.value, ast.Dict))
    builder = {k.value for k in returned.value.keys}

    fixture = set(json.loads((demo / "demo-fixtures.json").read_text(encoding="utf-8"))["overview.html"])
    page = set(_page_data(demo / "overview.html"))

    # Both directions. A subset check passes while the fixture carries a key the
    # page does not, and the next rebuild injects it — the committed page and
    # its own regeneration would already disagree.
    assert page == builder | fixture, {
        "on the page, from neither source": sorted(page - builder - fixture),
        "produced but not on the page": sorted((builder | fixture) - page),
    }


# --- the fleet, told on two screens ----------------------------------------
#
# `FleetCard` renders both the Overview strip and the Fleet section, so the two
# screens are one account of six DUTs — "two components would have been two
# accounts of the same DUT, and the next correction would have landed in one of
# them". The kit's pages are standalone files and each inlines its own copy,
# which is the whole point of the kit; what does not follow is that the two
# copies may say different things. `fleet.html` carries the registry's own
# shape, structured uplink and downlink objects; `overview.html` carries the two
# compressed lines the strip has room for. So the compression is done here, from
# the structured record, rather than trusting that whoever edited one remembered
# the other.


def _compress_uplink(remote: dict) -> str:
    """FleetCard's "Uplink to parent" line, from the registry's record.

    Order matters and is the component's: not-a-mesh is answered before
    is-a-root, and both before "nothing captured yet" — a root has no parent and
    a capture established that, so reporting it as a missing measurement would
    be a different claim.
    """
    if not remote["isMesh"]:
        return "Not applicable"
    if remote["role"] == "root":
        return "None — this is the root"
    uplink = remote["uplink"]
    if not uplink or uplink["rssi"] is None:
        return "Not captured"
    return f"{uplink['rssi']} dBm · {uplink['rssi_band']}"


def _compress_downlink(remote: dict) -> str:
    """FleetCard's "Children on backhaul" line, minus the configured suffix.

    The strip carries that suffix as its own fields (`downlinkSource`,
    `downlinkIface`, `downlinkEssid`) because the page builds the sentence, so
    it is compared separately below rather than glued on here.
    """
    if not remote["isMesh"]:
        return "Not applicable"
    downlink = remote["downlink"]
    if not downlink:
        return "Not captured"
    if not downlink["peers"]:
        return "None"
    return " · ".join("—" if p["rssi"] is None else f"{p['rssi']} dBm"
                      for p in downlink["peers"])


def test_the_fleet_says_the_same_thing_on_both_screens() -> None:
    """One fleet, two views — and the strip's is derived, not typed twice."""
    fixture = json.loads((DEMO.parent / "demo-fixtures.json").read_text(encoding="utf-8"))
    strip = {d["id"]: d for d in fixture["overview.html"]["fleet"]}
    section = {d["id"]: d for d in fixture["fleet.html"]["nodes"]}

    assert set(strip) == set(section), "the two screens list different DUTs"

    shared = ("label", "status", "cpu", "cpuSub", "reco", "recoLevel", "crashCount",
              "lastEvent", "lastSnapshot", "open", "lastSerial")
    for dut_id, node in section.items():
        card = strip[dut_id]
        assert {k: node[k] for k in shared} == {k: card[k] for k in shared}, dut_id
        assert ("remote" in node) == ("remote" in card), dut_id
        if "remote" not in node:
            continue

        remote, compressed = node["remote"], card["remote"]
        for key in ("host", "port", "isMesh", "role"):
            assert remote[key] == compressed[key], f"{dut_id}.{key}"
        assert compressed["uplink"] == _compress_uplink(remote), dut_id
        assert compressed["downlink"] == _compress_downlink(remote), dut_id

        source = (remote["downlink"] or {}).get("source", "detected")
        assert compressed["downlinkSource"] == source, dut_id
        if source == "configured":
            assert compressed["downlinkIface"] == remote["downlink"]["iface"], dut_id
            assert compressed["downlinkEssid"] == remote["downlink"]["essid"], dut_id

        # What pressing Connect would capture. Both screens run it on connect, as
        # FleetCard's onConnect does, so both have to agree about the reading.
        pending = remote["capturedOnConnect"]
        assert bool(pending) == bool(compressed.get("captured")), dut_id
        if pending:
            after = {**remote, "role": pending["role"], "uplink": pending["uplink"],
                     "downlink": pending["downlink"]}
            assert compressed["captured"]["uplink"] == _compress_uplink(after), dut_id
            assert compressed["captured"]["downlink"] == _compress_downlink(after), dut_id


def test_the_fleet_page_carries_every_state_the_section_can_show() -> None:
    """The under-showing direction, which has no chip to catch it.

    A demo misrepresents the product by showing LESS as easily as more, and the
    Fleet section exists for measurements the strip cannot hold: the uplink's
    SNR, radio band and parent BSSID had no reader anywhere in the frontend, and
    per-child RSSI was legible for one child only. A fixture trimmed back to one
    tidy node would render a page that looks finished and shows none of that.
    """
    fixture = json.loads((DEMO.parent / "demo-fixtures.json").read_text(encoding="utf-8"))
    nodes = fixture["fleet.html"]["nodes"]
    remotes = [n["remote"] for n in nodes if "remote" in n]
    uplinks = [r["uplink"] for r in remotes if r["uplink"]]
    downlinks = [r["downlink"] for r in remotes if r["downlink"]]

    assert any(all(u[f] is not None for f in ("snr", "radio_band", "peer_mac", "essid"))
               for u in uplinks), "no uplink exercises the fields only this page renders"
    assert any(len(d["peers"]) > 1 for d in downlinks), "no backhaul with more than one child"
    assert any(len({p["rssi"] for p in d["peers"]}) > 1 for d in downlinks), \
        "every child hears the same, so per-child RSSI shows nothing the strip did not"
    assert {d["source"] for d in downlinks} == {"detected", "configured"}, \
        "the unverified-interface disclosure has no card to appear on"
    assert any(r["isMesh"] and not r["uplink"] and not r["downlink"] for r in remotes), \
        "no mesh node in the never-captured state"
    assert any(not r["isMesh"] for r in remotes), "no standalone AP"
    assert any("remote" not in n for n in nodes), "no mother-server card"
    assert any(r["capturedOnConnect"] for r in remotes), "nothing to capture on connect"

    assert any(r["role"] == "root" for r in remotes), "no root, so nothing can be a parent"

    # Every identifier here is synthetic and visibly so — MACs carry the 02:
    # locally-administered prefix and backhaul SSIDs the DemoAP-* namespace, the
    # same shapes `Anonymiser` emits. Nothing on this page is captured, which is
    # exactly why it is where a real BSSID off the bench would get typed in.
    macs = [u["peer_mac"] for u in uplinks if u["peer_mac"]]
    macs += [p["mac"] for d in downlinks for p in d["peers"]]
    assert macs, "no BSSID anywhere; the field that ties two cards together is untested"
    assert all(re.fullmatch(r"02(:[0-9a-f]{2}){5}", mac) for mac in macs), macs
    essids = [u["essid"] for u in uplinks if u["essid"]]
    essids += [d["essid"] for d in downlinks if d["essid"]]
    assert essids and all(name.startswith("DemoAP-") for name in essids), essids

    page = _page_data(DEMO.parent / "fleet.html")
    assert page["nodes"] == nodes, "the page and its regeneration source disagree"


# --- the fixture-only sync -------------------------------------------------
#
# `demo-fixtures.json` owns the synthetic data, the pages ship it inlined, and
# until now the only thing that carried one into the other was a full
# regeneration — which needs a capture bundle for seven of the eleven. So a
# hand-written crash line or a sixth fleet card was either edited into the page,
# where the next rebuild overwrites it from the stale fixture, or edited into
# the fixture, where nobody can see it. `--sync-fixtures` is the path that needs
# no capture, and the tests below are about what it must and must not touch.


def _kit_copy(tmp_path: Path):
    """A throwaway copy of the kit, loaded so its `HERE` points into the copy.

    The sync writes to the pages beside the script, so driving the real
    directory would mean editing the working tree to make an assertion about it,
    and a failing test would leave it edited.
    """
    demo = tmp_path / "demo"
    demo.mkdir(parents=True)
    source = DEMO.parent
    shutil.copy(DEMO, demo / DEMO.name)
    shutil.copy(source / "demo-fixtures.json", demo / "demo-fixtures.json")
    for page in json.loads((source / "demo-fixtures.json").read_text(encoding="utf-8")):
        if page != "_comment":
            shutil.copy(source / page, demo / page)
    spec = importlib.util.spec_from_file_location(
        f"build_demo_data_{tmp_path.name}", demo / DEMO.name)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    assert module.HERE == demo.resolve()
    return module, demo


def _run(module, monkeypatch, *argv: str) -> int:
    monkeypatch.setattr(sys, "argv", ["build_demo_data.py", *argv])
    return module.main()


def _edit_fixture(demo: Path, page: str, key: str, value) -> None:
    path = demo / "demo-fixtures.json"
    fixture = json.loads(path.read_text(encoding="utf-8"))
    fixture[page][key] = value
    path.write_text(json.dumps(fixture, indent=2), encoding="utf-8")


def _synced_pages() -> list[str]:
    return [p for p in json.loads(
        (DEMO.parent / "demo-fixtures.json").read_text(encoding="utf-8")) if p != "_comment"]


def test_every_fixture_names_a_page_the_generator_writes(anon_module) -> None:
    """A fixture keyed to a page nobody builds is data with no way out.

    It would sit there looking authoritative while the page it names went on
    carrying something else, which is the disagreement this whole section is
    about — one typo away.
    """
    for page in _synced_pages():
        assert page in anon_module.PAGE_BUILDERS, page
        assert page not in anon_module.HAND_MAINTAINED, page
        assert (DEMO.parent / page).is_file(), page


def test_syncing_the_committed_kit_writes_the_same_bytes_back(tmp_path, monkeypatch) -> None:
    """The CI check: run the sync, and the tree must not move.

    Byte comparison rather than key comparison, because the postcondition worth
    having is the one a `git diff --exit-code` would report — a fixture edited
    without the pages being synced fails here, at the same gate as everything
    else, instead of shipping a page that disagrees with its own source.
    """
    module, demo = _kit_copy(tmp_path)

    assert _run(module, monkeypatch, "--sync-fixtures") == 0

    differs = [page for page in _synced_pages()
               if (demo / page).read_bytes() != (DEMO.parent / page).read_bytes()]
    assert not differs, f"out of sync with demo-fixtures.json: {differs}"


def test_a_fixture_edit_reaches_every_page_it_names(tmp_path, monkeypatch) -> None:
    """Two pages, because one proves nothing about the sweep.

    A sync that wrote the first page and stopped — or that only ever knew about
    overview.html, the page the problem was noticed on — passes a one-page
    check. So both pages are edited and both are read back.
    """
    module, demo = _kit_copy(tmp_path)
    _edit_fixture(demo, "overview.html", "crash",
                  [{"time": "09-09 09:09:09", "sev": "crit", "text": "edited in the fixture"}])
    _edit_fixture(demo, "firmware.html", "mgmtUrl", "https://198.51.100.99")

    assert _run(module, monkeypatch, "--sync-fixtures") == 0

    assert _page_data(demo / "overview.html")["crash"][0]["text"] == "edited in the fixture"
    assert _page_data(demo / "firmware.html")["mgmtUrl"] == "https://198.51.100.99"


def test_the_sync_leaves_every_key_the_fixture_does_not_own(tmp_path, monkeypatch) -> None:
    """The measured half of a page is not the fixture's to write.

    overview.html carries seven builder-produced keys — CPU, clients, the
    channel counts — and none of them can be rebuilt without the bundle this
    path deliberately does not have. A sync that replaced the block instead of
    merging into it would erase them and there would be no way back.
    """
    module, demo = _kit_copy(tmp_path)
    before = _page_data(demo / "overview.html")
    _edit_fixture(demo, "overview.html", "crash", [])          # force a rewrite

    assert _run(module, monkeypatch, "--sync-fixtures") == 0

    after = _page_data(demo / "overview.html")
    fixture_owned = set(json.loads(
        (demo / "demo-fixtures.json").read_text(encoding="utf-8"))["overview.html"])
    assert set(after) == set(before)
    assert {k: v for k, v in after.items() if k not in fixture_owned} == \
           {k: v for k, v in before.items() if k not in fixture_owned}
    assert after["crash"] == []


@pytest.mark.parametrize("page", ["files.html", "firmware.html"])
def test_the_sync_and_a_full_regeneration_write_the_same_bytes(
        tmp_path, monkeypatch, page: str) -> None:
    """Same fixture, two paths, one result — checked where both can run.

    A sync that wrote the fixture's keys *differently* from the regeneration
    would swap one disagreement for another, and the swap would be invisible
    until someone finally had a bundle. These two pages are synthetic in full,
    so `--page` needs no bundle either and the comparison is available.
    """
    sync_module, synced = _kit_copy(tmp_path / "a")
    build_module, built = _kit_copy(tmp_path / "b")
    for demo in (synced, built):
        _edit_fixture(demo, page, "files", [{"name": "edited-in-the-fixture.bin"}])

    assert _run(sync_module, monkeypatch, "--sync-fixtures") == 0
    assert _run(build_module, monkeypatch, "--page", page) == 0

    assert (synced / page).read_bytes() == (built / page).read_bytes()
    assert _page_data(synced / page)["files"] == [{"name": "edited-in-the-fixture.bin"}]


def test_the_fixture_still_wins_over_a_builder_that_writes_the_same_key(
        tmp_path, monkeypatch) -> None:
    """Precedence, on a page that has a real builder — the case above cannot see.

    `main` applies the fixture after the builder, so the two paths agree on
    every key the fixture owns. Were the builder to win, a sync would write one
    value and the next regeneration another, and the page would flip between
    them depending on which command was run last. The builder is stubbed because
    the real one needs a capture; `--bundle` is only passed to get past the
    "built from a capture" check, and the stub never opens it.
    """
    module, demo = _kit_copy(tmp_path)
    monkeypatch.setitem(
        module.PAGE_BUILDERS, "overview.html",
        lambda bundle, anon, survey: {"crash": ["from the builder"], "cpu": "from the builder"},
    )

    assert _run(module, monkeypatch, "--page", "overview.html", "--bundle", str(demo)) == 0

    page = _page_data(demo / "overview.html")
    fixture = json.loads(
        (demo / "demo-fixtures.json").read_text(encoding="utf-8"))["overview.html"]
    assert page["crash"] == fixture["crash"], "the builder overwrote the fixture"
    assert page["cpu"] == "from the builder", "a key the fixture does not own"


@pytest.mark.parametrize("damage", ["unknown-page", "missing-file"])
def test_a_fixture_the_sync_cannot_honour_writes_nothing_at_all(
        tmp_path, monkeypatch, damage: str) -> None:
    """Every name is checked before the first page is written.

    Validating inside the loop leaves the pages ahead of the bad entry rewritten
    and the ones after it stale, then reports a failure — a half-synced kit,
    which is worse than either end of it. The edit below is what makes that
    visible: without a pending change to overview.html, an in-loop check would
    have "written" a page whose bytes happened not to move, and this test would
    pass over the bug it exists to catch.
    """
    module, demo = _kit_copy(tmp_path)
    _edit_fixture(demo, "overview.html", "crash", [])
    before = (demo / "overview.html").read_bytes()
    if damage == "unknown-page":
        _edit_fixture(demo, "overview.html", "crash", [])
        fixture = json.loads((demo / "demo-fixtures.json").read_text(encoding="utf-8"))
        fixture["not-a-page.html"] = {"whatever": 1}
        (demo / "demo-fixtures.json").write_text(json.dumps(fixture, indent=2), encoding="utf-8")
    else:
        (demo / "firmware.html").unlink()

    with pytest.raises(SystemExit) as caught:
        _run(module, monkeypatch, "--sync-fixtures")

    assert "demo-fixtures.json names" in str(caught.value)
    assert (demo / "overview.html").read_bytes() == before, "wrote a page, then failed"


@pytest.mark.parametrize("argv", [["--bundle", "/nonexistent"],
                                  ["--survey-bundle", "/nonexistent"],
                                  ["--page", "overview.html"]])
def test_the_sync_refuses_the_arguments_it_does_not_read(
        tmp_path, monkeypatch, argv: list[str]) -> None:
    """Accepting them would let the command misreport what it did.

    `--sync-fixtures --bundle <session>` looks like a regeneration and is not
    one: nothing measured would be rebuilt. `--page` is the same lie from the
    other side — the sweep covers every page whatever is named.
    """
    module, demo = _kit_copy(tmp_path)
    before = {page: (demo / page).read_bytes() for page in _synced_pages()}

    with pytest.raises(SystemExit) as caught:
        _run(module, monkeypatch, "--sync-fixtures", *argv)

    assert argv[0] in str(caught.value)
    assert {page: (demo / page).read_bytes() for page in _synced_pages()} == before
