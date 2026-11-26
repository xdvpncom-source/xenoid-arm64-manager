"""Real native stores/target validation; no Docker, Android, or network calls."""
from __future__ import annotations

import ast
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import secrets
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / "nativebundle/source/src/xenoid"
PACKAGE = "native_regeneration_location_fixture"
pkg = ModuleType(PACKAGE)
pkg.__path__ = [str(NATIVE)]
sys.modules[PACKAGE] = pkg
config = ModuleType(PACKAGE + ".config")


class InstanceError(RuntimeError):
    def __init__(self, code, message=None):
        self.code = code
        super().__init__(message or code)


config.InstanceError = InstanceError
config.InstanceContext = SimpleNamespace
sys.modules[config.__name__] = config


def load(name):
    spec = importlib.util.spec_from_file_location(PACKAGE + "." + name, NATIVE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cellular, location, identity = (load(name) for name in ("cellular", "location", "device_identity"))


class NativeLocationRegenerationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.store = location.LocationStateStore(self.root, ["1.1.1.1"])
        self.store.ensure("fixture-instance", "JP")
        self.store.set_desired("JP")
        self.store.mark_staged("a" * 64)
        self.store.arm_restart()
        self.store.mark_restarted("b" * 64)
        self.store.promote("b" * 64)
        self.before = self.store.path.read_bytes()

    def target(self, epoch="1" * 32, **changes):
        return self.store.regeneration_target(epoch, country="US",
                    timezone="America/Chicago", locales=["en-US"],
                    randomize_carrier=True, **changes)

    def test_new_default_is_coherent_us_with_seeded_carrier_and_timezone(self):
        carriers, zones = set(), set()
        for index in range(1, 41):
            root = self.root / str(index)
            store = location.LocationStateStore(root)
            with patch.object(location.secrets, "token_bytes", return_value=index.to_bytes(32, "big")):
                state, created = store.ensure("new-device")
            self.assertTrue(created)
            self.assertEqual(state["desiredCountry"], "US")
            profile = state["profiles"]["US"]["profile"]
            self.assertEqual(profile["locale"], ["en-US"])
            self.assertEqual(profile["operator"]["isoCountry"], "us")
            self.assertEqual(profile["locationKey"], "US/" + profile["timezone"])
            self.assertTrue(profile["sim"]["imsi"].startswith(profile["operator"]["numeric"]))
            carriers.add(profile["carrier"]["name"]); zones.add(profile["timezone"])
            same = location.LocationStateStore(self.root / (str(index) + "-same"))
            with patch.object(location.secrets, "token_bytes", return_value=index.to_bytes(32, "big")):
                repeated, _ = same.ensure("other-instance")
            self.assertEqual(profile, repeated["profiles"]["US"]["profile"])
        self.assertEqual(len(carriers), 3)
        self.assertEqual(zones, set(cellular.dataset_countries()["US"]["timezones"]))

    def test_existing_explicit_country_is_not_migrated_by_new_default(self):
        for requested in (None, "US", "SG"):
            state, created = self.store.ensure("fixture-instance", requested)
            self.assertFalse(created)
            self.assertEqual(state["desiredCountry"], "JP")
            self.assertEqual(self.store.path.read_bytes(), self.before)

    def test_explicit_new_non_us_country_keeps_legacy_regional_defaults(self):
        store = location.LocationStateStore(self.root / "explicit-sg")
        with patch.object(location.secrets, "token_bytes", return_value=b"a" * 32):
            state, _ = store.ensure("new-sg", "SG")
        profile = state["profiles"]["SG"]["profile"]
        expected = cellular.generate_cellular_profile("SG", location._derive_seed(b"a" * 32, "SG", ""))
        self.assertEqual(profile, expected)

    def test_allocation_initialization_does_not_reset_existing_country(self):
        tree = ast.parse((NATIVE / "config.py").read_text())
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                        and any(isinstance(child, ast.Name) and child.id == "initialize_identity"
                                for child in ast.walk(n))
                        and any(isinstance(child, ast.Name) and child.id == "location_created"
                                for child in ast.walk(n)))
        block = next(n for n in function.body if isinstance(n, ast.Try)).body
        initialize = next(n for n in block if isinstance(n, ast.If) and isinstance(n.test, ast.Name)
                          and n.test.id == "initialize_identity")
        scope = {"__package__": PACKAGE, "initialize_identity": True,
                 "context": SimpleNamespace(state_root=self.root, instance_id="fixture-instance"),
                 "cfg": SimpleNamespace(network_dns_servers=["1.1.1.1"])}
        with patch.object(identity, "DeviceIdentityStore", return_value=SimpleNamespace(initialize=lambda: None)):
            exec(compile(ast.fix_missing_locations(ast.Module(body=[initialize], type_ignores=[])),
                         "native-allocation-initialization", "exec"), scope)
        self.assertEqual(self.store.path.read_bytes(), self.before)

    def test_derive_us_is_readonly_and_coherent(self):
        target = self.target()
        self.assertEqual(self.store.path.read_bytes(), self.before)
        profile = target["profile"]
        self.assertEqual(target["country"], "US")
        self.assertEqual(profile["operator"]["isoCountry"], "us")
        self.assertEqual(profile["operator"]["numeric"], profile["carrier"]["mcc"] + profile["carrier"]["mnc"])
        self.assertTrue(profile["sim"]["imsi"].startswith(profile["operator"]["numeric"]))
        self.assertEqual(profile["locale"], ["en-US"])
        self.assertEqual(profile["timezone"], "America/Chicago")
        self.assertEqual(profile["locationKey"], "US/America/Chicago")
        self.assertEqual(cellular.validate_profile(profile), profile)
        self.assertEqual(self.target(), target)

    def test_radio_commit_changes_country_atomically_and_retry_keeps_pending_phase(self):
        target = self.target()
        state = self.store.rotate_sim_identity(sim_epoch=target["simEpoch"], expected_epoch="",
                    expected_profile_digest=target["profileDigest"], target=target)
        self.assertEqual(state["desiredCountry"], "US")
        self.assertEqual(state["pending"]["phase"], "new")
        self.assertEqual(state["active"]["country"], "JP")
        self.assertEqual(state["profiles"]["US"]["profile"], target["profile"])
        self.store.mark_staged("b" * 64)
        self.store.arm_restart()
        armed = self.store.path.read_bytes()
        with patch.object(self.store, "regeneration_target", side_effect=AssertionError("must use pinned target")):
            self.store.rotate_sim_identity(sim_epoch=target["simEpoch"], expected_epoch="",
                    expected_profile_digest=target["profileDigest"], target=target)
        self.assertEqual(self.store.path.read_bytes(), armed)
        self.store.mark_restarted("c" * 64)
        promoted = self.store.promote("c" * 64)
        self.assertEqual(promoted["active"]["country"], "US")
        self.assertEqual(promoted["active"]["profileDigest"], target["profileDigest"])
        self.assertIsNone(promoted["pending"])

    def test_mutated_target_or_third_epoch_rejected_before_write(self):
        target = self.target()
        for field, value in (("country", "JP"), ("simEpoch", "2" * 32),
                             ("profileDigest", "f" * 64)):
            changed = copy.deepcopy(target)
            changed[field] = value
            with self.subTest(field=field), self.assertRaises(location.LocationError):
                self.store.rotate_sim_identity(sim_epoch=target["simEpoch"], expected_epoch="",
                        expected_profile_digest=target["profileDigest"], target=changed)
            self.assertEqual(self.store.path.read_bytes(), self.before)
        with self.assertRaises(location.LocationError):
            self.store.rotate_sim_identity(sim_epoch=target["simEpoch"], expected_epoch="3" * 32,
                    expected_profile_digest=target["profileDigest"], target=target)
        self.assertEqual(self.store.path.read_bytes(), self.before)

    def test_us_timezone_and_locale_must_belong_to_catalog(self):
        for options in ({"timezone": "Asia/Tokyo"}, {"locales": ["ja-JP"]},
                        {"locales": []}, {"locales": ["en-US", "en-US"]}):
            with self.subTest(options=options), self.assertRaises(cellular.CellularError):
                self.store.regeneration_target("1" * 32, country="US", **options)
        self.assertEqual(self.store.path.read_bytes(), self.before)

    def test_random_carrier_excludes_current_and_legacy_keeps_it(self):
        target = self.target()
        self.store.rotate_sim_identity(sim_epoch=target["simEpoch"], expected_epoch="",
                expected_profile_digest=target["profileDigest"], target=target)
        self.store.mark_staged("b" * 64); self.store.arm_restart()
        self.store.mark_restarted("c" * 64); self.store.promote("c" * 64)
        old = target["profile"]["carrier"]
        observed = set()
        for index in range(1, 31):
            result = self.store.regeneration_target("%032x" % (index + 1), country="US",
                        timezone="America/New_York", locales=["es-US"], randomize_carrier=True)
            carrier = result["profile"]["carrier"]
            self.assertNotEqual((carrier["mcc"], carrier["mnc"]), (old["mcc"], old["mnc"]))
            observed.add(carrier["name"])
        self.assertEqual(len(observed), 2)
        legacy = self.store.regeneration_target("a" * 32)
        self.assertEqual(legacy["profile"]["carrier"], old)

    def test_single_carrier_country_has_valid_fallback(self):
        current = cellular.generate_cellular_profile("JP", b"a" * 32)["carrier"]
        profile = cellular.generate_cellular_profile("JP", b"b" * 32, exclude_carrier=current)
        self.assertEqual(profile["carrier"], current)

    def journal_parts(self, include_location=True):
        stable = identity._generate_stable()
        cid = "a" * 64
        before = {"containerId": cid, "containerEpoch": identity.container_epoch(cid),
            "imageId": "sha256:" + "b" * 64, "imageInputSha256": "c" * 64,
            "imageBootInputSha256": "d" * 64, "runtimeEpoch": "e" * 64,
            "stableDigest": identity.stable_identity_digest(stable), "simEpoch": "",
            "locationDigest": self.store.load()["active"]["profileDigest"],
            "bootId": identity.generate_boot_id(), "statfsFsid": "1" * 16,
            "bluetoothAddress": "02:11:22:33:44:55", "deviceName": "Old phone",
            "hostname": "old-phone", "googleBindingDigest": "f" * 64,
            "drmDeviceUniqueId": "2" * 32, "advertisingIdDigest": "",
            "gsfAndroidIdDigest": "", "protectionEngineId": "fixture-engine",
            "protectionExpectedDigest": "0" * 64}
        sidecar = self.target()
        new_stable = identity.generate_stable_target(stable)
        target = {"stable": new_stable, "stableDigest": identity.stable_identity_digest(new_stable),
            "simEpoch": sidecar["simEpoch"], "locationProfileDigest": sidecar["profileDigest"],
            "bootId": identity.generate_boot_id(), "statfsFsid": "3" * 16,
            "bluetoothAddress": "02:66:77:88:99:AA", "deviceName": "New phone",
            "hostname": "new-phone", "googleBindingDigest": "f" * 64,
            "drmDeviceUniqueId": "4" * 32}
        if include_location:
            target["locationTarget"] = sidecar
        return before, target, stable

    def test_new_journal_roundtrip_keeps_complete_location_and_legacy_is_unchanged(self):
        for include in (True, False):
            with self.subTest(include_location=include):
                before, target, _ = self.journal_parts(include)
                context = SimpleNamespace(state_root=self.root, instance_id="fixture-instance")
                journal = identity.RegenerationJournal(context)
                if journal.path.exists(): journal.path.unlink()
                saved = journal.prepare("1" * 32, before, target)
                self.assertEqual(journal.load()["target"], target)
                self.assertEqual(saved["before"], before)
                self.assertEqual("locationTarget" in saved["target"], include)

    def test_journal_rejects_mismatched_sidecar_and_unknown_target_field(self):
        before, target, _ = self.journal_parts()
        for key, value in (("simEpoch", "f" * 32), ("locationProfileDigest", "e" * 64),
                           ("unexpected", True)):
            changed = copy.deepcopy(target); changed[key] = value
            with self.subTest(key=key), self.assertRaises(identity.IdentityError):
                identity._validate_regeneration_target(changed, before)

    def test_actual_cli_new_target_is_reserved_and_journaled_before_location_write(self):
        tree = ast.parse((NATIVE / "cli.py").read_text())
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                    and n.name == "_execute_device_regeneration")
        before, _, stable = self.journal_parts()
        events = []
        saved = {}

        class Prepared(Exception): pass
        class Journal:
            def __init__(inner, context): pass
            def load(inner): return None
            def prepare(inner, transaction, old, target):
                events.append("journal")
                saved.update(identity._validate_regeneration_target(target, old))
                self.assertEqual(self.store.path.read_bytes(), self.before)
                raise Prepared()
        class Gate:
            def transaction_id(inner): return "2" * 32
            def location_target(inner, value): inner.sidecar = value
            def reserve(inner, old, factory):
                events.append("reserve")
                result = factory()
                self.assertEqual(result["locationTarget"], inner.sidecar)
                self.assertEqual(self.store.path.read_bytes(), self.before)
                return result
        manager = SimpleNamespace(cfg=SimpleNamespace(network_dns_servers=["1.1.1.1"], cold_profile=None),
                                  regeneration_snapshot=lambda: copy.deepcopy(before))
        context = SimpleNamespace(state_root=self.root)
        scope = {"RegenerationJournal": Journal, "DeviceIdentityStore": lambda c: SimpleNamespace(load=lambda: {"stable": stable}),
            "runtime": lambda a: manager, "LocationStateStore": location.LocationStateStore,
            "normalize_country": location.normalize_country,
            "_regeneration_google_preflight": lambda *a: {"ok": True},
            "_regeneration_runtime_preflight": lambda *a: {"ok": True}, "secrets": secrets,
            "generate_stable_target": identity.generate_stable_target,
            "stable_identity_digest": identity.stable_identity_digest,
            "generate_boot_id": identity.generate_boot_id, "LocationError": location.LocationError,
            "_generate_bluetooth_address": lambda *a: "02:66:77:88:99:AA",
            "_generate_device_name": lambda *a, **k: "New phone", "_generate_hostname": lambda *a: "new-phone"}
        module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), node], type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), "actual-native-cli", "exec"), scope)
        args = SimpleNamespace(context=context, _identity_reservation_hook=Gate(), location_country="US",
                location_timezone="America/Los_Angeles", location_locales=["en-US"], randomize_carrier=True)
        with patch.object(location, "generate_cellular_profile", wraps=cellular.generate_cellular_profile) as generation:
            with self.assertRaises(Prepared): scope["_execute_device_regeneration"](args)
        self.assertEqual(generation.call_args.kwargs["msisdn_generation_version"], 2)
        self.assertEqual(events, ["reserve", "journal"])
        self.assertEqual(saved["locationTarget"]["profile"]["timezone"], "America/Los_Angeles")
        self.assertEqual(self.store.path.read_bytes(), self.before)

    def test_legacy_us_target_derivation_stays_v1_while_explicit_v2_is_pinned(self):
        legacy = self.target()
        self.assertTrue(legacy["profile"]["sim"]["msisdn"].startswith("+120155501"))
        current = self.target(msisdn_generation_version=2)
        self.assertNotEqual(current["profile"]["sim"]["msisdn"], legacy["profile"]["sim"]["msisdn"])
        with patch.object(self.store, "regeneration_target", side_effect=AssertionError("must use saved v2 target")):
            changed = self.store.rotate_sim_identity(sim_epoch=current["simEpoch"], target=current,
                                                    expected_profile_digest=current["profileDigest"])
        self.assertEqual(changed["profiles"]["US"]["profile"], current["profile"])

    def test_actual_radio_phase_retries_exact_target_after_failed_stage_without_recreate(self):
        before, target, _ = self.journal_parts()
        tree = ast.parse((NATIVE / "cli.py").read_text())
        execute = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                       and n.name == "_execute_device_regeneration")
        radio = next(n for n in execute.body if isinstance(n, ast.If)
                     and any(isinstance(value, ast.Constant) and value.value == "radio_committed"
                             for value in ast.walk(n.test)))
        stage = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                     and n.name == "_location_stage")
        calls = []
        def publish(request):
            calls.append(copy.deepcopy(request))
            return {"ok": len(calls) > 1, "error": "location_stage_failed"}
        context = SimpleNamespace(state_root=self.root)
        state = {"before": before, "target": target, "phase": "settings_committed"}
        scope = {"state": state, "context": context, "Mapping": __import__("typing").Mapping,
            "LocationStateStore": location.LocationStateStore, "LocationError": location.LocationError,
            "STAGE_SCHEMA": location.STAGE_SCHEMA, "base64": __import__("base64"),
            "encode_profile_v1": cellular.encode_profile_v1,
            "_location_error_code": lambda answer, fallback: answer.get("error", fallback),
            "_regeneration_phase_at_least": lambda value, phase: value["phase"] == phase,
            "manager": SimpleNamespace(cfg=SimpleNamespace(network_dns_servers=["1.1.1.1"]),
                                       _daemon_runtime_epoch=lambda: "b" * 64),
            "client": SimpleNamespace(location_stage=publish),
            "journal": SimpleNamespace(advance=lambda phase: {**state, "phase": phase})}
        # The production phase's failure returns are irrelevant to this test;
        # a live epoch is supplied so the executable block has no top-level return.
        radio = copy.deepcopy(radio)
        for parent in ast.walk(radio):
            for name, value in list(ast.iter_fields(parent)):
                if isinstance(value, list):
                    setattr(parent, name, [ast.Raise(exc=ast.Call(func=ast.Name(id="AssertionError", ctx=ast.Load()),
                        args=[ast.Constant("unexpected runtime failure")], keywords=[]), cause=None)
                        if isinstance(child, ast.Return) else child for child in value])
        module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                                  stage, radio], type_ignores=[])
        compiled = compile(ast.fix_missing_locations(module), "actual-radio-phase", "exec")
        with self.assertRaisesRegex(location.LocationError, "location_stage_failed"):
            exec(compiled, scope)
        after_failed_stage = self.store.load()
        self.assertEqual(after_failed_stage["pending"]["phase"], "new")
        self.assertEqual(after_failed_stage["profiles"]["US"]["profile"], target["locationTarget"]["profile"])
        with patch.object(location.LocationStateStore, "regeneration_target",
                          side_effect=AssertionError("retry must not reroll")):
            exec(compiled, scope)
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(scope["state"]["phase"], "radio_committed")
        self.assertEqual(self.store.load()["pending"]["phase"], "armed")


if __name__ == "__main__": unittest.main()
