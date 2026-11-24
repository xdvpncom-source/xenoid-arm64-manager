"""Fresh Bluejay logical/pacing contracts and native-input scenario regressions."""
from copy import deepcopy
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "qa"))
import qa_device_profile as profiles
import qa_fresh6a as fresh
import qa_resource_runtime as resources


def profile():
    value = json.loads((ROOT / "tests/fixtures/native-profile-template.json").read_text())
    spec = fresh.defaults()
    value["build"].update(spec["build"])
    value["display"].update(spec["display"], supportedRefreshRatesHz=[60],
        modes=[{"id": 1, "width": 1080, "height": 2400, "refreshRateHz": 60}])
    value["input"].update(x={"minimum": 0, "maximum": 1079}, y={"minimum": 0, "maximum": 2399})
    for key in ("capacityMah", "minimumCapacityMah", "chargeFullDesignUah", "chargeFullUah"):
        value["battery"][key] = spec["battery"][key]
    value["battery"]["plugged"] = 0
    value["battery"]["status"] = 3
    value["battery"]["chargeCounterUah"] = value["battery"]["chargeFullUah"] * value["battery"]["level"] // 100
    return value


class FreshPolicyTests(unittest.TestCase):
    def test_authentic_release_preserved_across_multiple_generations(self):
        base = profile(); policy = profiles.default_policy(base)
        self.assertNotIn("display_random", policy)
        previous = base
        for number in range(8):
            ids = deepcopy(base["ids"]); ids["android_id"] = f'{number + 1:016x}'
            result = profiles.generate_profile(base, policy, "cycle-" + str(number), previous=previous, native_ids=ids)
            got = result["profile"]
            for key in ("build", "display", "input", "cpu", "memory", "storage", "cameras", "sensors"):
                self.assertEqual(got[key], base[key], key)
            self.assertEqual(got["ids"], ids)
            self.assertEqual(got["usb"]["serial"], ids["serial"])
            battery = got["battery"]
            self.assertEqual((battery["capacityMah"], battery["minimumCapacityMah"]), (4410, 4306))
            self.assertEqual(battery["chargeCounterUah"], battery["chargeFullUah"] * battery["level"] // 100)
            self.assertEqual(battery["status"], 2 if battery["plugged"] else 3)
            self.assertNotEqual(battery["level"], previous["battery"]["level"])
            self.assertEqual(got["thermal"]["zone1"], base["thermal"]["zone1"])
            self.assertFalse(result["qa_build_metadata"])
            previous = got

    def test_all_declared_leaves_remain_visible(self):
        base = profile(); got = profiles.default_policy(base)
        self.assertEqual(set(got["fields"]), {path for path, value in profiles._leaves(base)})
        self.assertTrue(got["fields"]["ids.imei"]["enabled"])
        self.assertEqual(got["fields"]["ids.imei"]["values"], [])
        for path in ("build.fingerprint", "battery.capacityMah", "battery.plugged", "battery.status", "display.width", "memory.totalBytes", "thermal.zone1.temp"):
            self.assertFalse(got["fields"][path]["enabled"])
            self.assertEqual(got["fields"][path]["values"], [])

    def test_incoherent_bluejay_release_fails_without_silent_conversion(self):
        for path, bad in (("fingerprint", "bad"), ("host", "build1"), ("date_utc", "1716817354")):
            base = profile(); base["build"][path] = bad
            with self.assertRaises(ValueError):
                profiles.default_policy(base)

    def test_legacy_raven_defaults_and_saved_policy_unchanged(self):
        base = json.loads((ROOT / "tests/fixtures/native-profile-template.json").read_text())
        got = profiles.default_policy(base, {"enabled": False})
        self.assertIn("display_random", got)
        self.assertIn("Xenoid QA A", got["fields"]["build.model"]["values"])
        bluejay = profile(); explicit = {"schema": profiles.SCHEMA, "enabled": True, "revision": 8, "fields": {}}
        self.assertEqual(profiles.validate_policy(explicit, [bluejay]), explicit)

    def test_incompatible_saved_random_policy_rejected_without_rewriting(self):
        base = profile(); good = profiles.default_policy(base)
        fresh.validate_policy_contract(good, base)
        for path, value in (("build.model", "Xenoid QA A"), ("display.width", 204), ("thermal.zone1.temp", 47000), ("battery.plugged", 1), ("battery.status", 2)):
            bad = deepcopy(good); bad["fields"][path] = {"enabled": True, "values": [value]}
            original = deepcopy(bad)
            with self.assertRaisesRegex(ValueError, "fixed field"):
                fresh.validate_policy_contract(bad, base)
            self.assertEqual(bad, original)

    def test_fresh_runtime_preflight_rejects_incompatible_before_native_gen(self):
        import qa_profile_runtime as runtime
        base = profile(); bad = profiles.default_policy(base)
        bad["fields"]["build.fingerprint"] = {"enabled": True, "values": ["fake"]}
        with self.assertRaisesRegex(runtime.ProfilePending, "fresh6a_profile_policy_incompatible"):
            runtime._fresh_contract({"render_fps": 15}, base, bad)
        # Historical/legacy receipts retain their previous code path.
        runtime._fresh_contract({}, base, bad)

    def test_policy_migration_keeps_fresh_fixed_display_without_legacy_random_group(self):
        base = profile(); policy = profiles.default_policy([base])
        resource = json.loads((ROOT / "qa/qa-resources.json").read_text())
        migrated = profiles.migrate_policy(policy, [base], resource)
        self.assertEqual(migrated["revision"], policy["revision"] + 1)
        self.assertEqual(migrated["fields"], policy["fields"])
        self.assertNotIn("display_random", migrated)
        self.assertNotIn("display_presets", migrated)
        generated = profiles.generate_profile(base, migrated, "migrated-fresh", previous=base)
        self.assertEqual(generated["profile"]["display"], base["display"])
        self.assertEqual(generated["profile"]["build"], base["build"])

    def test_default_resources_separate_render_cap_from_advertised_hz(self):
        config = resources.validate_config(json.loads((ROOT / "qa/qa-resources.json").read_text()))
        self.assertEqual((config["fps"], config["render_fps"], config["scale"], config["dpi"]), (60, 15, None, 420))
        self.assertIsNone(resources.validate_config({})["render_fps"])
        for delta in ({"fps": 15}, {"scale": "minimum"}, {"render_fps": True}, {"dpi": 72}):
            with self.assertRaises(ValueError):
                resources.validate_config({**config, **delta})


class ActualResourceTests(unittest.TestCase):
    def fixture(self, root, *, cid='a' * 64):
        proc = root / 'proc'; cg = root / 'cgroup'; path = cg / ('docker-' + cid + '.scope')
        (proc / '123').mkdir(parents=True); path.mkdir(parents=True)
        (proc / '123/cgroup').write_text('0::/docker-' + cid + '.scope\n')
        for name, text in {'cpu.max': '100000 100000', 'memory.max': '2147483648', 'memory.swap.max': '536870912'}.items():
            (path / name).write_text(text)
        return proc, cg, path, {'Id': cid, 'State': {'Pid': 123}}

    def test_real_limit_drift_rejected_even_if_docker_desired_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            proc, cg, path, owned = self.fixture(Path(directory))
            self.assertEqual(fresh.verify_actual_caps(owned, proc_root=proc, cgroup_root=cg)["cpu_cores"], 1)
            for name, text in {'cpu.max': '200000 100000', 'memory.max': '4294967296', 'memory.swap.max': '0'}.items():
                old = (path / name).read_text(); (path / name).write_text(text)
                with self.assertRaises(RuntimeError):
                    fresh.verify_actual_caps(owned, proc_root=proc, cgroup_root=cg)
                (path / name).write_text(old)
            (proc / '123/cgroup').write_text('0::/foreign.scope\n')
            with self.assertRaisesRegex(RuntimeError, 'owner'):
                fresh.verify_actual_caps(owned, proc_root=proc, cgroup_root=cg)

    def test_native_bluejay_rejects_legacy_settings_before_any_mutation(self):
        owned = {"Id": "a" * 64, "State": {"Running": True}}
        manager = types.SimpleNamespace(cfg=types.SimpleNamespace(cold_profile=profile()),
            _owned_container_record=lambda **kwargs: (owned, None))
        with patch.object(resources, "ensure_host_vm_guard") as guard, \
             patch.object(resources, "pending_profile") as pending, \
             patch.object(resources, "apply_metrics") as metrics:
            with self.assertRaisesRegex(ValueError, "separate native"):
                resources.reconcile_owned("phone-1", resources.validate_config({}), manager)
        guard.assert_not_called(); pending.assert_not_called(); metrics.assert_not_called()

    def test_render_resource_path_never_calls_legacy_hwc_or_wm_setters(self):
        config = resources.validate_config(json.loads((ROOT / 'qa/qa-resources.json').read_text()))
        owned = {'Id': 'a' * 64, 'State': {'Running': True, 'StartedAt': 'boot'}}
        manager = types.SimpleNamespace(context=types.SimpleNamespace(instance_id='test-instance', resource_tag='0123456789ab'),
            _owned_container_record=lambda **kwargs: (owned, None))
        broker = Mock(); broker.set_frame_rate.side_effect = AssertionError('legacy HWC called')
        broker.owned_manager = lambda: contextlib.nullcontext(manager)
        with tempfile.TemporaryDirectory() as directory, patch.object(resources, 'STATE', Path(directory)), \
             patch.object(resources, 'pending_profile', return_value=False), \
             patch.object(resources, 'effective_config', side_effect=lambda c, m, o: c), \
             patch.object(resources, 'ensure_host_vm_guard', return_value={'ok': True}), \
             patch.object(resources, 'ensure_memory_headroom', return_value={'ok': True}) as headroom, \
             patch.object(resources, 'apply_metrics', side_effect=AssertionError('WM setter called')), \
             patch.object(fresh, 'verify_runtime', return_value={'ok': True, 'render_fps': 15}) as verify:
            got = resources.reconcile_owned('phone-1', config, manager, force=True, broker=broker)
        self.assertTrue(got['ok']); self.assertEqual(got['display']['render_fps'], 15)
        headroom.assert_called_once_with(manager, owned['Id'], reserve_mib=256)
        broker.set_frame_rate.assert_not_called(); verify.assert_called_once()


class NativeReadbackTests(unittest.TestCase):
    def setUp(self):
        self.cid = 'a' * 64
        self.owned = {'Id': self.cid, 'Image': 'sha256:' + 'b' * 64,
            'State': {'Running': True, 'Pid': 123, 'StartedAt': 'boot'},
            'HostConfig': {'NanoCpus': 1000000000, 'Memory': 2147483648, 'MemorySwap': 2684354560}}
        self.manager = types.SimpleNamespace(_owned_container_record=lambda **kwargs: (self.owned, None))
        self.expected = {'ro.expected.' + str(index): str(index) for index in range(191)}
        self.manifest = {'schema': 'xenoid.fresh-bluejay-render/v1', 'advertised_hz': 60, 'render_fps': 15,
            'display': {'width': 1080, 'height': 2400, 'densityDpi': 420, 'physicalPpi': 429},
            'properties': fresh.PACING, 'swiftshader_threads': 1,
            'regular_config_files': list(fresh.CONFIG_FILES), 'artifactSha256': {path: 'c' * 64 for path in
            ('/system/lib64/libgui.so', '/system/lib64/libgui_pacer.so', '/vendor/lib64/hw/vulkan.pastel.so')}}
        self.manifest['artifactSha256'].update({path:fresh.CONFIG_SHA256 for path in fresh.CONFIG_FILES})
        self.responses = {
            ('cat', '/data/local/tmp/xenoid-profile/effective.json'): json.dumps(profile()),
            ('getprop',): '\n'.join('[' + key + ']: [' + value + ']' for key, value in self.expected.items()),
            ('wm', 'size'): 'Physical size: 1080x2400', ('wm', 'density'): 'Physical density: 420',
            ('dumpsys', 'SurfaceFlinger', '--latency'): '16666666\n',
        }
        for key, value in fresh.PACING.items(): self.responses[('getprop', key)] = value
        self.commands = []
        def read(manager, argv, **kwargs):
            self.commands.append(tuple(argv))
            if tuple(argv) == ('cat', fresh.RENDER_MANIFEST): result = json.dumps(self.manifest)
            elif argv[0] == 'sha256sum': result = self.manifest['artifactSha256'][argv[1]] + '  ' + argv[1]
            else: result = self.responses[tuple(argv)]
            return types.SimpleNamespace(stdout=result.encode())
        self.broker = types.SimpleNamespace(container_id=self.cid, native_display_exec=read,
            native_interaction_snapshot=lambda manager, deadline: {'ready': True, 'mapping': {'logical_size': [1080, 2400]}})
        self.native_module = types.ModuleType('xenoid.cold_profile')
        self.native_module.template_from_live = deepcopy
        self.native_module.expected_properties = lambda value: self.expected

    def verify(self):
        with patch.object(fresh, 'verify_actual_caps', return_value={'cpu_cores': 1}), \
             patch.object(fresh, 'verify_config_files'), \
             patch.dict(sys.modules, {'xenoid': types.ModuleType('xenoid'), 'xenoid.cold_profile': self.native_module}):
            return fresh.verify_runtime(self.broker, self.manager, deadline=None)

    def test_pacing_readback_preserves_advertised60_and_has_no_setters(self):
        got = self.verify()
        self.assertEqual((got['fps'], got['render_fps'], got['build_properties_verified']), (60, 15, 191))
        self.assertFalse(got['changed']); self.assertFalse(got['presentation_cadence_measured'])
        self.assertNotIn('proxy', got)
        self.assertTrue(all(argv[0] in {'cat', 'getprop', 'wm', 'dumpsys', 'sha256sum'} for argv in self.commands))
        self.assertTrue(all(len(argv) == 2 for argv in self.commands if argv[0] == 'wm'))

    def test_live_native_ids_use_real_cold_template_contract_without_mutating_them(self):
        native_path = ROOT / 'nativebundle/source/src/xenoid/cold_profile.py'
        spec = importlib.util.spec_from_file_location('fresh6a_native_contract', native_path)
        native = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(native)
        live = json.loads((ROOT / 'nativebundle/source/examples/fingerprints/pixel-bluejay-android13.json').read_text())
        live['ids'].update(android_id='1234567890abcdef', serial='TESTNATIVE01',
                           imei='351804610000018', imeisv='01',
                           boot_id='12345678-1234-4234-8234-123456789abc')
        live['usb']['serial'] = live['ids']['serial']
        before = deepcopy(live)
        with self.assertRaisesRegex(ValueError, 'cold_profile_must_not_contain_device_ids'):
            native.expected_properties(live)
        self.native_module = native
        self.expected = native.expected_properties(native.template_from_live(live))
        self.responses[('cat', '/data/local/tmp/xenoid-profile/effective.json')] = json.dumps(live)
        self.responses[('getprop',)] = '\n'.join('[' + k + ']: [' + v + ']' for k, v in self.expected.items())
        result = self.verify()
        self.assertTrue(result['ok'])
        self.assertEqual(result['build_properties_verified'], 191)
        self.assertEqual(live, before)
        self.assertEqual(native.template_from_live(live)['ids']['android_id'], native.SENTINEL)
        self.assertEqual(live['ids']['android_id'], '1234567890abcdef')

    def test_wrong_geometry_property_artifact_contract_or_actual_caps_never_pass(self):
        for command, wrong in [(('wm', 'density'), 'Physical density: 420\nOverride density: 72'),
                                (('dumpsys', 'SurfaceFlinger', '--latency'), '66666666\n'),
                                (('getprop', 'debug.gui.frame_pacer_fps'), '0')]:
            old = self.responses[command]; self.responses[command] = wrong
            with self.assertRaises(RuntimeError): self.verify()
            self.responses[command] = old
        self.manifest['render_fps'] = 60
        with self.assertRaisesRegex(RuntimeError, 'manifest'): self.verify()
        self.manifest['render_fps'] = 15
        self.responses[('getprop',)] = '[ro.expected.0]: [bad]'
        with self.assertRaisesRegex(RuntimeError, 'build_properties'): self.verify()


class SwiftConfigTests(unittest.TestCase):
    def test_second_regular_copy_must_equal_exact_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);assets={p:fresh.CONFIG_SHA256 for p in fresh.CONFIG_FILES}
            for name in fresh.CONFIG_FILES:
                p=root/name.lstrip('/');p.parent.mkdir(parents=True,exist_ok=True)
                p.write_bytes(b'[Processor]\nThreadCount=1\n');p.chmod(0o644)
            original=Path.lstat
            def root_owned(path):
                info=original(path);values=list(info);values[4]=values[5]=0
                return os.stat_result(values)
            with patch.object(Path,'lstat',root_owned):
                fresh.verify_config_files(root,assets)
                (root/'vendor/sw.ini').write_bytes(b'[Processor]\nThreadCount=2\n')
                with self.assertRaisesRegex(RuntimeError,'config_changed'):
                    fresh.verify_config_files(root,assets)
                (root/'vendor/sw.ini').unlink();(root/'vendor/sw.ini').symlink_to('etc/SwiftShader.ini')
                with self.assertRaisesRegex(RuntimeError,'regular_file'):
                    fresh.verify_config_files(root,assets)
    def test_manifest_cannot_substitute_two_different_hashes(self):
        assets={p:fresh.CONFIG_SHA256 for p in fresh.CONFIG_FILES};assets['/vendor/sw.ini']='0'*64
        with self.assertRaisesRegex(RuntimeError,'manifest_hash'):
            fresh.verify_config_files(Path('/not-read'),assets)


if __name__ == '__main__':
    unittest.main()
