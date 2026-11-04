#!/usr/bin/env python3
"""Scan from a Host beside the DUT and report what an admin needs to compare.

Packet Monitor's managed-mode source, runnable from a bench machine before the
API and UI exist (docs/design-packet-monitor.md). It reads ``iw`` scan text --
from a file, or by ssh'ing to the Host -- and runs it through the same parser
and rules the dashboard uses (``services/wifi_survey._parse_iw_scan``, mapped
by ``services/scan_rows.py`` onto ``services/packet_rules.py``):

* legacy security (WEP, WPA1, TKIP) and open networks;
* the DUT's own beacons against its SKU: ``--domain N`` (an AP6's
  ``board_config get domain``, see ``services/dut_regdomain.py``) or a single
  ``--expected-country``;
* per-channel BSS counts, and the neighbours on the DUT's channels by signal.

    python3 tools/host_scan_report.py --file scan.txt --dut-bssid 02:11:22:33:44:55
    python3 tools/host_scan_report.py --ssh pi@192.0.2.10 \\
        --dut-bssid 02:11:22:33:44:55 --domain 2

**Read-only on the Host.** ``--ssh`` runs ``iw dev <iface> scan dump``, which
needs no root and returns the kernel's cached results; their age is printed.
``--sudo`` runs a fresh ``sudo -n iw dev <iface> scan`` instead, which needs a
passwordless sudoers rule for ``iw`` on the Host -- the tool never sends a
password. Key authentication only (``BatchMode=yes``). Nothing touches the DUT.

A DUT usually derives one BSSID per VAP by changing the first octet (setting
the locally-administered bit), so ``--dut-bssid`` matches any BSSID that
differs from it only there. Pass one per radio.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.services import dut_regdomain, packet_rules, scan_rows  # noqa: E402
from app.services.packet_decode import AKM_SUITES, band_of  # noqa: E402
from app.services.wifi_survey import _parse_iw_scan  # noqa: E402

RULES = ("wpa3_requires_mfpr", "sae_requires_mfpc", "mfpr_without_mfpc",
         "no_legacy_cipher_with_wpa3", "six_ghz_security", "legacy_security",
         "open_network", "country_mismatch", "hidden_ssid", "beacon_timing")
SSH_TIMEOUT_SEC = 30
#: Interface names reach a remote shell; keep them to what Linux allows anyway.
IFACE_RE = re.compile(r"[A-Za-z0-9_.-]{1,15}")
#: No leading "-": ssh would read the target as an option (-oProxyCommand=...).
_TARGET_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*@[A-Za-z0-9_.:-]+")
_MAC_RE = re.compile(r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}")


def fetch_over_ssh(target: str, iface: str, sudo: bool) -> str:
    if not _TARGET_RE.fullmatch(target):
        raise SystemExit(f"not a user@host target: {target!r}")
    if not IFACE_RE.fullmatch(iface):
        raise SystemExit(f"not an interface name: {iface!r}")
    remote = f"sudo -n iw dev {iface} scan" if sudo else f"iw dev {iface} scan dump"
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", target, remote]
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=SSH_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        raise SystemExit(f"ssh {target}: no answer within {SSH_TIMEOUT_SEC} s")
    if done.returncode != 0:
        raise SystemExit(f"ssh {target} '{remote}' failed ({done.returncode}): "
                         f"{done.stderr.strip()}")
    return done.stdout


def dut_matcher(bssids: list[str]):
    tails = set()
    for b in bssids:
        b = b.lower()
        if not _MAC_RE.fullmatch(b):
            raise SystemExit(f"not a BSSID: {b!r}")
        tails.add(b[3:])
    return lambda bssid: bool(bssid) and bssid.lower()[3:] in tails


def security_label(row: dict) -> str:
    if row.get("akm"):
        return " / ".join(AKM_SUITES.get(a, f"AKM {a}") for a in row["akm"])
    if row.get("wpa_version") is not None:
        return "WPA1"
    if row.get("privacy") is False:
        return "Open"
    # No WPA key: this iw shows no WPA element (scan_rows), so Privacy alone
    # may be either.
    return "WEP" if "wpa_version" in row else "WEP or WPA1"


def build_report(rows: list[dict], is_dut,
                 expected: "str | dut_regdomain.Domain | None") -> dict:
    """``expected`` is the SKU: one country code, a domain, or None (unchecked)."""
    dut_bssids = [r["bssid"] for r in rows if is_dut(r["bssid"])]
    for r in rows:
        if not is_dut(r["bssid"]):
            continue
        if isinstance(expected, dut_regdomain.Domain):
            r["expected_countries"] = expected.countries
            r["expected_region"] = expected.label
        elif expected:
            r["expected_country"] = expected
    findings = packet_rules.evaluate(rows, RULES, group_by="bssid")
    by_bssid = {r["bssid"]: r for r in rows}
    ages = [r["last_seen_ms"] for r in rows if r.get("last_seen_ms") is not None]
    return {
        "bss_count": len(rows),
        "dut_bssids": dut_bssids,
        "oldest_result_ms": max(ages) if ages else None,
        "findings": [
            {**f.to_dict(), "ssid": by_bssid.get(f.group, {}).get("ssid"),
             "dut": is_dut(f.group)}
            for f in findings
        ],
        "occupancy": packet_rules.channel_occupancy(rows, dut_bssids),
        "dut": [
            {k: r.get(k) for k in ("bssid", "ssid", "channel", "freq", "signal", "country")}
            | {"band": band_of(r.get("freq")), "security": security_label(r)}
            for r in rows if is_dut(r["bssid"])
        ],
    }


def print_report(report: dict) -> None:
    print(f"{report['bss_count']} BSS in scan; DUT BSSes matched: {len(report['dut_bssids'])}")
    if report["oldest_result_ms"] is not None:
        print(f"oldest cached result: {report['oldest_result_ms'] / 1000:.1f} s")
    print("\n== DUT ==")
    for d in report["dut"] or [{"bssid": "(none matched: check --dut-bssid)"}]:
        print(f"  {d.get('bssid')}  {d.get('band') or ''}G ch{d.get('channel')}  "
              f"{d.get('signal')} dBm  country={d.get('country')}  "
              f"{d.get('security', '')}  {d.get('ssid')!r}")
    print("\n== Findings ==")
    for f in report["findings"] or [{"severity": "-", "rule": "none"}]:
        tag = " [DUT]" if f.get("dut") else ""
        print(f"  [{f['severity']}] {f['rule']}{tag}: {f.get('message', '')}")
        if f.get("group"):
            print(f"        {f['group']} {f.get('ssid')!r}")
    print("\n== Channels (band ch: BSS count, strongest neighbour) ==")
    for c in report["occupancy"]["channels"]:
        mark = "  <- DUT" if c["dut_present"] else ""
        best = c["strongest_neighbour_dbm"]
        print(f"  {c['band']:>3}G ch{c['channel']:>3}: {c['bss_count']:>2} BSS, "
              f"strongest neighbour {best if best is not None else '-'} dBm{mark}")
    print("\n== Neighbours on the DUT's channels ==")
    for n in report["occupancy"]["dut_channel_neighbours"]:
        print(f"  {n['band']:>3}G ch{n['channel']:>3}  {n['signal']} dBm  {n['bssid']}  {n['ssid']!r}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--file", type=Path, help="saved `iw dev <iface> scan` output")
    src.add_argument("--ssh", metavar="USER@HOST", help="scan on this Host over ssh (key auth)")
    ap.add_argument("--iface", default="wlan0")
    ap.add_argument("--sudo", action="store_true", help="fresh scan via `sudo -n iw ... scan`")
    ap.add_argument("--dut-bssid", action="append", default=[], help="one of the DUT's BSSIDs (repeat per radio)")
    sku = ap.add_mutually_exclusive_group()
    sku.add_argument("--domain", type=int, choices=sorted(dut_regdomain.DOMAINS),
                     help="the DUT's SKU domain (AP6: `board_config get domain`)")
    sku.add_argument("--expected-country", help="the DUT's SKU country, e.g. US")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    args = ap.parse_args(argv)

    text = (args.file.read_text(encoding="utf-8", errors="replace") if args.file
            else fetch_over_ssh(args.ssh, args.iface, args.sudo))
    rows = scan_rows.rule_rows(_parse_iw_scan(text))
    if not rows:
        print("no BSS in the scan output (is the interface up? `ip link set wlan0 up`)",
              file=sys.stderr)
        return 2
    expected = (dut_regdomain.DOMAINS[args.domain] if args.domain is not None
                else args.expected_country)
    report = build_report(rows, dut_matcher(args.dut_bssid), expected)
    if args.json:
        print(json.dumps(report, indent=1, ensure_ascii=False))
    else:
        print_report(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
