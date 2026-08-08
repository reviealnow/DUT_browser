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
    # Not in the review's list: one column named `ssid;bssid` holding
    # `OlderSecret;aa:bb:…`, so every value merges and none is collected.
    ("a file that is not comma-separated",
     "ssid;bssid\nOlderSecret;aa:bb:cc:dd:ee:ff\n", "not comma-separated"),
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
