"""Recovery transport preserves the old request while proving the current worker."""
import copy
import hashlib
import json
import os
from pathlib import Path
from contextlib import closing
import sqlite3
import types
import unittest
from unittest.mock import patch

import test_identity_native_reservation as native_fixture


class RecoveryEnvelopeTest(unittest.TestCase):
    def setUp(self):
        self.fixture = native_fixture.NativeReservationBoundary()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.h = self.fixture.helper
        self.folder = self.fixture.folder
        self.old = "9" * 32
        self.current = "8" * 32
        self.key = "run:iteration-1"
        self.fixture.context.resource_tag = "a" * 12
        self.old_config = {**self.fixture.config, "resource_tag": "a" * 12,
                           "identity_reservations": True, "source": "print('original')"}
        self.current_config = {**self.old_config, "run_id": self.current, "source": "print('successor')"}
        context = {"scenario_sha256": hashlib.sha256(self.old_config["source"].encode()).hexdigest(),
                   "profile_plan_sha256": None}
        self.gate = self.h.NativeGate(self.folder, self.old_config, self.fixture.manager, self.key, context=context)
        def pending(*_args):
            raise self.h.ReservationPending("pending")
        self.gate._wait = pending
        with self.assertRaises(self.h.ReservationPending):
            self.fixture.run_native(self.gate)
        self.request_path = self.gate.path / "attempt-0.reserve.json"
        self.request = self.h.read(self.request_path)
        self.receipt = {"status": "pending", "binding": {key: self.request["binding"][key]
                        for key in ("instance_id", "resource_tag", "container_id")},
                        "before_digest": self.fixture.before["stableDigest"], "identity_reservation_context": context}
        self.db = self.folder / "runs.db"
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.executescript("CREATE TABLE runs(id TEXT PRIMARY KEY,config TEXT,state TEXT,iteration INTEGER,current INTEGER);"
                               "CREATE TABLE regeneration_receipts(run_id TEXT,iteration INTEGER,operation_key TEXT,document TEXT);")
            conn.execute("INSERT INTO runs VALUES(?,?,?,?,?)", (self.old, json.dumps(self.old_config), "stopped", 7, 0))
            conn.execute("INSERT INTO runs VALUES(?,?,?,?,?)", (self.current, json.dumps(self.current_config), "waiting", 0, 1))
            conn.execute("INSERT INTO regeneration_receipts VALUES(?,?,?,?)", (self.old, 7, self.key, json.dumps(self.receipt)))
        self.db.chmod(0o600)
        self.worker = {"pid": 321, "start_ticks": 900, "unit": "xenoid-panel-script@1.service"}
        self.worker_patch = patch.object(self.h, "_recovery_worker", return_value=self.worker.copy())
        self.worker_mock = self.worker_patch.start()
        self.addCleanup(self.worker_patch.stop)

    def change_config(self, run, **values):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            old = json.loads(conn.execute("SELECT config FROM runs WHERE id=?", (run,)).fetchone()[0])
            old.update(values)
            conn.execute("UPDATE runs SET config=? WHERE id=?", (json.dumps(old), run))

    def envelope(self):
        return self.h.recovery_envelope(self.folder, self.current)

    def packet(self, envelope=None):
        request = self.h.read(self.request_path)
        return {**{key: request[key] for key in ("request_sha256", "native_transaction", "target_sha256", "attempt", "operation_key")},
                "decision": "reserved", "identity_recovery": envelope or self.envelope()}

    def test_poll_exposes_original_request_separately_without_relabeling_old_observation(self):
        observation = self.h.signed({"schema": self.h.OBSERVATION_SCHEMA, "run_id": self.old,
                                     "values": self.fixture.before_values(), "captured_at": 1}, "proof_sha256")
        self.h.write_once(self.gate.root / "observation.json", observation)
        before_request = self.request_path.read_bytes()
        before_db = self.db.read_bytes()
        result = self.h.poll(self.folder, self.current)
        self.assertEqual(result["identity_request"], self.request)
        self.assertEqual(result["identity_request"]["run_id"], self.old)
        self.assertNotIn("identity_observation", result)
        envelope = result["identity_recovery"]
        self.assertEqual(envelope["current_run"], self.current)
        self.assertEqual(envelope["original_run"], self.old)
        self.assertEqual(envelope["original_iteration"], 7)
        self.assertEqual(envelope["request_sha256"], self.request["request_sha256"])
        self.assertEqual(self.h.validate_recovery_envelope(self.folder, self.current, envelope)["receipt"], self.receipt)
        self.assertEqual(self.request_path.read_bytes(), before_request)
        self.assertEqual(self.db.read_bytes(), before_db)
        self.assertFalse(self.request_path.with_suffix(".ack.json").exists())

    def test_ack_uses_original_path_and_strips_only_transport_envelope(self):
        packet = self.packet()
        original = copy.deepcopy(packet)
        self.assertTrue(self.h.acknowledge(self.folder, self.current, self.current_config, packet)["acknowledged"])
        stored = self.h.read(self.request_path.with_suffix(".ack.json"))
        self.assertEqual(stored, {key: value for key, value in packet.items() if key != "identity_recovery"})
        self.assertEqual(packet, original)
        self.assertFalse((self.folder / "identity-reservations" / self.current).exists())
        self.assertNotIn("identity_recovery", self.h.poll(self.folder, self.current))

    def test_valid_delivered_ack_does_not_block_normal_native_progress(self):
        self.h.acknowledge(self.folder, self.current, self.current_config, self.packet())
        journal = {"transactionId": self.request["native_transaction"], "instanceId": self.fixture.context.instance_id,
                   "before": self.request["native_before"], "target": self.request["target"], "phase": "prepared"}
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE regeneration_receipts SET document=?", (json.dumps({**self.receipt, "native_journal": journal}),))
        self.assertIsNone(self.envelope())
        journal["target"] = {**journal["target"], "hostname": "different"}
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE regeneration_receipts SET document=?", (json.dumps({**self.receipt, "native_journal": journal}),))
        with self.assertRaisesRegex(self.h.ReservationPending, "native_state_changed"):
            self.envelope()

    def test_invalid_existing_ack_is_not_treated_as_delivered(self):
        packet = {key: value for key, value in self.packet().items() if key != "identity_recovery"}
        packet["target_sha256"] = "0" * 64
        self.h.write_once(self.request_path.with_suffix(".ack.json"), packet)
        with self.assertRaisesRegex(self.h.ReservationPending, "ack_changed"):
            self.envelope()

    def test_changed_current_configuration_or_worker_rejects_ack_before_write(self):
        packet = self.packet()
        self.change_config(self.current, source="print('changed')")
        with self.assertRaises(self.h.ReservationPending):
            self.h.acknowledge(self.folder, self.current, self.current_config, packet)
        self.assertFalse(self.request_path.with_suffix(".ack.json").exists())

    def test_worker_replacement_rejects_saved_envelope(self):
        envelope = self.envelope()
        self.worker_mock.return_value = {**self.worker, "start_ticks": 901}
        with self.assertRaisesRegex(self.h.ReservationPending, "envelope_changed"):
            self.h.validate_recovery_envelope(self.folder, self.current, envelope)

    def test_queued_successor_requires_the_same_active_worker_proof(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE runs SET state='queued' WHERE id=?", (self.current,))
        self.assertEqual(self.envelope()["worker"], self.worker)
        self.worker_mock.side_effect = self.h.ReservationPending("identity_recovery_worker_unavailable")
        with self.assertRaisesRegex(self.h.ReservationPending, "worker_unavailable"):
            self.envelope()

    def test_changed_old_source_and_replaced_instance_never_adopt_request(self):
        self.change_config(self.old, source="print('different old source')")
        with self.assertRaisesRegex(self.h.ReservationPending, "source_changed"):
            self.envelope()
        self.change_config(self.current, instance_id="11111111-1111-1111-1111-111111111111")
        self.assertIsNone(self.envelope())

    def test_multiple_pending_operations_are_ambiguous(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO regeneration_receipts VALUES(?,?,?,?)", (self.old, 7, "another-key", json.dumps(self.receipt)))
        with self.assertRaisesRegex(self.h.ReservationPending, "ambiguous"):
            self.envelope()

    def test_terminal_current_run_and_absent_active_request_have_no_recovery(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE runs SET state='stopped' WHERE id=?", (self.current,))
        self.assertIsNone(self.envelope())
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE runs SET state='waiting' WHERE id=?", (self.current,))
        (self.gate.root / "active.json").unlink()
        self.assertIsNone(self.envelope())

    def test_receipt_change_or_native_journal_rejects_stale_transport(self):
        envelope = self.envelope()
        receipt = {**self.receipt, "native_journal": {"phase": "prepared"}}
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE regeneration_receipts SET document=?", (json.dumps(receipt),))
        with self.assertRaisesRegex(self.h.ReservationPending, "native_state_changed"):
            self.h.validate_recovery_envelope(self.folder, self.current, envelope)

    def test_bad_proof_or_extra_field_is_not_accepted(self):
        envelope = self.envelope()
        envelope["current_iteration"] = 10
        with self.assertRaisesRegex(self.h.ReservationPending, "digest_changed"):
            self.h.validate_recovery_envelope(self.folder, self.current, envelope)
        envelope = self.h.signed({**self.envelope(), "extra": True}, "proof_sha256")
        with self.assertRaises(self.h.ReservationPending):
            self.h.validate_recovery_envelope(self.folder, self.current, envelope)

    def test_committed_finalize_request_uses_same_strict_recovery_transport(self):
        reserved_ack = {**{key: self.request[key] for key in ("request_sha256", "native_transaction", "target_sha256", "attempt", "operation_key")},
                        "decision": "reserved"}
        self.h.write_once(self.request_path.with_suffix(".ack.json"), reserved_ack)
        state = {"schema": "dev.xenoid.device-regenerate/v3", "phase": "committed", "instanceId": self.fixture.context.instance_id,
                 "transactionId": self.request["native_transaction"], "before": self.request["native_before"], "target": self.request["target"]}
        self.h.write_once(self.gate.path / "prepared.json", {"native_transaction": state["transactionId"],
                          "target_sha256": self.request["target_sha256"], "attempt": 0})
        request, path = self.gate._request(0, "finalize", state["before"], self.request["before_values"], state["target"],
                                        self.request["location_sidecar"], values=self.h.values_from_target(state["target"], self.request["location_sidecar"]), state=state)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE regeneration_receipts SET document=?", (json.dumps({**self.receipt, "native_journal": state}),))
        envelope = self.envelope()
        packet = {**{key: request[key] for key in ("request_sha256", "native_transaction", "target_sha256", "attempt", "operation_key")},
                  "decision": "finalized", "identity_recovery": envelope}
        self.h.acknowledge(self.folder, self.current, self.current_config, packet)
        self.assertEqual(self.h.read(path.with_suffix(".ack.json"))["decision"], "finalized")
        self.assertIsNone(self.envelope())

    def test_real_worker_metadata_requires_uid_argv_and_systemd_cgroup(self):
        self.worker_patch.stop()
        proc = self.folder / "proc"
        process = proc / "321"
        process.mkdir(parents=True)
        fields = ["S", "1"] + ["0"] * 20
        fields[19] = "900"
        (process / "stat").write_text("321 (python3) " + " ".join(fields))
        (process / "status").write_text("Uid:\t%d\t%d\t%d\t%d\n" % ((os.getuid(),) * 4))
        (process / "cmdline").write_bytes(b"\0".join([b"/usr/bin/python3", str(self.h.SCRIPT_HOST).encode(), b"worker", b"1", b""]))
        (process / "cgroup").write_text("0::/system.slice/xenoid-panel-script@1.service\n")
        shown = b"ActiveState=active\nSubState=running\nMainPID=321\nControlGroup=/system.slice/xenoid-panel-script@1.service\n"
        with patch.object(self.h, "PROC", proc), patch.object(self.h.subprocess, "run", return_value=types.SimpleNamespace(returncode=0, stdout=shown)):
            self.assertEqual(self.h._recovery_worker(self.folder), self.worker)
            (process / "cmdline").write_bytes(b"/usr/bin/python3\0different.py\0")
            with self.assertRaisesRegex(self.h.ReservationPending, "worker_changed"):
                self.h._recovery_worker(self.folder)


if __name__ == "__main__":
    unittest.main()
