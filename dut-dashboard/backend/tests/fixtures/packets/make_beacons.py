"""Build a radiotap pcap of synthetic beacons covering the Packet Monitor rules.

The committed fixtures next to this file are what a real tshark made of it:

* ``beacons_tshark-<version>.tsv`` -- ``tshark -n -r beacons.pcap
  -Y <beacon filter> -T fields -E header=y -E separator=/t -E occurrence=a
  -E aggregator=<US>`` with one ``-e`` per field the beacon Test Modes resolve;
* ``field_types_tshark-<version>.json`` -- every Test Mode field's FT_* type
  from ``tshark -G fields``.

Regenerate both after adding a case, from ``dut-dashboard/backend``:

    python3 tests/fixtures/packets/make_beacons.py <path to tshark>

The pcap itself is not committed: it is reproducible from this script.
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path
import struct

OUI = b"\x00\x0f\xac"


def ie(eid, body):
    return bytes([eid, len(body)]) + body


def rsn(group, pairwise, akms, mfpr, mfpc):
    caps = (0x40 if mfpr else 0) | (0x80 if mfpc else 0)
    body = struct.pack("<H", 1) + OUI + bytes([group])
    body += struct.pack("<H", len(pairwise)) + b"".join(OUI + bytes([p]) for p in pairwise)
    body += struct.pack("<H", len(akms)) + b"".join(OUI + bytes([a]) for a in akms)
    body += struct.pack("<H", caps)
    return ie(48, body)


def beacon(bssid, ssid, freq, channel, rsn_ie=b"", interval=100, dtim=1, privacy=True):
    rt = struct.pack("<BBHI", 0, 0, 13, (1 << 3) | (1 << 5))
    rt += struct.pack("<HHb", freq, 0x00A0 if freq < 3000 else 0x0140, -44)
    mac = bytes(int(x, 16) for x in bssid.split(":"))
    hdr = b"\x80\x00\x00\x00" + b"\xff" * 6 + mac + mac + b"\x00\x00"
    cap = 0x0001 | 0x0400 | 0x0020 | (0x0010 if privacy else 0)
    body = b"\x00" * 8 + struct.pack("<HH", interval, cap)
    body += ie(0, ssid)
    body += ie(1, bytes([0x82, 0x84, 0x8B, 0x96]))
    if freq < 3000:
        body += ie(3, bytes([channel]))
    body += ie(5, bytes([0, dtim, 0, 0]))
    body += ie(7, b"US " + bytes([1, 11, 30]))
    body += rsn_ie
    return rt + hdr + body


CASES = [
    # bssid, ssid, freq, chan, rsn, interval, dtim, privacy
    ("00:03:7f:00:00:01", b"open",          2437, 6, b"", 100, 1, False),
    ("00:03:7f:00:00:02", b"wpa2psk",       2437, 6, rsn(4, [4], [2], 0, 0), 100, 1, True),
    ("00:03:7f:00:00:03", b"wpa3sae_ok",    2437, 6, rsn(4, [4], [8], 1, 1), 100, 1, True),
    ("00:03:7f:00:00:04", b"wpa3sae_nomfpr", 2437, 6, rsn(4, [4], [8], 0, 1), 100, 1, True),
    ("00:03:7f:00:00:05", b"transition",    2437, 6, rsn(4, [4], [2, 8], 0, 1), 100, 1, True),
    ("00:03:7f:00:00:06", b"trans_nomfpc",  2437, 6, rsn(4, [4], [2, 8], 0, 0), 100, 1, True),
    ("00:03:7f:00:00:07", b"sae_tkip",      2437, 6, rsn(2, [2, 4], [8], 1, 1), 100, 1, True),
    ("00:03:7f:00:00:08", b"owe_nomfpr",    2437, 6, rsn(4, [4], [18], 0, 1), 100, 1, True),
    ("00:03:7f:00:00:09", b"",              2437, 6, rsn(4, [4], [2], 0, 0), 100, 1, True),
    ("00:03:7f:00:00:0a", b"\x00\x00\x00\x00", 2437, 6, rsn(4, [4], [2], 0, 0), 100, 1, True),
    ("00:03:7f:00:00:0b", b"oddinterval",   2437, 6, rsn(4, [4], [8], 1, 1), 224, 24, True),
    ("00:03:7f:00:00:0c", b"6g_psk",        5975, 5, rsn(4, [4], [2], 0, 0), 100, 1, True),
    ("00:03:7f:00:00:0d", b"6g_sae_ok",     5975, 5, rsn(4, [4], [24], 1, 1), 100, 1, True),
    ("00:03:7f:00:00:0e", b"AP7_\xe4\xb8\xad\xe6\x96\x87 sp\tc", 5180, 36, rsn(4, [4], [8], 1, 1), 100, 1, True),
    ("00:03:7f:00:00:10", b"owe_ok",        5180, 36, rsn(4, [4], [18], 1, 1), 100, 1, True),
    ("00:03:7f:00:00:11", b"6g_enterprise", 5975, 5, rsn(9, [9], [5], 1, 1), 100, 1, True),
    ("00:03:7f:00:00:12", b"comma,ssid",    2437, 6, rsn(4, [4], [2], 0, 0), 100, 1, True),
    ("00:03:7f:00:00:13", b"mfpr_nomfpc",   2437, 6, rsn(4, [4], [2], 1, 0), 100, 1, True),
    # same BSSID twice, RSN changes between beacons
    ("00:03:7f:00:00:0f", b"flappy",        2437, 6, rsn(4, [4], [8], 1, 1), 100, 1, True),
    ("00:03:7f:00:00:0f", b"flappy",        2437, 6, rsn(4, [4], [2, 8], 0, 1), 100, 1, True),
]


def write_pcap(path):
    out = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 127)
    for i, c in enumerate(CASES):
        pkt = beacon(*c)
        out += struct.pack("<IIII", 1_700_000_000 + i, 0, len(pkt), len(pkt)) + pkt
    Path(path).write_bytes(out)


def main(tshark):
    sys.path.insert(0, ".")  # run from dut-dashboard/backend
    from app.services import packet_decode, packet_modes

    here = Path(__file__).parent
    version = subprocess.run([tshark, "--version"], capture_output=True, text=True,
                             check=True).stdout.split()[2]
    available = {}
    for line in subprocess.run([tshark, "-G", "fields"], capture_output=True, text=True,
                               check=True).stdout.splitlines():
        parts = line.split("\t")
        if parts[0] == "F":
            available[parts[2]] = parts[3]
    wanted = sorted({f for m in packet_modes.MODES for c in m.columns for f in c.fields})
    (here / f"field_types_tshark-{version}.json").write_text(
        json.dumps({f: available[f] for f in wanted if f in available}, indent=1, sort_keys=True) + "\n")

    fields = sorted({f for m in packet_modes.MODES if m.display_filter == packet_modes.BEACON_FILTER
                     for f in packet_modes.extraction_fields(packet_modes.resolve_columns(m, available))})
    with tempfile.TemporaryDirectory() as tmp:
        pcap = Path(tmp) / "beacons.pcap"
        write_pcap(pcap)
        args = [tshark, "-n", "-r", str(pcap), "-Y", packet_modes.BEACON_FILTER, "-T", "fields",
                "-E", "header=y", "-E", "separator=/t", "-E", "occurrence=a",
                "-E", f"aggregator={packet_decode.AGGREGATOR}"]
        for name in fields:
            args += ["-e", name]
        tsv = subprocess.run(args, capture_output=True, text=True, check=True).stdout
    (here / f"beacons_tshark-{version}.tsv").write_text(tsv)
    print(f"tshark {version}: {len(tsv.splitlines()) - 1} frames, {len(fields)} fields")


if __name__ == "__main__":
    main(sys.argv[1])
