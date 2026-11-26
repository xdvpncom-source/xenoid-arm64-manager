"""Fresh boot configuration and shared panel/CLI creation behavior."""
import contextlib
import io
import json
from pathlib import Path
import runpy
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
FLEET = runpy.run_path(str(ROOT / "xenoid-fleet"))
PANEL = runpy.run_path(str(ROOT / "control/panel_device_create.py"))


class FakeNative:
    def __init__(self, config=None):
        self.config = config
        self.calls = []

    def registry(self):
        return {"1": {"ok": True}} if self.config is not None else {}

    def require(self, device, command):
        self.calls.append(command)

    def document(self, command, device):
        self.calls.append(command)
        if command == ["config", "show"]:
            return {"ok": True, "config": self.config}, 0
        if command == ["google-services", "status"]:
            return {"ok": True, "provider": "microg", "googleIdentityMode": "offline-seeded"}, 0
        raise AssertionError(command)

    def recreate(self, device):
        self.calls.append(["recreate"])


class FreshCreation(unittest.TestCase):
    def test_config_has_native_ids_and_separate_reported_vs_real_resources(self):
        cfg = json.loads((ROOT / FLEET["FRESH_CONFIG"]).read_text())
        self.assertEqual(cfg["extra_docker_args"], ["--cpus", "1", "--memory", "2147483648", "--memory-swap", "2684354560"])
        self.assertEqual(cfg["google_services_release"], FLEET["BLUEJAY_GOOGLE_RELEASE"])
        profile = cfg["cold_profile"]
        canonical = json.loads((ROOT / "nativebundle/source/examples/fingerprints/pixel-bluejay-android13.json").read_text())
        self.assertEqual(profile, canonical)
        contract = runpy.run_path(str(ROOT / "nativebundle/source/src/xenoid/cold_profile.py"))
        self.assertEqual(contract["normalize_cold_profile"](profile), profile)
        self.assertEqual(set(profile["ids"].values()), {"REGENERATE"})
        self.assertEqual(profile["usb"]["serial"], "REGENERATE")
        self.assertEqual(profile["memory"]["totalBytes"], 6 * 1024**3)
        self.assertEqual({key: profile["display"][key] for key in ("width", "height", "densityDpi", "physicalPpi")},
                         {"width": 1080, "height": 2400, "densityDpi": 420, "physicalPpi": 429})
        self.assertEqual(profile["display"]["supportedRefreshRatesHz"], [60])
        build = profile["build"]
        self.assertEqual(build["fingerprint"], "{brand}/{product}/{device}:{release}/{id}/{incremental}:{type}/{tags}".format(**build))
        self.assertEqual(build["model"], "Pixel 6a")

    def test_panel_calls_the_same_native_creation_path_without_stock_override(self):
        native = FakeNative()
        globals_ = FLEET["create_device"].__globals__
        with patch.dict(globals_, {"qa_reconcile": lambda *a, **k: self.fail("panel must finalize QA after releasing its lock")}):
            with contextlib.redirect_stdout(io.StringIO()):
                PANEL["create"](FLEET, native, "1", None, None, None)
        self.assertEqual(native.calls[:2], [["init", "--config", str(ROOT / FLEET["FRESH_CONFIG"])], ["up"]])
        self.assertEqual(native.calls[-1], ["google-services", "status"])
        self.assertFalse(any("--image" in command for command in native.calls))

    def test_legacy_data_is_not_relabelled_or_removed_by_creation(self):
        native = FakeNative({"google_services_provider": "microg"})
        with self.assertRaisesRegex(FLEET["FleetError"], "explicitly delete"):
            FLEET["create_device"](native, "1", SimpleNamespace(no_google_services=False), None, None)
        self.assertEqual(native.calls, [["config", "show"]])

    def test_if_missing_reuses_matching_instance_and_still_converges(self):
        native = FakeNative(json.loads((ROOT / FLEET["FRESH_CONFIG"]).read_text()))
        with patch.dict(FLEET["create_device"].__globals__, {"qa_reconcile": lambda *a, **k: None}):
            FLEET["create_device"](native, "1", SimpleNamespace(no_google_services=False, if_missing=True), None, None)
        self.assertEqual(native.calls, [["config", "show"], ["up"]])

    def test_capacity_reserves_real_ram_and_cpu(self):
        calculate = FLEET["capacity_report"]
        small = calculate(memory_total_bytes=8104976384, cpu_count=4, swap_total_bytes=4 * 1024**3)
        large = calculate(memory_total_bytes=int(15.212 * 1024**3), cpu_count=8, swap_total_bytes=8 * 1024**3)
        self.assertEqual(small["recommended_count"], 3)
        self.assertEqual(large["recommended_count"], 7)
        self.assertEqual(small["reserve"]["memory_bytes"], 1024**3)
        self.assertEqual(large["reserve"]["memory_bytes"], 1024**3)
        self.assertEqual(large["reserve"]["memory_policy"], "fixed-1gib")
        self.assertEqual(calculate(memory_total_bytes=2 * 1024**3, cpu_count=1, swap_total_bytes=0)["recommended_count"], 0)

    def test_measured_fleet_has_seventy_complete_slots_without_ram_overcommit(self):
        calculate = FLEET["capacity_report"]
        # Actual Linux MemTotal from the 22-host, 69-device census.
        hosts = [(4, 8104976384)] * 21 + [(8, 16333709312)]
        counts = []
        for cpus, ram in hosts:
            report = calculate(memory_total_bytes=ram, cpu_count=cpus,
                               swap_total_bytes=8 * 1024**3)
            count = report["recommended_count"]
            counts.append(count)
            self.assertLessEqual(count + report["reserve"]["cpu_count"], cpus)
            self.assertLessEqual(count * report["per_device"]["memory_bytes"]
                                 + report["reserve"]["memory_bytes"], ram)
        self.assertEqual(counts, [3] * 21 + [7])
        self.assertEqual(sum(counts), 70)

    def test_capacity_limits_cpu_and_physical_ram_independently_of_swap(self):
        calculate = FLEET["capacity_report"]
        cpu_limited = calculate(memory_total_bytes=64 * 1024**3, cpu_count=4,
                                swap_total_bytes=0)
        ram_limited = calculate(memory_total_bytes=4 * 1024**3, cpu_count=64,
                                swap_total_bytes=1024 * 1024**3)
        self.assertEqual(cpu_limited["recommended_count"], 3)
        self.assertEqual(ram_limited["recommended_count"], 1)
        self.assertFalse(cpu_limited["swap_capacity_ok"])
        self.assertTrue(ram_limited["swap_capacity_ok"])

    def test_larger_explicit_host_reserve_remains_available(self):
        calculate = FLEET["capacity_report"]
        report = calculate(memory_total_bytes=16333709312, cpu_count=8,
                           swap_total_bytes=8 * 1024**3, memory_reserve_mib=2048)
        self.assertEqual(report["recommended_count"], 6)
        self.assertEqual(report["reserve"]["memory_bytes"], 2 * 1024**3)
        self.assertEqual(report["reserve"]["memory_policy"], "explicit")

    def test_host_reserve_cannot_be_removed_or_made_smaller_than_one_gib(self):
        calculate = FLEET["capacity_report"]
        for reserve in (0, 1023, True, 1024.0):
            with self.subTest(memory_reserve_mib=reserve), self.assertRaises(FLEET["FleetError"]):
                calculate(memory_total_bytes=16333709312, cpu_count=8,
                          swap_total_bytes=0, memory_reserve_mib=reserve)

    def test_explicit_capacity_override_is_visible_and_not_a_silent_clamp(self):
        report = FLEET["capacity_report"](memory_total_bytes=8104976384, cpu_count=4, swap_total_bytes=0)
        self.assertEqual(FLEET["capacity_warnings"](report, 4), ["cpu_memory_overcommit", "swap_capacity_below_requested_limits"])
        with self.assertRaises(FLEET["FleetError"]):
            FLEET["capacity_report"](memory_total_bytes=8104976384, cpu_count=4, swap_total_bytes=0, cpu_reserve=0)
        override = FLEET["capacity_report"](memory_total_bytes=int(15.212 * 1024**3), cpu_count=8, swap_total_bytes=8 * 1024**3, memory_reserve_mib=1024)
        self.assertEqual(override["recommended_count"], 7)
        self.assertEqual(override["reserve"]["memory_policy"], "explicit")


if __name__ == "__main__":
    unittest.main()
