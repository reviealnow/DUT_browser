from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.dut.registry as registry_mod
from app.dut.registry import (
    DEFAULT_DUT_ID,
    DEFAULT_DUT_LABEL,
    MAX_LABEL_LEN,
    DutContext,
    DutRegistry,
    build_default_registry,
)


class _StubWsManager:
    """Records the events the per-DUT on_event closure broadcasts."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def emit_from_thread(self, event: dict) -> None:
        self.events.append(event)


class DutRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self._tmp = Path(self._dir.name)
        # Keep all snapshot files + the persisted DUTs list inside the tempdir.
        self._duts_file = self._tmp / "duts.json"
        patches = [
            mock.patch.object(registry_mod, "DUTS_FILE", self._duts_file),
            mock.patch.object(registry_mod, "snapshot_file_for", lambda d: self._tmp / f"snap-{d}.jsonl"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self._loop = asyncio.new_event_loop()
        self.addCleanup(self._loop.close)
        self.ws = _StubWsManager()

    def _registry(self) -> DutRegistry:
        return DutRegistry(ws_manager=self.ws, loop=self._loop)

    def test_create_and_get_returns_wired_context(self) -> None:
        reg = self._registry()
        ctx = reg.create_dut(DEFAULT_DUT_ID, label="Default")
        self.assertIsInstance(ctx, DutContext)
        self.assertEqual(ctx.dut_id, DEFAULT_DUT_ID)
        self.assertEqual(ctx.label, "Default")
        for attr in ("parser", "serial_worker", "snapshot_store", "console_buffer", "terminal_manager"):
            self.assertIsNotNone(getattr(ctx, attr))
        self.assertIs(reg.get(DEFAULT_DUT_ID), ctx)
        self.assertEqual(reg.ids(), [DEFAULT_DUT_ID])

    def test_unknown_dut_raises(self) -> None:
        reg = self._registry()
        reg.create_dut(DEFAULT_DUT_ID)
        with self.assertRaises(KeyError):
            reg.get("bogus")

    def test_on_event_tags_dut_id_and_routes_to_own_buffers(self) -> None:
        reg = self._registry()
        ctx = reg.create_dut("duta")
        ctx.parser.on_event({"type": "console_line", "text": "hello"})
        self.assertEqual(ctx.console_buffer.recent(10), ["hello"])
        self.assertTrue(self.ws.events)
        self.assertEqual(self.ws.events[-1]["dut_id"], "duta")

    def test_register_lists_and_removes(self) -> None:
        reg = self._registry()
        reg.create_dut(DEFAULT_DUT_ID, label="Default")
        reg.register_dut("lab2", label="Replay DUT")
        ids = {d["id"]: d for d in reg.describe()}
        self.assertEqual(set(ids), {DEFAULT_DUT_ID, "lab2"})
        self.assertEqual(ids["lab2"]["label"], "Replay DUT")
        self.assertTrue(ids["lab2"]["removable"])
        self.assertFalse(ids[DEFAULT_DUT_ID]["removable"])
        reg.remove_dut("lab2")
        self.assertEqual(reg.ids(), [DEFAULT_DUT_ID])

    def test_register_rejects_duplicate(self) -> None:
        reg = self._registry()
        reg.register_dut("lab2")
        with self.assertRaises(KeyError):
            reg.register_dut("lab2")

    def test_register_rejects_bad_id(self) -> None:
        reg = self._registry()
        for bad in ("Lab2", "ab/cd", "-x", "", "a" * 40):
            with self.assertRaises(ValueError):
                reg.register_dut(bad)

    def test_cannot_remove_default(self) -> None:
        reg = self._registry()
        reg.create_dut(DEFAULT_DUT_ID)
        with self.assertRaises(ValueError):
            reg.remove_dut(DEFAULT_DUT_ID)

    def test_persistence_round_trip(self) -> None:
        reg = self._registry()
        reg.create_dut(DEFAULT_DUT_ID, label="Default")
        reg.register_dut("lab2", label="Replay DUT")
        # A fresh registry that loads the persisted file gets lab2 back (not default).
        reg2 = self._registry()
        reg2.create_dut(DEFAULT_DUT_ID, label="Default")
        reg2.load_persisted()
        self.assertEqual(set(reg2.ids()), {DEFAULT_DUT_ID, "lab2"})
        self.assertEqual(reg2.get("lab2").label, "Replay DUT")

    # --- Phase 67: remembered serial params ------------------------------------

    def test_record_serial_params_exposed_in_describe(self) -> None:
        reg = self._registry()
        reg.create_dut(DEFAULT_DUT_ID, label="Default")
        self.assertIsNone(reg.describe()[0]["last_serial"])
        reg.record_serial_params(DEFAULT_DUT_ID, "/dev/cu.usbserial", 115200)
        self.assertEqual(
            reg.describe()[0]["last_serial"], {"port": "/dev/cu.usbserial", "baudrate": 115200}
        )

    def test_record_serial_params_unknown_dut_is_noop(self) -> None:
        reg = self._registry()
        reg.create_dut(DEFAULT_DUT_ID)
        reg.record_serial_params("ghost", "/dev/cu.x", 115200)  # must not raise
        self.assertFalse(self._duts_file.exists())

    def test_record_serial_params_rejects_malformed(self) -> None:
        reg = self._registry()
        reg.create_dut(DEFAULT_DUT_ID)
        for port, baud in (("", 115200), ("/dev/x", 0), ("/dev/x", -1)):
            reg.record_serial_params(DEFAULT_DUT_ID, port, baud)
        self.assertIsNone(reg.get(DEFAULT_DUT_ID).last_serial)

    def test_last_serial_survives_restart_default_and_registered(self) -> None:
        reg = self._registry()
        reg.create_dut(DEFAULT_DUT_ID, label="Default")
        reg.register_dut("lab2", label="Replay DUT")
        reg.record_serial_params(DEFAULT_DUT_ID, "/dev/cu.default", 115200)
        reg.record_serial_params("lab2", "/dev/cu.lab2", 9600)
        # Fresh registry: default is re-created by build, lab2 restored from file;
        # both get their remembered params back.
        reg2 = self._registry()
        reg2.create_dut(DEFAULT_DUT_ID, label="Default")
        reg2.load_persisted()
        self.assertEqual(reg2.get(DEFAULT_DUT_ID).last_serial, {"port": "/dev/cu.default", "baudrate": 115200})
        self.assertEqual(reg2.get("lab2").last_serial, {"port": "/dev/cu.lab2", "baudrate": 9600})

    def test_legacy_file_without_last_serial_loads(self) -> None:
        # A duts.json written by a pre-P67 build has no last_serial key.
        self._duts_file.write_text('[{"id": "lab2", "label": "Legacy DUT"}]', encoding="utf-8")
        reg = self._registry()
        reg.create_dut(DEFAULT_DUT_ID, label="Default")
        reg.load_persisted()
        self.assertEqual(reg.get("lab2").label, "Legacy DUT")
        self.assertIsNone(reg.get("lab2").last_serial)

    def test_malformed_last_serial_in_file_is_ignored(self) -> None:
        self._duts_file.write_text(
            '[{"id": "lab2", "label": "L", "last_serial": {"port": "", "baudrate": "fast"}}]',
            encoding="utf-8",
        )
        reg = self._registry()
        reg.create_dut(DEFAULT_DUT_ID, label="Default")
        reg.load_persisted()
        self.assertIsNone(reg.get("lab2").last_serial)


class SerialOpenRecordingTests(unittest.TestCase):
    """The /api/serial/open handler records params on serial-mode opens only."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        tmp = Path(self._dir.name)
        patches = [
            mock.patch.object(registry_mod, "DUTS_FILE", tmp / "duts.json"),
            mock.patch.object(registry_mod, "snapshot_file_for", lambda d: tmp / f"snap-{d}.jsonl"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self._loop = asyncio.new_event_loop()
        self.addCleanup(self._loop.close)
        self.reg = DutRegistry(ws_manager=_StubWsManager(), loop=self._loop)
        ctx = self.reg.create_dut(DEFAULT_DUT_ID, label="Default")
        # Stub the worker open so no real serial/replay file is needed.
        ctx.serial_worker.open = lambda **kw: None  # type: ignore[assignment]
        self.request = mock.Mock()
        self.request.app.state.dut_registry = self.reg

    def _open(self, **kw):
        from app.api.serial_api import SerialOpenRequest, open_serial

        body = SerialOpenRequest(**kw)
        return open_serial(body, self.request, dut=DEFAULT_DUT_ID)

    def test_serial_open_records_params(self) -> None:
        self._open(mode="serial", port="/dev/cu.usbserial", baudrate=115200)
        self.assertEqual(
            self.reg.get(DEFAULT_DUT_ID).last_serial,
            {"port": "/dev/cu.usbserial", "baudrate": 115200},
        )

    def test_replay_open_does_not_record(self) -> None:
        self._open(mode="replay", replay_path="/tmp/x.log")
        self.assertIsNone(self.reg.get(DEFAULT_DUT_ID).last_serial)

    def test_serial_open_without_port_does_not_record(self) -> None:
        self._open(mode="serial", port="", baudrate=115200)
        self.assertIsNone(self.reg.get(DEFAULT_DUT_ID).last_serial)

    def test_a_replay_open_revokes_the_backhaul_reading_from_the_device(self) -> None:
        """Replay records no port, so nothing else here notices that what is
        behind this DUT id has stopped being a device at all. Driven through the
        endpoint: the registry method only protects anybody if this path calls
        it, and a test of the method alone stays green while the call goes."""
        ctx = self.reg.get(DEFAULT_DUT_ID)
        self.reg.record_serial_params(DEFAULT_DUT_ID, "/dev/cu.bench", 115200)
        ctx.backhaul_role = "node"
        ctx.backhaul_uplink = {"iface": "ath15", "rssi": -37}
        ctx.backhaul_captured = True
        ctx.backhaul_console = registry_mod.console_token(ctx, "serial")

        self._open(mode="replay", replay_path="/tmp/x.log")

        published = self.reg.describe()[0]["backhaul"]
        self.assertIsNone(published["role"], "a live reading published over a replay")
        self.assertIsNone(published["uplink"])
        self.assertFalse(published["captured"])

    def test_a_serial_reopen_on_the_same_port_keeps_the_reading(self) -> None:
        """The revocation must cost a capture only when the console changed."""
        ctx = self.reg.get(DEFAULT_DUT_ID)
        self.reg.record_serial_params(DEFAULT_DUT_ID, "/dev/cu.bench", 115200)
        ctx.backhaul_role = "node"
        ctx.backhaul_captured = True
        ctx.backhaul_console = registry_mod.console_token(ctx, "serial")

        self._open(mode="serial", port="/dev/cu.bench", baudrate=115200)

        self.assertEqual(self.reg.describe()[0]["backhaul"]["role"], "node")


if __name__ == "__main__":
    unittest.main()


class RenameDutTests(DutRegistryTests):
    """Renaming, and the three ways it used to be impossible.

    Before this existed the built-in DUT could not be renamed by ANY runtime
    means: POST 409s on an existing id, DELETE refuses the default, and a label
    in duts.json was ignored on load for a DUT that already existed. The last
    one is the trap -- a rename that does not survive a restart looks like it
    worked, right up until the next deploy.
    """

    def _booted(self) -> DutRegistry:
        """A registry built the way main.on_startup builds it: the default DUT
        created with its hard-coded label, THEN the persisted file merged in."""
        return build_default_registry(ws_manager=self.ws, loop=self._loop)

    def test_renames_the_built_in_dut_that_nothing_else_could_touch(self) -> None:
        reg = self._booted()
        self.assertEqual(reg.get(DEFAULT_DUT_ID).label, DEFAULT_DUT_LABEL)
        reg.rename_dut(DEFAULT_DUT_ID, "AP6_420E")
        self.assertEqual(reg.get(DEFAULT_DUT_ID).label, "AP6_420E")
        [described] = [d for d in reg.describe() if d["id"] == DEFAULT_DUT_ID]
        self.assertEqual(described["label"], "AP6_420E")

    def test_a_rename_survives_a_restart(self) -> None:
        """The one that matters. `_save_locked` skipped a bare default entry and
        `load_persisted` ignored a saved label for an id that already existed,
        so a rename evaporated on the next boot with nothing to show for it."""
        self._booted().rename_dut(DEFAULT_DUT_ID, "AP6_420E")
        self.assertEqual(self._booted().get(DEFAULT_DUT_ID).label, "AP6_420E")

    def test_the_id_never_moves_so_history_is_not_orphaned(self) -> None:
        """Renaming by remove-and-re-add would change nothing visible and lose
        that DUT's snapshot history, which is keyed on the id."""
        reg = self._booted()
        reg.rename_dut(DEFAULT_DUT_ID, "AP6_420E")
        self.assertEqual(reg.ids(), [DEFAULT_DUT_ID])
        self.assertEqual(reg.get(DEFAULT_DUT_ID).dut_id, DEFAULT_DUT_ID)

    def test_an_untouched_default_still_writes_no_bare_entry(self) -> None:
        """The persistence condition gained a clause; it must not have gained a
        reason to write an entry that a reload would try to re-create."""
        reg = self._booted()
        reg.rename_dut(DEFAULT_DUT_ID, DEFAULT_DUT_LABEL)  # renamed to itself
        saved = self._duts_file.read_text() if self._duts_file.exists() else "[]"
        self.assertNotIn(DEFAULT_DUT_ID, saved)

    def test_renaming_an_unknown_dut_is_a_key_error(self) -> None:
        with self.assertRaises(KeyError):
            self._booted().rename_dut("nosuch", "X")

    def test_refuses_a_label_that_is_not_a_display_name(self) -> None:
        reg = self._booted()
        for bad in ("", "   ", "A" * (MAX_LABEL_LEN + 1), "\n", "\x00", 7, None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    reg.rename_dut(DEFAULT_DUT_ID, bad)  # type: ignore[arg-type]
        self.assertEqual(reg.get(DEFAULT_DUT_ID).label, DEFAULT_DUT_LABEL)

    def test_keeps_the_spaces_and_case_a_display_name_needs(self) -> None:
        reg = self._booted()
        reg.rename_dut(DEFAULT_DUT_ID, "  Lab 2 · AP6_420E  ")
        self.assertEqual(reg.get(DEFAULT_DUT_ID).label, "Lab 2 · AP6_420E")

    def test_a_hand_edited_file_gets_the_same_cleaning_as_the_api(self) -> None:
        """The load path must not be a way around the rename path's rules. A
        control character in a label reaches a log filename and the fleet UI.

        The contract is strip-then-judge, not reject-outright: control
        characters are dropped and what is left is used, so this asserts what
        survives -- both for a DUT that already exists (the merge branch) and
        for one being created from the file (the create branch). A label that
        cleans away to nothing is refused, which the empty case covers.
        """
        self._duts_file.write_text(
            '[{"id": "default", "label": "bad\\u0000name"},'
            ' {"id": "lab2", "label": "also\\u0000bad"},'
            ' {"id": "blank", "label": "\\u0000\\u0000"}]'
        )
        reg = self._booted()
        self.assertEqual(reg.get(DEFAULT_DUT_ID).label, "badname")
        self.assertEqual(reg.get("lab2").label, "alsobad")
        for dut_id in (DEFAULT_DUT_ID, "lab2", "blank"):
            self.assertNotIn("\x00", reg.get(dut_id).label)
        # Nothing printable left => no label at all, and create_dut falls back
        # to the id rather than storing an empty display name.
        self.assertEqual(reg.get("blank").label, "blank")


class DutModelDetectionTests(DutRegistryTests):
    """The model is learned from console output, stored, and survives a restart.

    It is not decoration: it decides how many VAPs sit in each band, and so
    which band an athN belongs to when the output states no frequency. Getting
    it from the prompt costs no serial time, which is the point -- the
    connect-time capture races sysMon for the line and loses.
    """

    def _booted(self) -> DutRegistry:
        return build_default_registry(ws_manager=self.ws, loop=self._loop)

    def test_learns_the_model_from_a_console_line(self) -> None:
        reg = self._booted()
        ctx = reg.get(DEFAULT_DUT_ID)
        self.assertIsNone(ctx.model)
        ctx.parser.on_event({"type": "console_line", "text": "AP6_420E#"})
        self.assertEqual(ctx.model, "AP6_420E")

    def test_learns_it_from_a_batched_line_too(self) -> None:
        """Console lines arrive batched as often as singly; a model seen only in
        the batch path would be missed on a busy console."""
        reg = self._booted()
        ctx = reg.get(DEFAULT_DUT_ID)
        ctx.parser.on_event(
            {"type": "console_line_batch", "lines": ["ath0: link up", "AP6_840E#"]}
        )
        self.assertEqual(ctx.model, "AP6_840E")

    def test_the_model_survives_a_restart(self) -> None:
        self._booted().get(DEFAULT_DUT_ID).parser.on_event(
            {"type": "console_line", "text": "AP6_420E#"}
        )
        self.assertEqual(self._booted().get(DEFAULT_DUT_ID).model, "AP6_420E")

    def test_ordinary_console_noise_leaves_it_unset(self) -> None:
        reg = self._booted()
        ctx = reg.get(DEFAULT_DUT_ID)
        for line in ("BusyBox v1.31.1", "cmd>", "", "wlanconfig ath13"):
            ctx.parser.on_event({"type": "console_line", "text": line})
        self.assertIsNone(ctx.model)

    def test_a_later_blank_prompt_does_not_clear_a_known_model(self) -> None:
        """A bootloader prompt says nothing about the hardware. A band mapping
        that flickered back to the default mid-session would be worse than a
        stale one, because nothing on screen would say it had moved."""
        reg = self._booted()
        ctx = reg.get(DEFAULT_DUT_ID)
        ctx.parser.on_event({"type": "console_line", "text": "AP6_420E#"})
        for line in ("cmd>", "", "reboot"):
            ctx.parser.on_event({"type": "console_line", "text": line})
        self.assertEqual(ctx.model, "AP6_420E")

    def test_describe_publishes_the_model_and_the_mapping_it_implies(self) -> None:
        """A caller reading interfaces out of this API cannot work out which
        numbering applies unless the API says so."""
        reg = self._booted()
        [before] = [d for d in reg.describe() if d["id"] == DEFAULT_DUT_ID]
        self.assertIsNone(before["model"])
        self.assertEqual(before["vaps_per_band"], 16)

        reg.get(DEFAULT_DUT_ID).parser.on_event({"type": "console_line", "text": "AP6_420E#"})
        [after] = [d for d in reg.describe() if d["id"] == DEFAULT_DUT_ID]
        self.assertEqual(after["model"], "AP6_420E")
        self.assertEqual(after["vaps_per_band"], 8)

    def test_a_hand_edited_model_goes_through_the_same_detector(self) -> None:
        """Both spellings load identically, and a file cannot store a model the
        console could never have produced."""
        self._duts_file.write_text(
            '[{"id": "default", "model": "AP6420E-PB1005QPCFVFMA8"},'
            ' {"id": "lab2", "model": "not a model"}]'
        )
        reg = self._booted()
        self.assertEqual(reg.get(DEFAULT_DUT_ID).model, "AP6_420E")
        self.assertIsNone(reg.get("lab2").model)


class DeviceIdentityTests(DutRegistryTests):
    """Which physical unit is behind a console, and what changes when it moves.

    A model is not a device. Two AP6_420Es print the same prompt, report the
    same core count and open on the same cable, so the console-identity rule one
    level down -- which revokes a capture when the port, the SSH config or the
    registration changes -- sees nothing at all when one is swapped for the
    other. Every reading taken on the first then goes on being published as the
    second's, which is the gap `device_id` closes.

    The bench is what makes this concrete rather than theoretical: the `default`
    entry carried snapshots recorded while an 840E was cabled to it, and a 420E
    inherited them under its own name.
    """

    def _booted(self) -> DutRegistry:
        return build_default_registry(ws_manager=self.ws, loop=self._loop)

    def _captured(self, reg: DutRegistry) -> DutContext:
        """A DUT holding a reading, as one that has been measured would be."""
        ctx = reg.get(DEFAULT_DUT_ID)
        ctx.backhaul_role = "node"
        ctx.backhaul_uplink = {"iface": "ath15", "rssi": -37}
        ctx.backhaul_captured = True
        ctx.mesh_probe = {"probed": True, "mesh": True, "members": [], "detail": ""}
        return ctx

    def test_learning_an_identity_for_the_first_time_keeps_the_capture(self) -> None:
        """Nobody had asked before, so "we now know" is not "it changed". Read as
        a swap, the first identify after every connect would throw away the
        capture that connect had just taken."""
        reg = self._booted()
        self._captured(reg)

        previous = reg.record_device_id(DEFAULT_DUT_ID, "AP6420E-PB1005QPCFVFMA8")

        self.assertIsNone(previous)
        self.assertTrue(reg.describe()[0]["backhaul"]["captured"])
        self.assertEqual(reg.describe()[0]["device_id"], "AP6420E-PB1005QPCFVFMA8")

    def test_a_different_unit_on_the_same_console_revokes_the_reading(self) -> None:
        """Same cable, same model, same prompt, same core count: nothing else in
        this registry can tell these two apart."""
        reg = self._booted()
        reg.record_device_id(DEFAULT_DUT_ID, "AP6420E-PB1005QPCFVFMA8")
        self._captured(reg)

        previous = reg.record_device_id(DEFAULT_DUT_ID, "AP6420E-PA10054DDHWVF2D")

        self.assertEqual(previous, "AP6420E-PB1005QPCFVFMA8")
        published = reg.describe()[0]
        self.assertEqual(published["device_id"], "AP6420E-PA10054DDHWVF2D")
        self.assertIsNone(published["backhaul"]["role"], "the old unit's reading was published")
        self.assertIsNone(published["backhaul"]["uplink"])
        self.assertFalse(published["backhaul"]["captured"])
        self.assertIsNone(published["mesh_probe"])

    def test_the_same_unit_answering_again_costs_nothing(self) -> None:
        """Identify runs on every connect. If re-confirming the same device
        dropped the capture, the feature would be a capture shredder."""
        reg = self._booted()
        reg.record_device_id(DEFAULT_DUT_ID, "AP6420E-PB1005QPCFVFMA8")
        self._captured(reg)

        self.assertIsNone(reg.record_device_id(DEFAULT_DUT_ID, "AP6420E-PB1005QPCFVFMA8"))
        self.assertTrue(reg.describe()[0]["backhaul"]["captured"])

    def test_a_console_change_leaves_the_identity_unknown_not_wrong(self) -> None:
        """A capture is dropped when the console changes; so is the identity,
        because nobody has asked the device now behind it who it is. Keeping the
        old name would have the next identify read as a swap that never
        happened -- and unknown must never be published as a mismatch."""
        reg = self._booted()
        reg.record_serial_params(DEFAULT_DUT_ID, "/dev/cu.bench", 115200)
        reg.record_device_id(DEFAULT_DUT_ID, "AP6420E-PB1005QPCFVFMA8")

        reg.record_serial_params(DEFAULT_DUT_ID, "/dev/cu.other", 115200)

        self.assertIsNone(reg.get(DEFAULT_DUT_ID).device_id)
        self.assertIsNone(reg.describe()[0]["device_id"])

    def test_a_reopen_on_the_same_console_keeps_the_identity(self) -> None:
        """The revocation must cost an identity only when the console changed.
        Cleared on every open, the card would spend every session unable to say
        whether its reading came off this device or another."""
        reg = self._booted()
        reg.record_serial_params(DEFAULT_DUT_ID, "/dev/cu.bench", 115200)
        reg.record_device_id(DEFAULT_DUT_ID, "AP6420E-PB1005QPCFVFMA8", mode="serial")

        reg.record_serial_params(DEFAULT_DUT_ID, "/dev/cu.bench", 115200)

        self.assertEqual(reg.get(DEFAULT_DUT_ID).device_id, "AP6420E-PB1005QPCFVFMA8")

    def test_an_identity_survives_a_restart(self) -> None:
        """It is learned by a console command, so without persistence it would
        be relearned only after the next connect -- and until then a stale
        reading would have nothing to be compared against."""
        self._booted().record_device_id(DEFAULT_DUT_ID, "AP6420E-PB1005QPCFVFMA8")
        self.assertEqual(self._booted().get(DEFAULT_DUT_ID).device_id, "AP6420E-PB1005QPCFVFMA8")

    def test_an_untouched_default_still_writes_no_bare_entry(self) -> None:
        reg = self._booted()
        reg.record_device_id(DEFAULT_DUT_ID, "AP6420E-PB1005QPCFVFMA8")
        self.assertIn("AP6420E-PB1005QPCFVFMA8", self._duts_file.read_text())

    def test_a_hand_edited_identity_goes_through_the_same_detector(self) -> None:
        """A file must not be able to store an identity no console could produce
        -- otherwise an edit could silence the mismatch it exists to report."""
        self._duts_file.write_text(
            '[{"id": "default", "device_id": "ap6420e-pb1005qpcfvfma8"},'
            ' {"id": "lab2", "device_id": "somebody typed this"}]'
        )
        reg = self._booted()
        self.assertEqual(reg.get(DEFAULT_DUT_ID).device_id, "AP6420E-PB1005QPCFVFMA8")
        self.assertIsNone(reg.get("lab2").device_id)

    def test_an_identity_also_settles_the_model(self) -> None:
        """A hostname names the model as well as the unit, and dropping that
        half leaves the band mapping on its 840-shaped default.

        Found on hardware, not in review: the Pi's mesh node published
        `model: null`, `bands` with a 6GHz radio and `vaps_per_band` 16, while
        its own hostname said AP6_420 -- eight VAPs per band and no 6GHz radio
        at all. `band_for_iface` would have answered "2.4G" for ath8, measured
        at 5.66 GHz on that very device.
        """
        reg = self._booted()
        reg.record_device_id(DEFAULT_DUT_ID, "AP6420-PA10054DDHWVF2D")

        published = reg.describe()[0]
        self.assertEqual(published["model"], "AP6_420")
        self.assertEqual(published["vaps_per_band"], 8)
        self.assertEqual(published["bands"], ["2.4G", "5G"], "invented a 6GHz radio")
        self.assertEqual(published["model_cores"], 2)

    def test_a_swapped_device_moves_the_model_with_it(self) -> None:
        """The identity is a read of the device in front of us now, so it
        outranks a model left behind by whatever used to be on this console.
        Filling the field in only when empty would leave an 840E's sixteen-wide
        mapping over a 420 that had replaced it."""
        reg = self._booted()
        reg.record_device_id(DEFAULT_DUT_ID, "AP6840E-PD1005VMG3KJH9C")
        self.assertEqual(reg.describe()[0]["model"], "AP6_840E")

        reg.record_device_id(DEFAULT_DUT_ID, "AP6420-PA10054DDHWVF2D")

        published = reg.describe()[0]
        self.assertEqual(published["model"], "AP6_420")
        self.assertEqual(published["vaps_per_band"], 8)

    def test_the_model_it_implies_survives_a_restart(self) -> None:
        self._booted().record_device_id(DEFAULT_DUT_ID, "AP6420-PA10054DDHWVF2D")
        self.assertEqual(self._booted().get(DEFAULT_DUT_ID).model, "AP6_420")

    def test_recording_against_an_unknown_dut_is_a_noop(self) -> None:
        self.assertIsNone(self._booted().record_device_id("nosuchdut", "AP6420E-PB1005QPCFVFMA8"))


class ReadingStampTests(DutRegistryTests):
    """Every snapshot leaves here naming the unit it was measured on.

    Stamped in the registry's own event closure rather than in the parser, which
    has no idea which DUT it belongs to, and rather than at persist time, so the
    live stream and the backfill carry one field a card can read with one rule.
    """

    def _booted(self) -> DutRegistry:
        return build_default_registry(ws_manager=self.ws, loop=self._loop)

    def _snapshot(self, ts: str = "T1") -> dict:
        return {
            "type": "snapshot_update",
            "snapshot": {"test_count": 1, "device_ts": ts, "cpu": {"0": {"idle": 80.0}}},
        }

    def test_a_snapshot_carries_the_identified_unit(self) -> None:
        reg = self._booted()
        reg.record_device_id(DEFAULT_DUT_ID, "AP6420E-PB1005QPCFVFMA8")
        ctx = reg.get(DEFAULT_DUT_ID)

        ctx.parser.on_event(self._snapshot())

        self.assertEqual(
            ctx.snapshot_store.recent(1)[0]["device_id"], "AP6420E-PB1005QPCFVFMA8"
        )

    def test_a_snapshot_taken_before_anyone_asked_is_stamped_unknown(self) -> None:
        """Not omitted and not guessed. A reader has to be able to tell "this
        came off another device" from "nobody knows", and only one of those is
        worth a warning."""
        reg = self._booted()
        ctx = reg.get(DEFAULT_DUT_ID)

        ctx.parser.on_event(self._snapshot())

        self.assertIsNone(ctx.snapshot_store.recent(1)[0]["device_id"])

    def test_readings_taken_before_and_after_a_swap_are_told_apart(self) -> None:
        """The whole point, end to end through the registry: the reading held
        from the departed unit keeps its own name while the console moves on."""
        reg = self._booted()
        ctx = reg.get(DEFAULT_DUT_ID)
        reg.record_device_id(DEFAULT_DUT_ID, "AP6420E-PA10054DDHWVF2D")
        ctx.parser.on_event(self._snapshot("T1"))

        reg.record_device_id(DEFAULT_DUT_ID, "AP6420E-PB1005QPCFVFMA8")
        ctx.parser.on_event(self._snapshot("T2"))

        stamps = [snap["device_id"] for snap in ctx.snapshot_store.recent(10)]
        self.assertEqual(
            stamps, ["AP6420E-PA10054DDHWVF2D", "AP6420E-PB1005QPCFVFMA8"]
        )

    def test_the_browser_is_told_the_same_thing_as_the_store(self) -> None:
        """The card reads the live stream, not only the backfill. Two sources
        disagreeing about which device a number came from would be worse than
        neither carrying it."""
        reg = self._booted()
        reg.record_device_id(DEFAULT_DUT_ID, "AP6420E-PB1005QPCFVFMA8")

        reg.get(DEFAULT_DUT_ID).parser.on_event(self._snapshot())

        emitted = [e for e in self.ws.events if e.get("type") == "snapshot_update"]
        self.assertEqual(
            emitted[-1]["snapshot"]["device_id"], "AP6420E-PB1005QPCFVFMA8"
        )
