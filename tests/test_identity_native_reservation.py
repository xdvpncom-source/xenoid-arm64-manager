"""Run the actual native GEN entry through its pre-journal boundary.

The manager stops before the first daemon operation.  NativeGate is the real
filesystem implementation; only the remote decision transport is simulated.
No Android/runtime commands are executed by this module.
"""
from __future__ import annotations

import argparse
import ast
import base64
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import tempfile
import types
import unittest
from collections.abc import Mapping
from typing import Any
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "nativebundle/source/src/xenoid/cli.py"
HELPER = ROOT / "qa/identity_reservations.py"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class BoundaryReached(RuntimeError):
    pass


class CrashAfterPrepare(RuntimeError):
    pass


class IdentityError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class NativeReservationBoundary(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name) / "1"
        self.folder.mkdir(mode=0o700)
        spec = importlib.util.spec_from_file_location("identity_gate_native_test", HELPER)
        self.helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.helper)
        self.helper.OWNER = os.getuid()
        self.events = []
        self.generation_count = 0
        self.location_count = 0
        self.cellular = json.loads((ROOT / 'tests/fixtures/identity-native-cellular.json').read_text())
        for entry in self.cellular['profiles']:
            self.assertEqual(hashlib.sha256(base64.b64decode(entry['wire_base64'])).hexdigest(), entry['wire_sha256'])
            self.assertEqual(digest({k: v for k, v in entry['profile'].items() if k != 'identityDigest'}), entry['profile']['identityDigest'])
        self.state = None
        self.prepared = []
        self.identity = {"stable": {"androidId": "0123456789abcdef", "serial": "SERIAL-OLD", "imei": "351804610000001", "imeisv": "01"}}
        self.before = {
            "stableDigest": digest(self.identity["stable"]),
            "simEpoch": "0" * 32,
            "statfsFsid": "0" * 15 + "1",
            "drmDeviceUniqueId": "1" * 32,
            "bootId": "2" * 32,
            "bluetoothAddress": "02:11:22:33:44:55",
            "deviceName": "old-name", "hostname": "old-host",
            "googleBindingDigest": "3" * 64,
            "protectionExpectedDigest": "4" * 64,
            "containerId": "5" * 64, "containerEpoch": "old-epoch",
            "imageId": "6" * 64, "imageInputSha256": "7" * 64,
            "imageBootInputSha256": "8" * 64,
        }
        self.context = types.SimpleNamespace(state_root=self.folder,
            instance_id="12345678-1234-4234-8234-123456789abc", resource_tag="test-tag")
        self.config = {"run_id": "9" * 32, "expected_container_id": self.before["containerId"],
            "instance_id": self.context.instance_id, "resource_tag": self.context.resource_tag}
        outer = self

        class Store:
            def __init__(self, context):
                outer.events.append("identity.construct")

            def load(self):
                outer.events.append("identity.load")
                return copy.deepcopy(outer.identity)

        class Journal:
            def __init__(self, context):
                outer.events.append("journal.construct")

            def load(self):
                outer.events.append("journal.load")
                return copy.deepcopy(outer.state)

            def prepare(self, transaction, before, target):
                outer.events.append("journal.prepare")
                if outer.state is not None:
                    raise AssertionError("journal must not be overwritten")
                outer.state = {"schema": "dev.xenoid.device-regenerate/v3", "transactionId": transaction,
                    "instanceId": outer.context.instance_id, "phase": "prepared",
                    "before": copy.deepcopy(before), "target": copy.deepcopy(target)}
                outer.prepared.append(copy.deepcopy(outer.state))
                return copy.deepcopy(outer.state)

        class Manager:
            context = self.context
            cfg = types.SimpleNamespace(network_dns_servers=["1.1.1.1"], cold_profile=None)

            def regeneration_snapshot(self):
                outer.events.append("snapshot")
                return copy.deepcopy(outer.before)

            def ensure_engine_started(self):
                outer.events.append("ensure_engine")
                if outer.stop_at_engine:
                    raise BoundaryReached("stopped at first engine recovery")
                return {"ok": True}

            def maintain_shared_protection(self, **kwargs):
                outer.events.append("protection")
                return {"ok": True}

            def ensure_regeneration_runtime(self, *args):
                outer.events.append("ensure_runtime")
                return {"ok": True}

            def reconcile_bootstrap(self):
                outer.events.append("bootstrap")
                return {"ok": True}

            def assert_regeneration_single_user(self):
                outer.events.append("single_user")

            def daemon_client(self, **kwargs):
                outer.events.append("daemon_client")
                raise BoundaryReached("stopped before first native stage")

            def location_runtime_container_id(self):
                raise AssertionError("pre-journal owner/resume must not query running manager")

        self.manager = Manager()
        self.stop_at_engine = False

        class Location:
            def __init__(self, *args):
                pass

            def load(self):
                return {"desiredCountry": "SG"}

            def regeneration_target(self, epoch, **options):
                outer.location_count += 1
                profile = copy.deepcopy(outer.cellular['profiles'][1 + (outer.location_count - 1) % 2]['profile'])
                return {"simEpoch": epoch, "country": "SG", "profile": profile,
                        "profileDigest": profile['identityDigest']}

        def stable(previous):
            self.events.append("factory")
            self.generation_count += 1
            return {"androidId": f"{self.generation_count:016x}",
                "serial": f"SERIAL-NEW-{self.generation_count}",
                "imei": f"35180461{self.generation_count:07d}",
                "imeisv": f"{self.generation_count + 1:02d}"}

        def google(*args):
            self.events.append("google_preflight")
            return {"ok": True}

        def runtime_pre(*args):
            self.events.append("runtime_preflight")
            return {"ok": True}

        tree = ast.parse(CLI.read_text())
        function = next(node for node in tree.body
                        if isinstance(node, ast.FunctionDef) and node.name == "_execute_device_regeneration")
        ns = {"argparse": argparse, "Any": Any, "Mapping": Mapping,
            "DeviceIdentityStore": Store, "RegenerationJournal": Journal,
            "runtime": lambda args: self.manager, "secrets": secrets,
            "_regeneration_google_preflight": google,
            "_regeneration_runtime_preflight": runtime_pre,
            "IdentityError": IdentityError, "LocationStateStore": Location,
            "normalize_country": lambda value: value.upper(),
            "generate_stable_target": stable, "stable_identity_digest": digest,
            "generate_boot_id": lambda: str(self.generation_count + 3) * 32,
            "_generate_bluetooth_address": lambda old: f"02:AA:BB:CC:DD:{self.generation_count:02X}",
            "_generate_device_name": lambda old, **kw: f"new-name-{self.generation_count}",
            "_generate_hostname": lambda old: f"new-host-{self.generation_count}"}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(CLI), "exec"), ns)
        self.execute = ns["_execute_device_regeneration"]
        self.helper.observe = lambda manager, **kwargs: (self.before_values(), {"model": "Pixel 6a"})

    def tearDown(self):
        self.temp.cleanup()

    def before_values(self):
        stable = self.identity["stable"]
        return {"android_id": stable["androidId"], "serial": stable["serial"],
            "imei": stable["imei"], "imeisv": stable["imeisv"],
            "boot_id": self.before["bootId"], "bluetooth_address": self.before["bluetoothAddress"],
            "hostname": self.before["hostname"], "device_name": self.before["deviceName"],
            "statfs_fsid": self.before["statfsFsid"], "drm_device_unique_id": self.before["drmDeviceUniqueId"],
            **{key: self.cellular['profiles'][0]['profile']['sim'][key] for key in ('imsi', 'iccid', 'msisdn')}}

    def gate(self):
        return self.helper.NativeGate(self.folder, self.config, self.manager, "run:iteration-1")

    def run_native(self, gate=None):
        args = argparse.Namespace(context=self.context)
        if gate is not None:
            args._identity_reservation_hook = gate
        return self.execute(args)

    def wait_with_ack(self, gate, decisions):
        decisions = iter(decisions)

        def wait(value, path):
            decision = next(decisions)
            packet = {k: value[k] for k in ("request_sha256", "native_transaction",
                "target_sha256", "attempt", "operation_key")}
            packet["decision"] = decision
            self.helper.acknowledge(self.folder, self.config["run_id"], self.config, packet)
            self.events.append("ack." + decision)
            return self.helper.NativeGate._wait(gate, value, path)
        gate._wait = wait

    def test_legacy_without_hook_reaches_same_prepare_before_native_stage(self):
        with self.assertRaises(BoundaryReached):
            self.run_native()
        self.assertEqual(len(self.prepared), 1)
        self.assertEqual(self.generation_count, 1)
        order = [self.events.index(x) for x in ("google_preflight", "runtime_preflight", "snapshot", "factory", "journal.prepare", "daemon_client")]
        self.assertEqual(order, sorted(order))
        self.assertFalse((self.folder / "identity-reservations").exists())

    def test_unknown_ack_is_before_journal_and_retains_exact_request(self):
        gate = self.gate()
        def pending(value, path):
            raise self.helper.ReservationPending("simulated lost ACK")
        gate._wait = pending
        with self.assertRaises(self.helper.ReservationPending):
            self.run_native(gate)
        self.assertIsNone(self.state)
        self.assertEqual(self.prepared, [])
        self.assertNotIn("daemon_client", self.events)
        raw = (gate.path / "attempt-0.reserve.json").read_bytes()
        tx = gate.transaction_id(create=False)
        new_gate = self.gate()
        self.wait_with_ack(new_gate, ["reserved", "reserved"])
        with self.assertRaises(BoundaryReached):
            self.run_native(new_gate)
        self.assertEqual(self.generation_count, 1)
        self.assertEqual(self.location_count, 1)
        self.assertEqual((new_gate.path / "attempt-0.reserve.json").read_bytes(), raw)
        request = self.helper.read(new_gate.path / "attempt-0.reserve.json")
        self.assertEqual(request['location_sidecar']['profileDigest'], request['target']['locationProfileDigest'])
        self.assertEqual(request['location_sidecar']['simEpoch'], request['target']['simEpoch'])
        self.assertEqual(self.state["transactionId"], tx)
        self.assertTrue((new_gate.path / "prepared.json").is_file())

    def test_explicit_collision_rerolls_only_before_first_journal_prepare(self):
        gate = self.gate()
        self.wait_with_ack(gate, ["collision", "reserved", "reserved"])
        with self.assertRaises(BoundaryReached):
            self.run_native(gate)
        self.assertEqual(self.generation_count, 2)
        self.assertEqual(len(self.prepared), 1)
        first = self.helper.read(gate.path / "attempt-0.reserve.json")
        accepted = self.helper.read(gate.path / "attempt-1.reserve.json")
        self.assertNotEqual(first["target_sha256"], accepted["target_sha256"])
        self.assertEqual(first["native_transaction"], accepted["native_transaction"])
        self.assertEqual(self.state["target"], accepted["target"])
        self.assertLess(self.events.index("ack.reserved"), self.events.index("journal.prepare"))

    def explicit_location_args(self, gate):
        return argparse.Namespace(context=self.context, location_country="SG", location_timezone="Asia/Singapore",
                                  location_locales=["en-SG"], randomize_carrier=False,
                                  _identity_reservation_hook=gate)

    def test_explicit_location_target_is_exactly_the_reserved_sidecar(self):
        gate = self.gate()
        self.wait_with_ack(gate, ["reserved", "reserved"])
        with self.assertRaises(BoundaryReached):
            self.execute(self.explicit_location_args(gate))
        request = self.helper.read(gate.path / "attempt-0.reserve.json")
        self.assertEqual(request["target"]["locationTarget"], request["location_sidecar"])
        self.assertEqual(self.state["target"]["locationTarget"], request["location_sidecar"])
        self.assertEqual(len(self.prepared), 1)

    def test_sidecar_country_cannot_differ_from_embedded_target_before_reservation(self):
        gate = self.gate()
        original = gate.location_target
        def changed_country(value):
            # Same epoch and profile digest: only the new whole-document check
            # detects that the separately supplied country was changed.
            original({**value, "country": "US"})
        gate.location_target = changed_country
        with self.assertRaisesRegex(self.helper.ReservationPending, "identity_location_target_changed"):
            self.execute(self.explicit_location_args(gate))
        self.assertIsNone(self.state)
        self.assertEqual(self.prepared, [])
        self.assertNotIn("daemon_client", self.events)
        self.assertEqual(list(gate.path.glob("attempt-*.reserve.json")), [])

    def test_optional_location_target_preserves_legacy_sidecar_validation(self):
        profile = copy.deepcopy(self.cellular["profiles"][1]["profile"])
        sidecar = {"simEpoch": "a" * 32, "country": "SG", "profile": profile,
                   "profileDigest": profile["identityDigest"]}
        old_target = {"simEpoch": sidecar["simEpoch"], "locationProfileDigest": sidecar["profileDigest"]}
        self.assertEqual(self.helper.location_sidecar(sidecar, old_target), sidecar)
        target = {**old_target, "locationTarget": copy.deepcopy(sidecar)}
        self.assertEqual(self.helper.location_sidecar(copy.deepcopy(sidecar), target), sidecar)
        target["locationTarget"]["country"] = "US"
        with self.assertRaisesRegex(self.helper.ReservationPending, "identity_location_target_changed"):
            self.helper.location_sidecar(sidecar, target)

    def test_sim_only_collision_rederives_sidecar_before_prepare(self):
        gate = self.gate()
        occupied_imsi = self.cellular['profiles'][1]['profile']['sim']['imsi']
        decisions = []
        original_wait = self.helper.NativeGate._wait

        def wait(value, path):
            decision = 'collision' if value['location_sidecar']['profile']['sim']['imsi'] == occupied_imsi else 'reserved'
            decisions.append(decision)
            self.assertIsNone(self.state, 'controller collision decision must precede journal.prepare')
            packet = {key: value[key] for key in ('request_sha256', 'native_transaction', 'target_sha256', 'attempt', 'operation_key')}
            packet['decision'] = decision
            self.helper.acknowledge(self.folder, self.config['run_id'], self.config, packet)
            return original_wait(gate, value, path)

        gate._wait = wait
        # Prepared rechecks the existing ACK after journal creation; only the
        # reservation decisions above are made by this simulated controller.
        def prepared(state):
            gate._wait = lambda value, path: original_wait(gate, value, path)
            return self.helper.NativeGate.prepared(gate, state)
        gate.prepared = prepared
        with self.assertRaises(BoundaryReached):
            self.run_native(gate)
        self.assertEqual(decisions, ['collision', 'reserved'])
        self.assertEqual(self.location_count, 2)
        self.assertEqual(len(self.prepared), 1)
        accepted = self.helper.read(gate.path / 'attempt-1.reserve.json')
        self.assertNotEqual(accepted['location_sidecar']['profile']['sim']['imsi'], occupied_imsi)
        self.assertEqual(accepted['location_sidecar']['profileDigest'], self.state['target']['locationProfileDigest'])

    def prepare_then_crash(self):
        gate = self.gate()
        self.wait_with_ack(gate, ["reserved"])
        def crash(state):
            raise CrashAfterPrepare("process died after durable journal.prepare")
        gate.prepared = crash
        with self.assertRaises(CrashAfterPrepare):
            self.run_native(gate)
        self.assertEqual(len(self.prepared), 1)
        self.assertFalse((gate.path / "prepared.json").exists())
        return gate

    def test_crash_after_prepare_resumes_same_target_before_engine_recovery(self):
        gate = self.prepare_then_crash()
        original = copy.deepcopy(self.state)
        tx = gate.transaction_id(create=False)
        self.events.clear()
        resumed = self.gate()
        self.stop_at_engine = True
        with self.assertRaises(BoundaryReached):
            self.run_native(resumed)
        self.assertTrue((resumed.path / "prepared.json").is_file())
        self.assertEqual(self.state, original)
        self.assertEqual(self.state["transactionId"], tx)
        self.assertEqual(self.generation_count, 1)
        self.assertNotIn("snapshot", self.events)
        self.assertNotIn("factory", self.events)
        self.assertNotIn("journal.prepare", self.events)
        self.assertEqual(self.events[-1], "ensure_engine")

    def test_changed_prepared_target_rejected_before_engine_or_new_factory(self):
        self.prepare_then_crash()
        self.state["target"]["hostname"] = "foreign-changed-target"
        self.events.clear()
        with self.assertRaisesRegex(self.helper.ReservationPending, "prepared_identity_not_reserved"):
            self.run_native(self.gate())
        self.assertNotIn("ensure_engine", self.events)
        self.assertNotIn("factory", self.events)
        self.assertEqual(len(self.prepared), 1)

    def test_missing_prepared_owner_cannot_invent_new_transaction(self):
        gate = self.prepare_then_crash()
        (gate.path / "owner.json").unlink()
        self.events.clear()
        with self.assertRaisesRegex(self.helper.ReservationPending, "prepared_identity_owner_missing"):
            self.run_native(self.gate())
        self.assertFalse((gate.path / "owner.json").exists())
        self.assertNotIn("ensure_engine", self.events)
        self.assertEqual(self.generation_count, 1)

    def test_reserved_ack_after_crash_never_replays_fresh_preflight(self):
        gate = self.gate()
        self.wait_with_ack(gate, ["reserved", "reserved"])
        with self.assertRaises(BoundaryReached):
            self.run_native(gate)
        original = copy.deepcopy(self.state)
        self.events.clear()
        with self.assertRaises(BoundaryReached):
            self.run_native(self.gate())
        self.assertEqual(self.state, original)
        self.assertEqual(self.generation_count, 1)
        self.assertNotIn("google_preflight", self.events)
        self.assertNotIn("runtime_preflight", self.events)
        self.assertNotIn("snapshot", self.events)
        self.assertLess(self.events.index("ensure_engine"), self.events.index("daemon_client"))


if __name__ == "__main__":
    unittest.main()
