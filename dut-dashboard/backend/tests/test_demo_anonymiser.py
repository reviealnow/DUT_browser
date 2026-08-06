"""The two properties the demo kit's identifier replacement has to hold.

A neighbour scan sweeps up the SSIDs and BSSIDs of everyone in radio range, so
`dut-dashboard/demo/` may never ship them as captured. Both properties below
were defects found in review before they were tests:

* the mapping depended on the order rows happened to be walked, so the same
  bundle could produce two different pages;
* the IP space was one /24 with no collision handling, so two real addresses
  could land on one fake — which understates how many distinct devices were
  seen, and that count is the measurement the page exists to show.
"""

from __future__ import annotations

import importlib.util
import random
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
    # 02: is the locally-administered bit — never a vendor OUI.
    assert all(v.startswith("02:") for v in macs.values())
    # RFC 5737 documentation ranges only.
    assert all(v.split(".")[0:3] in ([  "192", "0", "2"], ["198", "51", "100"], ["203", "0", "113"])
               for v in ips.values())


def test_running_out_of_aliases_fails_loudly(anon_module) -> None:
    # Silently reusing a name would misreport how crowded the air is, so the
    # generator must stop rather than degrade.
    anon = anon_module.Anonymiser()
    with pytest.raises(SystemExit, match="exhausted"):
        anon.prepare(vendors=[f"vendor-{i}" for i in range(len(anon_module.VENDORS) + 1)])
