"""Terminal handoff retires transport only, never the retained native work."""
from contextlib import closing, nullcontext
import copy
import fcntl
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_identity_recovery as recovery_fixture


class TerminalHandoffTests(unittest.TestCase):
    def setUp(self):
        fixture = recovery_fixture.RecoveryEnvelopeTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.f = fixture
        self.h, self.folder, self.db = fixture.h, fixture.folder, fixture.db
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("DELETE FROM runs WHERE id=?", (fixture.current,))
            conn.execute("UPDATE runs SET current=1 WHERE id=?", (fixture.old,))
        self.worker = {"unit": "xenoid-panel-script@1.service", "pid": 0,
                       "control_pid": 0, "active_state": "inactive", "sub_state": "dead"}
        self.worker_patch = patch.object(self.h, "_terminal_worker", return_value=self.worker.copy())
        self.worker_mock = self.worker_patch.start()
        self.addCleanup(self.worker_patch.stop)
        self.native_patch = patch.object(self.h, "_terminal_native_lock", return_value=nullcontext())
        self.native_mock = self.native_patch.start()
        self.addCleanup(self.native_patch.stop)

    def proof(self):
        return self.h.terminal_handoff(self.folder, self.f.old)

    def receipt(self, document):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE regeneration_receipts SET document=?", (json.dumps(document),))

    def test_exact_terminal_metadata_does_not_mutate_or_ack(self):
        before = self.db.read_bytes(), self.f.request_path.read_bytes()
        value = self.proof()
        self.assertEqual(value["schema"], self.h.TERMINAL_HANDOFF_SCHEMA)
        self.assertEqual(value["original_run"], self.f.old)
        self.assertEqual(value["original_state"], "stopped")
        self.assertEqual(value["original_iteration"], 7)
        self.assertEqual(value["worker"], self.worker)
        self.assertEqual(value["original_config_sha256"], self.h.digest(self.f.old_config))
        self.assertEqual(value["receipt_sha256"], self.h.digest(self.f.receipt))
        self.assertIsNone(value["native_journal_sha256"])
        self.assertEqual(value["request_sha256"], self.f.request["request_sha256"])
        self.h.verified(value, "proof_sha256")
        self.assertEqual(before, (self.db.read_bytes(), self.f.request_path.read_bytes()))
        self.assertFalse(self.f.request_path.with_suffix(".ack.json").exists())

    def test_poll_keeps_original_request_and_attaches_separate_handoff(self):
        value = self.h.poll(self.folder, self.f.old)
        self.assertEqual(value["identity_request"], self.f.request)
        self.assertEqual(value["identity_terminal_handoff"], self.proof())

    def test_active_original_cannot_handoff(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE runs SET state='running'")
        self.assertIsNone(self.proof())
        self.worker_mock.assert_not_called()

    def test_superseded_or_ambiguous_host_current_cannot_handoff(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE runs SET current=0")
        self.assertIsNone(self.proof())

    def test_ambiguous_pending_receipts_rejected(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO regeneration_receipts VALUES(?,?,?,?)",
                         (self.f.old, 6, "another-key", json.dumps(self.f.receipt)))
        with self.assertRaisesRegex(self.h.ReservationPending, "identity_recovery_ambiguous"):
            self.proof()

    def test_changed_source_rejected(self):
        self.f.change_config(self.f.old, source="print('changed')")
        with self.assertRaisesRegex(self.h.ReservationPending, "identity_recovery_source_changed"):
            self.proof()

    def test_wrong_receipt_iteration_rejected(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE regeneration_receipts SET iteration=6")
        with self.assertRaisesRegex(self.h.ReservationPending, "identity_terminal_request_changed"):
            self.proof()

    def test_partial_matching_journal_preserved_not_claimed_complete(self):
        receipt = copy.deepcopy(self.f.receipt)
        receipt["native_journal"] = {"phase": "storage_identity_committed",
            "transactionId": self.f.request["native_transaction"],
            "instanceId": self.f.request["binding"]["instance_id"],
            "before": self.f.request["native_before"], "target": self.f.request["target"]}
        self.receipt(receipt)
        value = self.proof()
        self.assertEqual(value["native_journal_sha256"], self.h.digest(receipt["native_journal"]))
        self.assertNotIn("committed", value)
        self.assertFalse(self.f.request_path.with_suffix(".ack.json").exists())

    def test_changed_journal_owner_rejected(self):
        receipt = copy.deepcopy(self.f.receipt)
        receipt["native_journal"] = {"transactionId": "0" * 32}
        self.receipt(receipt)
        with self.assertRaisesRegex(self.h.ReservationPending, "identity_terminal_native_state_changed"):
            self.proof()

    def test_worker_or_row_change_during_proof_rejected(self):
        self.worker_mock.side_effect = [self.worker, {**self.worker, "pid": 10}]
        with self.assertRaisesRegex(self.h.ReservationPending, "identity_terminal_state_changed"):
            self.proof()
        self.worker_mock.side_effect = None
        def change(_folder, _remaining=None):
            with closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute("UPDATE runs SET state='running'")
            return self.worker
        self.worker_mock.side_effect = change
        with self.assertRaisesRegex(self.h.ReservationPending, "identity_terminal_state_changed"):
            self.proof()

    def test_real_stopped_worker_rejects_live_pid_and_descendant(self):
        self.worker_patch.stop()
        group = "/system.slice/xenoid-panel-script@1.service"
        cgroup = self.folder / "cgroups"
        child = cgroup / group.lstrip("/") / "child"
        child.mkdir(parents=True)
        (child.parent / "cgroup.procs").write_text("")
        (child / "cgroup.procs").write_text("")
        text = "ActiveState=inactive\nSubState=dead\nMainPID=0\nControlPID=0\nControlGroup=" + group + "\n"
        with patch.object(self.h, "CGROUP_ROOT", cgroup), patch.object(self.h.subprocess, "run") as run:
            run.return_value = SimpleNamespace(returncode=0, stdout=text.encode())
            self.assertEqual(self.h._terminal_worker(self.folder), self.worker)
            (child / "cgroup.procs").write_text("123\n")
            with self.assertRaisesRegex(self.h.ReservationPending, "identity_terminal_worker_active"):
                self.h._terminal_worker(self.folder)
            run.return_value.stdout = text.replace("MainPID=0", "MainPID=123").encode()
            with self.assertRaisesRegex(self.h.ReservationPending, "identity_terminal_worker_active"):
                self.h._terminal_worker(self.folder)

    def test_real_native_lock_is_nonblocking_and_never_created(self):
        self.native_patch.stop()
        home = self.folder / "native"
        home.mkdir(mode=0o700)
        instances = home / "instances"
        instances.mkdir(mode=0o755)
        binding = self.f.request["binding"]
        state = instances / binding["instance_id"]
        state.mkdir(mode=0o700)
        self.h.write_once(state / "allocation.json", {"instance_id": binding["instance_id"],
            "resource_tag": binding["resource_tag"], "instance_name": "phone-1"})
        path = state / "operation.lock"
        with patch.object(self.h, "NATIVE_STATE_HOME", home):
            with self.assertRaises(FileNotFoundError):
                with self.h._terminal_native_lock(binding, self.f.request):
                    pass
            self.assertFalse(path.exists())
            path.touch(mode=0o600)
            with self.h._terminal_native_lock(binding, self.f.request):
                pass
            fd = os.open(path, os.O_RDONLY)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(self.h.ReservationPending, "identity_terminal_native_busy"):
                    with self.h._terminal_native_lock(binding, self.f.request):
                        pass
            finally:
                os.close(fd)
            journal = {"instanceId": binding["instance_id"],
                       "transactionId": self.f.request["native_transaction"],
                       "before": self.f.request["native_before"], "target": self.f.request["target"],
                       "phase": "storage_identity_committed"}
            self.h.write_once(state / "device-regenerate-v3.json", journal)
            with self.h._terminal_native_lock(binding, self.f.request):
                pass
            with self.assertRaisesRegex(self.h.ReservationPending, "identity_terminal_native_state_changed"):
                with self.h._terminal_native_lock(binding, self.f.request):
                    self.h.replace_owned(state / "device-regenerate-v3.json", {**journal, "phase": "changed"})


if __name__ == "__main__":
    unittest.main()
