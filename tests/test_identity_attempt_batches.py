"""Collision batches append durable attempts without rerolling admitted work."""
import copy
from pathlib import Path
import unittest
from unittest.mock import patch

import test_identity_native_reservation as native_fixture
import test_identity_recovery as recovery_fixture


class AttemptBatchTests(unittest.TestCase):
    def setUp(self):
        self.f = native_fixture.NativeReservationBoundary()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.h = self.f.helper

    def decision_transport(self, gate):
        def wait(value, path):
            if not path.with_suffix(".ack.json").exists():
                packet = {k: value[k] for k in ("request_sha256", "native_transaction", "target_sha256", "attempt", "operation_key")}
                packet["decision"] = "collision" if value["attempt"] < 8 else "reserved"
                self.h.acknowledge(self.f.folder, self.f.config["run_id"], self.f.config, packet)
            return self.h.NativeGate._wait(gate, value, path)
        gate._wait = wait

    def exhausted(self):
        gate = self.f.gate()
        self.decision_transport(gate)
        with self.assertRaisesRegex(self.h.ReservationPending, "identity_collision_batch_pending"):
            self.f.run_native(gate)
        self.assertEqual(self.f.generation_count, 8)
        self.assertIsNone(self.f.state)
        self.assertFalse((gate.path / "prepared.json").exists())
        return gate

    def test_eight_collisions_then_ninth_success_same_transaction_after_restart(self):
        first = self.exhausted()
        before = {p.name: p.read_bytes() for p in first.path.iterdir()}
        transaction = first.transaction_id(False)
        resumed = self.f.gate()
        self.decision_transport(resumed)
        with self.assertRaises(native_fixture.BoundaryReached):
            self.f.run_native(resumed)
        self.assertEqual(self.f.generation_count, 9)
        self.assertEqual(self.f.state["transactionId"], transaction)
        self.assertEqual(self.h.read(resumed.path / "prepared.json")["attempt"], 8)
        self.assertEqual(self.f.state["target"], self.h.read(resumed.path / "attempt-8.reserve.json")["target"])
        for name, content in before.items():
            self.assertEqual((resumed.path / name).read_bytes(), content)
        self.assertEqual(len(self.f.prepared), 1)

    def test_prepared_attempt_eight_replay_never_scans_history_or_calls_factory(self):
        self.exhausted()
        gate = self.f.gate(); self.decision_transport(gate)
        with self.assertRaises(native_fixture.BoundaryReached):
            self.f.run_native(gate)
        original = copy.deepcopy(self.f.state)
        self.f.stop_at_engine = True
        with patch.object(Path, "glob", side_effect=AssertionError("history scan forbidden")):
            with self.assertRaises(native_fixture.BoundaryReached):
                self.f.run_native(self.f.gate())
        self.assertEqual(self.f.generation_count, 9)
        self.assertEqual(self.f.state, original)

    def test_candidate_written_before_active_pointer_crash_is_reused_exactly(self):
        self.exhausted()
        gate = self.f.gate(); self.decision_transport(gate)
        original = self.h.replace_owned
        def crash(path, value):
            if str(value.get("path", "")).endswith("attempt-8.reserve.json"):
                raise RuntimeError("simulated active publish crash")
            return original(path, value)
        with patch.object(self.h, "replace_owned", side_effect=crash):
            with self.assertRaisesRegex(RuntimeError, "simulated active publish crash"):
                self.f.run_native(gate)
        raw = (gate.path / "attempt-8.reserve.json").read_bytes()
        self.assertEqual(self.f.generation_count, 9)
        resumed = self.f.gate(); self.decision_transport(resumed)
        with self.assertRaises(native_fixture.BoundaryReached):
            self.f.run_native(resumed)
        self.assertEqual(self.f.generation_count, 9)
        self.assertEqual((gate.path / "attempt-8.reserve.json").read_bytes(), raw)

    def test_tampered_last_collision_ack_cannot_advance_or_generate(self):
        gate = self.exhausted()
        path = gate.path / "attempt-7.reserve.ack.json"
        value = self.h.read(path); value["target_sha256"] = "0" * 64
        self.h.replace_owned(path, value)
        with self.assertRaisesRegex(self.h.ReservationPending, "identity_recovery_ack_changed"):
            self.f.run_native(self.f.gate())
        self.assertEqual(self.f.generation_count, 8)
        self.assertFalse((gate.path / "attempt-8.reserve.json").exists())

    def test_changed_before_after_collisions_does_not_allocate_new_candidate(self):
        self.exhausted()
        self.f.before["hostname"] = "changed-before"
        original = self.f.before_values
        self.h.observe = lambda manager, **kwargs: ({**original(), "hostname": "changed-before"}, {})
        with self.assertRaisesRegex(self.h.ReservationPending, "identity_before_changed"):
            self.f.run_native(self.f.gate())
        self.assertEqual(self.f.generation_count, 8)

    def test_negative_boolean_and_noncanonical_path_attempts_rejected(self):
        gate = self.exhausted()
        active = self.h.read(gate.root / "active.json")
        for path in (active["path"].replace("attempt-7", "attempt--1"),
                     active["path"].replace("attempt-7", "attempt-07")):
            self.h.replace_owned(gate.root / "active.json", {**active, "path": path})
            with self.assertRaisesRegex(self.h.ReservationPending, "identity_request_path_invalid"):
                self.h.active_request(gate.root, gate.run)
        self.h.replace_owned(gate.root / "active.json", active)
        request = self.h.read(gate.path / "attempt-7.reserve.json")
        for attempt in (-1, True, 1.5, "8"):
            with self.assertRaisesRegex(self.h.ReservationPending, "identity_attempt_invalid"):
                gate._request(attempt, "reserve", request["native_before"], request["before_values"],
                              request["target"], request["location_sidecar"])

    def test_appended_attempt_cross_run_recovery_and_exact_ack_path(self):
        fixture = recovery_fixture.RecoveryEnvelopeTest(); fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        h = fixture.h
        request = h.read(fixture.request_path)
        request["attempt"] = 8
        request = h.signed({k: v for k, v in request.items() if k != "request_sha256"}, "request_sha256")
        path = fixture.gate.path / "attempt-8.reserve.json"
        h.write_once(path, request)
        h.replace_owned(fixture.gate.root / "active.json", {"path": str(path.relative_to(fixture.gate.root)),
                        "sha256": request["request_sha256"]})
        result = h.poll(fixture.folder, fixture.current)
        self.assertEqual(result["identity_request"]["attempt"], 8)
        packet = {k: request[k] for k in ("request_sha256", "native_transaction", "target_sha256", "attempt", "operation_key")}
        packet.update(decision="reserved", identity_recovery=result["identity_recovery"])
        self.assertTrue(h.acknowledge(fixture.folder, fixture.current, fixture.current_config, packet)["acknowledged"])
        self.assertTrue(path.with_suffix(".ack.json").exists())
        self.assertFalse(fixture.request_path.with_suffix(".ack.json").exists())


if __name__ == "__main__":
    unittest.main()
