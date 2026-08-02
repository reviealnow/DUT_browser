#!/usr/bin/env python3
"""Serve a fixed channel-recommendation payload so the survey UI can be worked
on without a DUT.

The Site Survey card is the hardest surface in the app to iterate on: it needs a
real AP, a quiet serial line, and a scan that takes minutes on a 29-VAP device.
This stands in for the backend and answers with one hand-built payload covering
the three cases that actually differ in the UI:

- **2.4GHz** — busiest, recommended and current are all different channels.
- **5GHz** — the recommended channel is unobserved, i.e. occupancy 0.
- **6GHz** — the current channel is already optimal, so nothing should be urged.

It also answers just enough of `/api/duts`, `/api/serial/ports` and
`POST /api/serial/open` for the connect flow to complete, because returning a
`log_path` is what makes `handleOpen` fire the on-connect prescan.

Everything it serves is static: no serial, no DUT, no database, stdlib only.
It is a development aid, not a test fixture — the survey parsers and the
recommendation maths have real coverage in `backend/tests/test_site_survey.py`
and `test_survey_cache.py`.

Run it, then point the Vite dev server at it:

    python3 tools/mock_survey_server.py            # 127.0.0.1:8000
    python3 tools/mock_survey_server.py --port 8011

The default is 8000 because that is what `frontend/vite.config.ts` proxies to.
That port is single-owner on the shared bench, so pass `--port` (and update the
proxy target) when the real backend already holds it.
"""

from __future__ import annotations

import argparse
import errno
import json
from http.server import BaseHTTPRequestHandler, HTTPServer


def neighbor(iface, bssid, ssid, band, ch, sig):
    return {
        "iface": iface, "bssid": bssid, "ssid": ssid, "band": band,
        "freq_mhz": None, "channel": ch, "signal_dbm": sig,
        "generation": "Wi-Fi 6", "security": "WPA2", "category": None, "pmf": None,
    }


NEIGHBORS = (
    [neighbor("wl0", f"aa:bb:cc:00:01:{i:02x}", f"Lab24-{i}", "2.4GHz", 1, -40 - i) for i in range(7)]
    + [neighbor("wl0", f"aa:bb:cc:00:06:{i:02x}", f"Office-{i}", "2.4GHz", 6, -55 - i) for i in range(3)]
    + [neighbor("wl0", "aa:bb:cc:00:03:01", "Guest", "2.4GHz", 3, -70)]
    + [neighbor("wl0", "aa:bb:cc:00:03:02", "Guest2", "2.4GHz", 3, -72)]
    + [neighbor("wl0", "aa:bb:cc:00:09:01", "Printer", "2.4GHz", 9, -60)]
    + [neighbor("wl1", f"dd:ee:ff:00:24:{i:02x}", f"Lab5-{i}", "5GHz", 36, -45 - i) for i in range(4)]
    + [neighbor("wl1", "dd:ee:ff:00:28:01", "Mesh-A", "5GHz", 40, -50)]
    + [neighbor("wl1", "dd:ee:ff:00:28:02", "Mesh-B", "5GHz", 40, -58)]
    + [neighbor("wl1", "dd:ee:ff:00:2c:01", "Corp5", "5GHz", 44, -66)]
    + [neighbor("wl2", "11:22:33:00:35:01", "Neo6-A", "6GHz", 53, -52)]
    + [neighbor("wl2", "11:22:33:00:35:02", "Neo6-B", "6GHz", 53, -57)]
    + [neighbor("wl2", "11:22:33:00:25:01", "Neo6-C", "6GHz", 37, -63)]
)

PAYLOAD = {
    "recommendations": [
        {
            "band": "2.4GHz", "iface": "wl0", "current_channel": 6, "recommended_channel": 11,
            "score": 1.2, "occupancy": {str(c): v for c, v in
                {1: 142.0, 2: 96.0, 3: 44.0, 4: 31.0, 5: 38.0, 6: 52.0, 7: 30.0,
                 8: 12.0, 9: 18.0, 10: 9.0, 11: 1.2, 12: 0.8, 13: 0.5}.items()},
            "reasoning": "ch 11 has the lowest adjacent-weighted occupancy", "caveat": None,
        },
        {
            "band": "5GHz", "iface": "wl1", "current_channel": 36, "recommended_channel": 149,
            "score": 0.0, "occupancy": {"36": 88.0, "40": 41.0, "44": 12.0, "149": 0.0},
            "reasoning": "ch 149 is unoccupied", "caveat": None,
        },
        {
            "band": "6GHz", "iface": "wl2", "current_channel": 37, "recommended_channel": 37,
            "score": 5.0, "occupancy": {"37": 5.0, "53": 30.0},
            "reasoning": "current channel already optimal", "caveat": None,
        },
    ],
    "neighbors": NEIGHBORS,
    "survey_vaps": [
        {"iface": "wl0", "ssid": "DUT-24", "band": "2.4GHz", "channel": 6, "mode": "AP"},
        {"iface": "wl1", "ssid": "DUT-5", "band": "5GHz", "channel": 36, "mode": "AP"},
        {"iface": "wl2", "ssid": "DUT-6", "band": "6GHz", "channel": 37, "mode": "AP"},
    ],
    "captured_at": "2026-07-02 10:00:00",
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api/wifi/channel-recommendation"):
            body = json.dumps(PAYLOAD).encode()
        elif self.path.startswith("/api/version"):
            body = json.dumps({"version": "mock"}).encode()
        elif self.path.startswith("/api/duts"):
            body = json.dumps({"duts": [{
                "id": "default", "label": "Mock DUT", "mode": "serial",
                "serial_open": True, "log_path": None, "removable": False,
            }]}).encode()
        elif self.path.startswith("/api/serial/ports"):
            body = json.dumps({"ports": [
                {"device": "/dev/cu.MOCK", "description": "Mock adapter"},
            ]}).encode()
        else:
            self.send_response(404)
            self.end_headers()
            return
        self._send(body)

    def do_POST(self):
        # Enough of the serial-open flow to exercise the on-connect prescan:
        # returning a log_path makes handleOpen succeed → onSerialOpened fires.
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length:
            self.rfile.read(length)
        if self.path.startswith("/api/serial/open"):
            self._send(json.dumps({"log_path": "logs/dut-session-mock.log"}).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def _send(self, body):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="bind port (default: 8000)")
    args = parser.parse_args()

    try:
        server = HTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            # The usual cause on this bench: the real backend already owns 8000.
            print(f"port {args.port} is already in use — pass --port to pick another")
            return 1
        raise

    print(f"mock survey backend on http://{args.host}:{args.port} (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
