"""Durable native profile application: replay, fencing and separate reload."""
import copy
import base64
import hashlib
import json
import random
import shlex
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "qa"))
import qa_device_profile as engine
import qa_profile_runtime as runtime


INSTANCE = "12345678-1234-4234-8234-123456789abc"


class Store:
    def __init__(self): self.saved = []
    def save(self, key, receipt): self.saved.append(copy.deepcopy(receipt))


class Manager:
    def __init__(self, profile):
        self.profile = copy.deepcopy(profile)
        self.property_profile = copy.deepcopy(profile)
        self.context = types.SimpleNamespace(instance_id=INSTANCE)
        self.cfg = self.lease = None
        self.binding = {"instance_id": INSTANCE, "resource_tag": "a" * 12, "container_id": "b" * 64}
        self.epoch = "epoch-before-profile"; self.token = None
        self.reload_calls = 0; self.actual_reloads = 0
        self.fail_reload_once = False
        ids = profile["ids"]
        self.identity = {"stable": {"androidId": ids["android_id"], "serial": ids["serial"], "imei": ids["imei"], "imeisv": ids["imeisv"]}, "pending": None, "pendingStable": None, "active": {"bootId": ids["boot_id"]}}
        self.context.manager = self
    def _userspace_runtime_epoch(self, deadline=None): return self.epoch
    def regeneration_restart_receipt(self, token): return {"ok": token == self.token}
    def clear_regeneration_restart_receipt(self, token):
        if self.token == token: self.token = None
        return {"ok": True}
    def soft_reboot(self, **kwargs):
        self.reload_calls += 1
        assert kwargs["reset_ssaid"] is False and kwargs["require_health"] is False
        if not kwargs["userspace_restart_observed"]:
            self.actual_reloads += 1
            self.epoch = "epoch-after-profile-" + str(self.actual_reloads)
            self.token = kwargs["restart_receipt"]
        if self.fail_reload_once:
            self.fail_reload_once = False
            return {"ok": False, "error": "soft_reboot_daemon_timeout"}
        return {"ok": True}


class Broker:
    def __init__(self, manager):
        self.stopping = threading.Event(); self.manager = manager; self.input_checks = 0
        self.drm = "d" * 32; self.fsid = "f" * 16
        self.bad_staged_field = None
        self.consumer_profile = None
        self.fps = manager.profile["display"]["defaultRefreshRateHz"]; self.frame_calls = []
    def regeneration_binding(self, manager): return dict(manager.binding)
    def verify_regeneration_native_completion(self, manager, cli, receipt, deadline):
        if engine.profile_sha256(manager.identity["stable"]) != receipt["native_completion"]["stable_digest"]:
            raise runtime.ProfilePending("native_identity_changed")
        return {"ok": True, "phase": "committed"}
    def ensure_native_interaction(self, manager, deadline, **kwargs): self.input_checks += 1
    def set_frame_rate(self, fps, timeout=None): self.fps = fps; self.frame_calls.append(fps); return {"fps": fps}
    def native_display_exec(self, manager, args, **kwargs):
        profile = manager.profile; ids = manager.identity
        def properties():
            return {**{prop: str(manager.property_profile["build"][key]) for key, prop in runtime.BUILD_PROPERTIES.items()},
                    "ro.build.display.id": manager.property_profile["build"]["id"]}
        if args == ["getprop"]:
            return types.SimpleNamespace(returncode=0, stdout=("\n".join("[%s]: [%s]" % pair for pair in properties().items())+"\n").encode())
        if args[:2] == ["sh", "-c"] and "XENOID_PROFILE_CONSUMERS" in args[2]:
            actual = self.consumer_profile or profile
            content = {"props": "\n".join("[%s]: [%s]" % pair for pair in properties().items()),
                       "cpuinfo": "\n\n".join("processor: %d\nmodel name: %s\nCPU implementer: %s\nCPU part: %s" % (i, core["model"], actual["cpu"]["implementer"], core["part"]) for i, core in enumerate(actual["cpu"]["cores"])),
                       "meminfo": "MemTotal: %d kB\nSwapTotal: %d kB" % (actual["memory"]["totalKiB"], actual["memory"]["swapBytes"]//1024),
                       "battery": "\n".join("%s: %s" % (key, value) for key, value in actual["battery"].items() if isinstance(value, int))}
            rows = []
            for line in args[2].splitlines():
                if not line.startswith("emit "): continue
                parts = shlex.split(line); key = parts[1]
                if key in content:
                    rows.append("%s:0:%s" % (key, base64.b64encode(content[key].encode()).decode()))
                else: rows.append(key+":1:")
            return types.SimpleNamespace(returncode=0, stdout=("\n".join(rows)+"\n").encode())
        if args[:2] == ["sh", "-c"] and "XENOID_PROFILE_STAGES" in args[2]:
            fields = runtime._staged_fields(profile)
            if self.bad_staged_field: fields[self.bad_staged_field] = "mismatch"
            text = "\n".join(key + "=" + base64.b64encode((value + "\n").encode()).decode() for key, value in sorted(fields.items()))
            return types.SimpleNamespace(returncode=0, stdout=text.encode())
        values = {
            ("cat", runtime.EFFECTIVE_PROFILE): json.dumps(profile),
            ("wm", "size"): "Physical size: 720x1280\nOverride size: %sx%s" % (profile["display"]["width"], profile["display"]["height"]),
            ("wm", "density"): "Physical density: 560\nOverride density: %s" % profile["display"]["densityDpi"],
            ("dumpsys", "SurfaceFlinger", "--latency"): str(1000000000 // self.fps),
            ("settings", "get", "secure", "android_id"): ids["stable"]["androidId"],
            ("getprop", "ro.serialno"): ids["stable"]["serial"],
            ("getprop", "persist.xenoid.radio.imei"): ids["stable"]["imei"],
            ("getprop", "persist.xenoid.radio.imeisv"): ids["stable"]["imeisv"],
            ("cat", "/proc/sys/kernel/random/boot_id"): ids["active"]["bootId"],
            ("cat", "/data/local/tmp/xenoid-profile/drm_device_unique_id"): self.drm,
            ("cat", "/data/local/tmp/xenoid-profile/statfs_fsid"): self.fsid,
        }
        for key, prop in {"model": "ro.product.model", "fingerprint": "ro.build.fingerprint", "id": "ro.build.id", "incremental": "ro.build.version.incremental", "release": "ro.build.version.release", "sdk": "ro.build.version.sdk", "security_patch": "ro.build.version.security_patch", "bootloader": "ro.bootloader"}.items():
            values[("getprop", prop)] = manager.property_profile["build"][key]
        return types.SimpleNamespace(returncode=0, stdout=(values[tuple(args)] + "\n").encode())


class Cli:
    def __init__(self, manager):
        self.manager = manager; self.apply_calls = 0; self.lose_apply_response = False
        self.hide_calls = 0; self.lose_hide_response = False; self.skip_property_publish = False
        self.runtime = lambda _args: manager
        self.print_json = lambda _value: None
        self.fail_nested_action = False
        self.property_warning = False
        class IdentityStore:
            def __init__(self, context): self.context = context
            def load(self): return self.context.manager.identity
        self.DeviceIdentityStore = IdentityStore
    def cmd_device_apply(self, args):
        assert args.instance_identity is True and args.keep_unique is False and args.generate_frida is False
        self.apply_calls += 1
        self.manager.profile = json.loads(Path(args.profile).read_text())
        if self.lose_apply_response:
            self.lose_apply_response = False
            raise ConnectionError("response lost after native apply")
        actions = [{"ok": not self.fail_nested_action}]
        if self.property_warning:
            actions.append({"field": "ro.product.system.model", "ok": True, "actions": [{"exit": 1}]})
        self.print_json({"ok": True, "rotated": False, "apply": {"ok": True, "actions": actions}})
        return 0
    def cmd_hide_apply(self, args):
        assert args.policy is None and args._operation_lock_held is True
        self.hide_calls += 1
        if not self.skip_property_publish:
            self.manager.property_profile = copy.deepcopy(self.manager.profile)
        if self.lose_hide_response:
            self.lose_hide_response = False
            raise ConnectionError("native property publication response lost")
        self.print_json({"ok": True, "failures": [], "actions": [{"name": "property-area", "ok": True, "required": True}]})
        return 0
    def cmd_device_regenerate(self, _args): raise AssertionError("Profile phase must never repeat native GEN")


class RuntimeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base = json.loads((Path(__file__).parent / "fixtures/native-profile-template.json").read_text())
        self.resources = {"enabled": True, "fps": 20, "scale": "minimum", "min_side": 200, "dpi": 72, "timeout": 180, "physical_size": [720, 1280]}
        self.manager = Manager(engine.normalize_resources(self.base, self.resources))
        self.broker = Broker(self.manager); self.cli = Cli(self.manager); self.store = Store()
        self.policy_file = self.root / "policy.json"; self.resource_file = self.root / "resources.json"
        self.policy = engine.default_policy([self.base], self.resources)
        self.policy_file.write_text(json.dumps(self.policy)); self.resource_file.write_text(json.dumps(self.resources))
        plan = runtime.capture_plan(self.broker, self.manager, self.manager.binding, None, config_path=self.policy_file, resource_path=self.resource_file)
        digest = engine.profile_sha256(self.manager.identity["stable"])
        self.receipt = {"binding": copy.deepcopy(self.manager.binding), "profile_plan": plan,
                        "native_journal": {"phase": "committed", "target": {"drmDeviceUniqueId": self.broker.drm, "statfsFsid": self.broker.fsid}},
                        "native_completion": {"stable_digest": digest}}
    def complete(self):
        return runtime.complete(self.broker, self.manager, self.cli, self.receipt, self.store, "round-8-natural-5", None, state_dir=self.root / "state")

    def test_official_apply_preserves_ids_and_publishes_valid_binding(self):
        old_ids = copy.deepcopy(self.manager.identity)
        result = self.complete()
        self.assertTrue(result["official_apply"] and result["native_ids_preserved"])
        self.assertEqual(self.cli.apply_calls, 1)
        self.assertEqual(self.cli.hide_calls, 1)
        self.assertEqual(self.manager.identity, old_ids)
        self.assertEqual(self.manager.actual_reloads, 1)
        state = runtime.load_applied(INSTANCE, "a" * 12, "b" * 64, state_dir=self.root / "state")
        self.assertEqual(state["profile_sha256"], result["profile_sha256"])
        self.assertEqual(state["profile"]["display"]["width"], 202)
        self.assertIsNotNone(runtime.status_for_binding(INSTANCE, "a" * 12, "b" * 64, state_dir=self.root / "state"))

    def test_selected_profile_rate_drives_actual_composer_adapter(self):
        for path in ("display.defaultRefreshRateHz", "display.peakRefreshRateHz"):
            self.receipt["profile_plan"]["policy"]["fields"][path]["values"] = [30]
        self.receipt["profile_plan"]["policy_sha256"] = engine.profile_sha256(self.receipt["profile_plan"]["policy"])
        result = self.complete()
        self.assertEqual(self.broker.frame_calls[-1], 30)
        self.assertEqual(result["period_ns"], 33333333)
        self.assertEqual(result["fps"], 30)

    def test_unchecked_rate_preserves_actual_previous_fps(self):
        self.broker.fps = 30
        plan = runtime.capture_plan(self.broker, self.manager, self.manager.binding, None, config_path=self.policy_file, resource_path=self.resource_file)
        self.assertEqual(plan["previous_profile"]["display"]["defaultRefreshRateHz"], 30)

    def test_capture_preserves_existing_peak_above_actual_cadence(self):
        self.manager.profile["display"]["peakRefreshRateHz"] = 30
        self.broker.fps = 20
        plan = runtime.capture_plan(self.broker, self.manager, self.manager.binding, None, config_path=self.policy_file, resource_path=self.resource_file)
        self.assertEqual(plan["previous_profile"]["display"]["defaultRefreshRateHz"], 20)
        self.assertEqual(plan["previous_profile"]["display"]["peakRefreshRateHz"], 30)

    def test_completed_replay_does_not_apply_or_reload_again(self):
        first = self.complete(); second = self.complete()
        self.assertEqual(first["profile_sha256"], second["profile_sha256"])
        self.assertEqual(first["effective_changes_verified"], second["effective_changes_verified"])
        self.assertEqual(self.cli.apply_calls, 1); self.assertEqual(self.manager.actual_reloads, 1)

    def select_only(self, choices):
        policy = self.receipt["profile_plan"]["policy"]
        for descriptor in engine.field_catalog(self.base):
            if descriptor["mutable"]:
                policy["fields"][descriptor["path"]]["enabled"] = descriptor["path"] in choices
        for path, values in choices.items(): policy["fields"][path]["values"] = values
        self.receipt["profile_plan"]["policy_sha256"] = engine.profile_sha256(policy)

    def test_selected_identical_values_are_not_reported_as_success(self):
        value = self.receipt["profile_plan"]["previous_profile"]["battery"]["temperature"]
        self.select_only({"battery.temperature": [value]})
        with self.assertRaisesRegex(runtime.ProfilePending, "profile_no_effect"): self.complete()
        self.assertEqual(self.cli.apply_calls, 0)
        self.assertIsNone(runtime.load_applied(INSTANCE, state_dir=self.root / "state"))

    def test_all_fields_off_intentionally_retains_profile_without_randomization(self):
        self.select_only({})
        result = self.complete()
        self.assertEqual(result["randomization_skipped"], "no_enabled_fields")
        self.assertEqual(result["effective_changes_verified"], [])
        self.assertTrue(result["official_apply"])

    def test_native_id_changes_alone_do_not_satisfy_selected_field_effect(self):
        value = self.receipt["profile_plan"]["previous_profile"]["battery"]["temperature"]
        self.select_only({"battery.temperature": [value]})
        self.receipt["profile_plan"]["previous_profile"]["ids"]["serial"] = "OLD0000000000000"
        self.receipt["profile_plan"]["previous_sha256"] = engine.profile_sha256(self.receipt["profile_plan"]["previous_profile"])
        with self.assertRaisesRegex(runtime.ProfilePending, "profile_no_effect"): self.complete()
        self.assertEqual(self.cli.apply_calls, 0)

    def test_peak_only_metadata_change_is_not_an_actual_fps_effect(self):
        value = self.receipt["profile_plan"]["previous_profile"]["display"]["peakRefreshRateHz"]
        self.select_only({"display.peakRefreshRateHz": [value + 1]})
        with self.assertRaisesRegex(runtime.ProfilePending, "profile_no_effect"): self.complete()

    def test_lost_apply_reply_reuses_same_durable_target(self):
        self.cli.lose_apply_response = True
        with self.assertRaises(ConnectionError): self.complete()
        target = copy.deepcopy(self.receipt["profile_progress"]["target_profile"])
        self.assertEqual(self.manager.profile, target)
        result = self.complete()
        self.assertEqual(self.cli.apply_calls, 1)
        self.assertEqual(result["profile_sha256"], engine.profile_sha256(target))
        self.assertEqual(self.manager.actual_reloads, 1)

    def test_official_property_stage_publishes_requested_build_before_reload(self):
        original = self.manager.property_profile["build"]["fingerprint"]
        self.complete()
        self.assertNotEqual(self.manager.property_profile["build"]["fingerprint"], original)
        self.assertEqual(self.cli.hide_calls, 1)
        self.assertEqual(self.manager.actual_reloads, 1)

    def test_lost_property_publication_reply_retries_without_profile_or_id_rotation(self):
        self.cli.lose_hide_response = True
        with self.assertRaises(ConnectionError): self.complete()
        self.assertEqual(self.cli.apply_calls, 1)
        self.complete()
        self.assertEqual(self.cli.apply_calls, 1)
        self.assertEqual(self.cli.hide_calls, 2)
        self.assertEqual(self.manager.actual_reloads, 1)

    def test_post_reload_build_mismatch_is_reported_partial_when_other_consumers_change(self):
        self.cli.skip_property_publish = True
        self.manager.property_profile["build"]["fingerprint"] = "wrong-runtime-fingerprint"
        result = self.complete()
        self.assertEqual(result["application_status"], "partial")
        self.assertIn(result["field_audit"]["fields"]["build.fingerprint"]["status"], {"unchanged", "mismatch"})
        self.assertEqual(self.manager.actual_reloads, 1)
        self.assertIsNotNone(runtime.load_applied(INSTANCE, state_dir=self.root / "state"))

    def test_staged_ram_alone_is_not_a_proven_consumer_change(self):
        old = copy.deepcopy(self.manager.profile)
        self.select_only({"memory.totalBytes": [old["memory"]["totalBytes"] + 4194304],
                          "memory.totalKiB": [old["memory"]["totalKiB"] + 4096]})
        self.broker.consumer_profile = old
        with self.assertRaisesRegex(runtime.ProfilePending, "profile_no_effect"): self.complete()
        self.assertEqual(self.manager.actual_reloads, 1)
        self.assertEqual(self.receipt["profile_progress"]["field_audit"]["effective_changes"], [])
        self.assertIsNone(runtime.load_applied(INSTANCE, state_dir=self.root / "state"))

    def test_ram_consumer_change_is_audited_without_claiming_physical_allocation(self):
        old = self.manager.profile["memory"]
        self.select_only({"memory.totalBytes": [old["totalBytes"] + 4194304],
                          "memory.totalKiB": [old["totalKiB"] + 4096]})
        result = self.complete()
        audit = result["field_audit"]
        self.assertIn("memory.totalBytes", result["effective_changes_verified"])
        self.assertEqual(audit["fields"]["memory.totalBytes"]["status"], "applied")
        self.assertIn("physical allocation", audit["scope"])
        state = runtime.load_applied(INSTANCE, state_dir=self.root / "state")
        self.assertEqual(state["audit_sha256"], engine.profile_sha256(audit))

    def test_audit_retains_failed_build_readback_and_valid_resource_change(self):
        self.cli.skip_property_publish = True
        result = self.complete()
        self.assertEqual(result["application_status"], "partial")
        audit = result["field_audit"]
        self.assertEqual(audit["fields"]["build.id"]["status"], "unchanged")
        self.assertIn("display.width", audit["effective_changes"])
        self.assertNotIn("build.id", audit["effective_changes"])

    def test_failed_reported_property_action_is_auditable_but_other_failures_remain_fatal(self):
        self.assertFalse(runtime._failed_action({"field": "ro.build.id", "ok": False}, allow_reported_property=True))
        self.assertTrue(runtime._failed_action({"field": "battery.level", "ok": False}, allow_reported_property=True))
        self.assertTrue(runtime._failed_action({"exit": 1}))
        self.assertTrue(runtime._failed_action({"exitCode": 1}))

    def test_official_property_warnings_include_both_exit_spellings_and_parent_false(self):
        raw = {"actions": [{"field": "ro.build.id", "ok": True, "actions": [{"exit": 1}, {"exitCode": 2}]},
                           {"field": "ro.build.fingerprint", "ok": False, "error": "failure", "actions": [{"exit": 0}]}]}
        failures = runtime._reported_property_failures(raw)
        self.assertEqual(failures, [{"property": "ro.build.id", "status": "failed", "failed_command_count": 2},
                                    {"property": "ro.build.fingerprint", "status": "failed", "failed_command_count": 0}])
        self.assertFalse(runtime._failed_action(raw, allow_reported_property=True))

    def test_property_warning_survives_matching_consumer_readback(self):
        self.cli.property_warning = True
        result = self.complete()
        self.assertEqual(result["field_audit"]["official_failure_counts"], {"properties": 1, "commands": 1})
        row = result["field_audit"]["fields"]["build.model"]
        self.assertEqual(row["observed"]["root_getprop"], row["requested"])
        self.assertEqual(row["status"], "partial")
        self.assertEqual(row["reported_failures"], ["ro.product.system.model"])

    def test_consumer_snapshot_cannot_cross_a_userspace_restart(self):
        original = self.broker.native_display_exec
        def changed(manager, args, **kwargs):
            result = original(manager, args, **kwargs)
            if args[:2] == ["sh", "-c"] and "XENOID_PROFILE_CONSUMERS" in args[2]:
                self.manager.epoch = "unexpected-restart"
            return result
        self.broker.native_display_exec = changed
        with self.assertRaisesRegex(runtime.ProfilePending, "consumer_epoch_changed"): self.complete()
        self.assertEqual(self.cli.apply_calls, 0)

    def test_property_slots_use_minimum_live_alias_room(self):
        capacities = runtime._property_capacities({"ro.build.fingerprint": "x" * 80,
            "ro.vendor.build.fingerprint": "x" * 63, "ro.product.model": "Xenoid QA"})
        self.assertEqual(capacities["build.fingerprint"], 63)
        self.assertEqual(capacities["build.model"], 63)
        self.assertNotIn("build.board", capacities)
        self.assertTrue(runtime._property_alias("model", "ro.product.system.model", "ro.product.model"))
        self.assertTrue(runtime._property_alias("product", "ro.product.vendor.name", "ro.product.name"))
        self.assertFalse(runtime._property_alias("model", "ro.product.system.device", "ro.product.model"))

    def test_oversized_fingerprint_pool_fails_capture_before_any_native_mutation(self):
        candidate = self.base["build"]["fingerprint"] + "x" * (80 - len(self.base["build"]["fingerprint"]))
        self.policy["fields"]["build.fingerprint"]["values"] = [candidate]
        self.policy_file.write_text(json.dumps(self.policy))
        before = copy.deepcopy(self.manager.identity)
        rng = random.getstate()
        with self.assertRaisesRegex(runtime.ProfilePending, "property_capacity_exceeded"):
            runtime.capture_plan(self.broker, self.manager, self.manager.binding, None,
                config_path=self.policy_file, resource_path=self.resource_file)
        self.assertEqual(random.getstate(), rng)
        self.assertEqual(self.manager.identity, before)
        self.assertEqual(self.manager.actual_reloads, 0)
        self.assertEqual(self.cli.apply_calls, 0)

    def test_feasibility_preview_does_not_consume_randomness_or_change_operation_state(self):
        rng = random.getstate(); previous = copy.deepcopy(self.manager.profile)
        plan = runtime.capture_plan(self.broker, self.manager, self.manager.binding, None,
            config_path=self.policy_file, resource_path=self.resource_file)
        self.assertEqual(random.getstate(), rng)
        self.assertEqual(self.manager.profile, previous)
        self.assertEqual(plan["preflight"], {"capacity_checked": True, "feasible": True, "validation_only": True})
        self.assertNotIn("operation_key", plan)

    def test_per_field_mixed_observations_preserve_each_surface(self):
        before = {"values": {"memory.totalBytes": {"procfs": 12, "app_sysinfo": 12}}}
        after = {"values": {"memory.totalBytes": {"procfs": 16, "app_sysinfo": 12}},
                 "observed_at": "test", "scope": "explicit test consumers"}
        audit = runtime.audit_consumers(before, after, {"memory": {"totalBytes": 16}}, ["memory.totalBytes"])
        row = audit["fields"]["memory.totalBytes"]
        self.assertEqual(row["status"], "partial")
        self.assertEqual(row["observed"], after["values"]["memory.totalBytes"])
        self.assertEqual(row["changed_consumers"], ["procfs"])
        self.assertEqual(audit["effective_changes"], ["memory.totalBytes"])

    def test_consumer_baseline_is_pinned_across_lost_response(self):
        self.cli.lose_apply_response = True
        with self.assertRaises(ConnectionError): self.complete()
        before = copy.deepcopy(self.receipt["profile_progress"]["consumer_before"])
        self.complete()
        self.assertEqual(self.receipt["profile_progress"]["consumer_before"], before)
        self.receipt["profile_progress"]["consumer_before"]["values"] = {}
        with self.assertRaisesRegex(runtime.ProfilePending, "consumer_baseline_changed"): self.complete()

    def test_completed_v26_receipt_resumes_without_fabricating_before_observations(self):
        self.complete()
        for key in ("consumer_before", "consumer_before_sha256", "field_audit", "audit_epoch"):
            self.receipt["profile_progress"].pop(key, None)
        path = self.root / "state" / (INSTANCE + ".json")
        old = json.loads(path.read_text())
        for key in ("field_audit", "audit_sha256", "application_status"): old.pop(key, None)
        path.write_text(json.dumps(old))
        result = self.complete()
        self.assertTrue(result["legacy_observation_unavailable"])
        self.assertEqual(result["effective_changes_verified"], [])
        self.assertEqual(self.cli.apply_calls, 1)
        self.assertEqual(self.manager.actual_reloads, 1)
        self.assertTrue(all(not row["before"] for row in result["field_audit"]["fields"].values()))






    def test_pending_marker_blocks_resource_overwrite_until_published(self):
        self.cli.lose_apply_response = True
        with self.assertRaises(ConnectionError): self.complete()
        pending = runtime.pending_for_binding(INSTANCE, "a" * 12, "b" * 64, state_dir=self.root / "state")
        self.assertEqual(pending["operation_key"], "round-8-natural-5")
        self.assertIsNone(runtime.pending_for_binding(INSTANCE, "a" * 12, "c" * 64, state_dir=self.root / "state"))
        self.complete()
        self.assertIsNone(runtime.pending_for_binding(INSTANCE, state_dir=self.root / "state"))

    def test_nested_official_action_failure_is_not_marked_verified(self):
        self.cli.fail_nested_action = True
        with self.assertRaisesRegex(runtime.ProfilePending, "official_apply_pending"): self.complete()
        self.assertFalse(self.receipt["profile_progress"].get("apply_verified", False))
        self.assertEqual(self.manager.actual_reloads, 0)

    def test_staged_cpu_battery_or_usb_mismatch_prevents_commit(self):
        for field in ("cpu_0_maximumFrequencyKhz", "battery_chargeCounterUah", "usb_product"):
            self.broker.bad_staged_field = field
            with self.subTest(field=field), self.assertRaisesRegex(runtime.ProfilePending, "staged_fields_mismatch"):
                self.complete()
            self.assertFalse(self.receipt["profile_progress"].get("completed", False))
        self.broker.bad_staged_field = None
        self.complete()

    def test_reload_timeout_resumes_same_restart_without_second_zygote_restart(self):
        self.manager.fail_reload_once = True
        with self.assertRaisesRegex(runtime.ProfilePending, "profile_reload_pending"): self.complete()
        self.assertEqual(self.manager.actual_reloads, 1)
        self.complete()
        self.assertEqual(self.manager.reload_calls, 2)
        self.assertEqual(self.manager.actual_reloads, 1)
        self.assertEqual(self.cli.apply_calls, 1)

    def test_policy_edit_after_capture_does_not_change_pending_target(self):
        self.policy["revision"] = 99
        self.policy_file.write_text(json.dumps(self.policy))
        self.complete()
        state = runtime.load_applied(INSTANCE, state_dir=self.root / "state")
        self.assertEqual(state["policy_revision"], 1)

    def test_no_policy_is_legacy_noop(self):
        receipt = copy.deepcopy(self.receipt); del receipt["profile_plan"]
        self.assertIsNone(runtime.complete(self.broker, self.manager, self.cli, receipt, self.store, "legacy", None, state_dir=self.root))
        self.assertEqual(self.cli.apply_calls, 0)

    def test_changed_container_fails_before_any_apply(self):
        self.manager.binding["container_id"] = "c" * 64
        with self.assertRaisesRegex(runtime.ProfilePending, "binding"): self.complete()
        self.assertEqual(self.cli.apply_calls, 0)

    def test_changed_drm_fails_before_any_apply(self):
        self.broker.drm = "a" * 32
        with self.assertRaisesRegex(runtime.ProfilePending, "native_drm"): self.complete()
        self.assertEqual(self.cli.apply_calls, 0)

    def test_saved_target_tampering_fails_on_retry(self):
        self.cli.lose_apply_response = True
        with self.assertRaises(ConnectionError): self.complete()
        self.receipt["profile_progress"]["target_profile"]["build"]["model"] = "tampered"
        with self.assertRaisesRegex(runtime.ProfilePending, "digest"): self.complete()

    def test_published_state_is_fenced_against_other_device_and_tampering(self):
        self.complete()
        self.assertIsNone(runtime.load_applied(INSTANCE, "wrong", "b" * 64, state_dir=self.root / "state"))
        path = self.root / "state" / (INSTANCE + ".json")
        data = json.loads(path.read_text()); data["profile"]["build"]["model"] = "other"; path.write_text(json.dumps(data))
        self.assertIsNone(runtime.load_applied(INSTANCE, state_dir=self.root / "state"))

    def test_missing_published_metadata_never_breaks_status(self):
        self.complete()
        path = self.root / "state" / (INSTANCE + ".json")
        original = json.loads(path.read_text())
        for key in ("policy_revision", "policy_sha256", "operation_key", "applied_at"):
            value = copy.deepcopy(original); del value[key]; path.write_text(json.dumps(value))
            self.assertIsNone(runtime.status_for_binding(INSTANCE, state_dir=self.root / "state"))
        path.write_text("[]")
        self.assertIsNone(runtime.status_for_binding(INSTANCE, state_dir=self.root / "state"))

    def test_completed_target_file_is_removed_but_other_operation_kept(self):
        other = self.root / "state" / "pending" / INSTANCE / "another.json"
        other.parent.mkdir(parents=True); other.write_text('{"unrelated":true}')
        result = self.complete()
        self.assertTrue(result["temporary_target_removed"])
        self.assertEqual(list(other.parent.iterdir()), [other])
        self.assertEqual(json.loads(other.read_text()), {"unrelated": True})

    def test_lost_cleanup_replay_reconstructs_and_removes_same_target(self):
        remove = runtime._remove_target
        with patch.object(runtime, "_remove_target", return_value=False):
            first = self.complete()
        self.assertFalse(first["temporary_target_removed"])
        self.assertEqual(len(list((self.root / "state" / "pending" / INSTANCE).glob("*.json"))), 1)
        second = self.complete()
        self.assertTrue(second["temporary_target_removed"])
        self.assertEqual(self.cli.apply_calls, 1)
        self.assertEqual(self.manager.actual_reloads, 1)
        self.assertFalse((self.root / "state" / "pending" / INSTANCE).exists())

    def test_missing_restart_receipt_never_triggers_another_restart(self):
        self.manager.fail_reload_once = True
        with self.assertRaises(runtime.ProfilePending): self.complete()
        self.manager.token = None
        with self.assertRaisesRegex(runtime.ProfilePending, "receipt_missing"): self.complete()
        self.assertEqual(self.manager.actual_reloads, 1)

    def abandoned_profile_target(self):
        target = copy.deepcopy(self.manager.profile)
        target["build"]["id"] += ".QA"
        target["build"]["incremental"] += ".q-old"
        target["build"]["fingerprint"] = "{brand}/{product}/{device}:{release}/{id}/{incremental}:{type}/{tags}".format(**target["build"])
        target["build"]["description"] = "{product}-{type} {release} {id} {incremental} {tags}".format(**target["build"])
        old_sha = engine.profile_sha256(target)
        self.manager.profile = target
        self.receipt["status"] = "pending"
        self.receipt["profile_progress"] = {"target_profile": target, "target_sha256": old_sha, "apply_started": True, "created_at": "2026-11-21T00:00:00+00:00"}
        for field in ("id", "incremental", "fingerprint", "description"):
            self.receipt["profile_plan"]["policy"]["fields"]["build." + field] = {"enabled": True, "values": [target["build"][field]]}
        self.receipt["profile_plan"]["policy_sha256"] = engine.profile_sha256(self.receipt["profile_plan"]["policy"])
        key = "round-8-natural-5"
        path = self.root / "state" / "pending" / INSTANCE / (hashlib.sha256(key.encode()).hexdigest() + ".json")
        runtime._atomic_json(path, target)
        runtime._atomic_json(self.root / "state" / (INSTANCE + ".pending.json"), {"schema": runtime.PENDING_SCHEMA, **self.manager.binding, "operation_key": key, "target_sha256": old_sha})
        return old_sha

    def v26_policy(self):
        policy = engine.default_policy([self.base], self.resources)
        for path, row in policy["fields"].items():
            if path.startswith("build.") or path.startswith("memory.") or path == "cpu.implementer" or path.endswith((".part", ".model")):
                row["values"] = []
        return policy

    def test_authorized_scope_rebase_keeps_completed_native_gen_and_restores_fixed_build(self):
        old_sha = self.abandoned_profile_target()
        native = copy.deepcopy(self.receipt["native_completion"])
        policy = self.v26_policy(); policy["revision"] = 2
        result = runtime.rebase_pending_profile(self.broker, self.manager, self.cli, self.receipt, self.store,
            "round-8-natural-5", policy, reason="administrator_json_only_scope_change")
        self.assertEqual(result["old_target_sha256"], old_sha)
        self.assertEqual(self.receipt["native_completion"], native)
        self.assertEqual(self.receipt["profile_progress"]["target_profile"]["build"], self.base["build"])
        self.assertEqual(self.cli.apply_calls, 0)
        self.complete()
        self.assertEqual(self.manager.profile["build"], self.base["build"])
        self.assertEqual(self.receipt["native_completion"], native)
        self.assertEqual(len(self.receipt["profile_scope_history"]), 1)
        self.assertEqual(self.manager.actual_reloads, 1)

    def test_scope_rebase_cannot_cross_started_postprocessing_or_reload(self):
        self.abandoned_profile_target()
        policy = engine.default_policy([self.base], self.resources)
        for extra in ({"post_cache_completed": {"gms": {"ok": True}}}, {"post_google_progress": {"registration_started": True}}):
            receipt = copy.deepcopy(self.receipt); receipt.update(extra)
            with self.assertRaisesRegex(runtime.ProfilePending, "not_allowed"):
                runtime.rebase_pending_profile(self.broker, self.manager, self.cli, receipt, self.store,
                    "round-8-natural-5", policy, reason="administrator_json_only_scope_change")
        receipt = copy.deepcopy(self.receipt); receipt["profile_progress"]["reload"] = {"requested": True}
        with self.assertRaisesRegex(runtime.ProfilePending, "not_allowed"):
            runtime.rebase_pending_profile(self.broker, self.manager, self.cli, receipt, self.store,
                "round-8-natural-5", policy, reason="administrator_json_only_scope_change")

    def test_repeated_scope_rebase_preserves_original_predecessor_until_file_migration(self):
        old_sha = self.abandoned_profile_target()
        policy = self.v26_policy(); policy["revision"] = 2
        args = (self.broker, self.manager, self.cli, self.receipt, self.store, "round-8-natural-5", policy)
        first = runtime.rebase_pending_profile(*args, reason="administrator_json_only_scope_change")
        repeated = runtime.rebase_pending_profile(*args, reason="administrator_json_only_scope_change")
        self.assertEqual(first, repeated)
        self.assertEqual(self.receipt["profile_scope_transition"]["old_target_sha256"], old_sha)
        self.assertEqual(len(self.receipt["profile_scope_history"]), 1)
        self.complete()
        self.assertEqual(self.manager.profile["build"], self.base["build"])
        self.assertEqual(self.cli.apply_calls, 1)

    def test_different_policy_cannot_rewrite_an_existing_scope_transition(self):
        self.abandoned_profile_target()
        policy = self.v26_policy(); policy["revision"] = 2
        runtime.rebase_pending_profile(self.broker, self.manager, self.cli, self.receipt, self.store,
            "round-8-natural-5", policy, reason="administrator_json_only_scope_change")
        before = copy.deepcopy(self.receipt["profile_scope_transition"])
        policy["revision"] = 3
        with self.assertRaisesRegex(runtime.ProfilePending, "already_recorded"):
            runtime.rebase_pending_profile(self.broker, self.manager, self.cli, self.receipt, self.store,
                "round-8-natural-5", policy, reason="administrator_json_only_scope_change")
        self.assertEqual(self.receipt["profile_scope_transition"], before)
        self.assertEqual(len(self.receipt["profile_scope_history"]), 1)

    def test_scope_rebase_does_not_replace_another_operations_marker(self):
        self.abandoned_profile_target()
        policy = engine.default_policy([self.base], self.resources)
        runtime.rebase_pending_profile(self.broker, self.manager, self.cli, self.receipt, self.store,
            "round-8-natural-5", policy, reason="administrator_json_only_scope_change")
        marker = self.root / "state" / (INSTANCE + ".pending.json")
        other = json.loads(marker.read_text()); other["operation_key"] = "other-operation"; runtime._atomic_json(marker, other)
        with self.assertRaisesRegex(runtime.ProfilePending, "another_profile_operation"):
            self.complete()
        self.assertEqual(self.cli.apply_calls, 0)


if __name__ == "__main__": unittest.main()
