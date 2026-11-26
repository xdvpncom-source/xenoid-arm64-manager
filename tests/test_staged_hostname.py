"""Actual native identity store/converge; only Android and daemon I/O are doubled."""
from __future__ import annotations

import copy
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / 'nativebundle/source/src/xenoid'
if not NATIVE.is_dir():
    NATIVE = ROOT / 'src/xenoid'
PROFILE_PATH = ROOT / 'nativebundle/source/examples/fingerprints/pixel-bluejay-android13.json'
if not PROFILE_PATH.is_file():
    PROFILE_PATH = ROOT / 'tests/fixtures/bluejay-profile.json'
package = types.ModuleType('staged_hostname_native_test')
package.__path__ = [str(NATIVE)]
sys.modules[package.__name__] = package


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


module(package.__name__ + '.config', NATIVE / 'config.py')
identity = module(package.__name__ + '.device_identity', NATIVE / 'device_identity.py')
PROFILE = json.loads(PROFILE_PATH.read_text())


class Runtime:
    def __init__(self, staged='android-owned-gen', current=''):
        self.cid = 'a' * 64
        self.staged = staged
        self.metadata = '0 0 600 1'
        self.current = current
        self.device_name = 'Pixel 6a TEST'
        self.calls = []
        self.setters = []
        self.reads = []
        self.setter_ok = True
        self.setter_publishes = True
        self.setter_exception = None
        self.stage_changes_after_setter = False
        self.fail_property_read = False
        self.fsid = '1' * 16

    def collect_persisted_device_identity(self):
        return {}

    def location_runtime_container_id(self):
        return self.cid

    def ensure_drm_identity(self):
        self.calls.append('drm')
        return {'ok': True}

    def apply_fingerprint(self, profile, regenerate_unique=True):
        self.calls.append('fingerprint')
        assert regenerate_unique is False
        return {'ok': True}

    def set_fingerprint_field(self, field, value):
        self.setters.append((field, value))
        if self.setter_exception is not None:
            raise self.setter_exception
        if not self.setter_ok:
            return {'ok': False}
        if field == 'net.hostname' and self.setter_publishes:
            self.current = value
        if field == 'device_name':
            self.device_name = value
        if self.stage_changes_after_setter:
            self.staged = 'unexpected-different-stage'
        return {'ok': True}

    def shared_protection_manager(self):
        return types.SimpleNamespace(statfs_fsid_status=lambda: {
            'ok': True, 'running': True, 'stagedValid': True,
            'published': True, 'staged': self.fsid, 'kernel': self.fsid})

    def docker_exec(self, args, **kwargs):
        self.reads.append(list(args))
        if args == ['getprop', 'net.hostname']:
            return {'ok': not self.fail_property_read, 'stdout': self.current}
        command = args[-1]
        if "stat -c '%u %g %a %h'" in command:
            if self.staged is None:
                return {'ok': False, 'stdout': ''}
            return {'ok': True, 'stdout': self.metadata + '\n' + self.staged + '\n'}
        if 'cat $d/statfs_fsid' in command:
            return {'ok': True, 'stdout': '\n'.join((self.fsid, self.staged, self.device_name)) + '\n'}
        if 'getprop net.hostname; settings get global device_name' in command:
            return {'ok': True, 'stdout': self.current + '\n' + self.device_name + '\n'}
        raise AssertionError(args)


class StagedHostname(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        state = Path(self.temp.name)
        self.context = types.SimpleNamespace(
            state_root=state, instance_id='12345678-1234-4234-8234-123456789abc',
            resource_tag='123456abcdef')
        self.store = identity.DeviceIdentityStore(self.context)
        self.store.initialize()
        _, boot = self.store.prepare_epoch('a' * 64)
        self.store.mark_applied(boot['containerEpoch'])
        self.before = copy.deepcopy(self.store.load())
        self.runtime = Runtime()

    def converge(self, **kwargs):
        return identity.converge_instance_identity(
            self.context, self.runtime, self.runtime, PROFILE, **kwargs)

    def test_applied_identity_restores_existing_gen_hostname_only(self):
        self.assertTrue(self.converge()['ok'])
        self.assertEqual(self.runtime.setters, [('net.hostname', 'android-owned-gen')])
        self.assertEqual(self.runtime.current, self.runtime.staged)
        self.assertEqual(self.store.load(), self.before)
        self.assertNotEqual(self.runtime.staged, identity.initial_runtime_identity(
            self.before['stable'], PROFILE['build']['model'])['hostname'])

    def test_existing_correct_hostname_is_idempotent_without_setter(self):
        self.runtime.current = self.runtime.staged
        for _ in range(2):
            self.assertTrue(self.converge()['ok'])
        self.assertEqual(self.runtime.setters, [])
        self.assertEqual(self.store.load(), self.before)

    def test_recreated_runtime_keeps_staged_name_and_stable_ids(self):
        self.runtime.cid = 'b' * 64
        self.assertTrue(self.converge()['ok'])
        self.assertEqual(self.runtime.current, 'android-owned-gen')
        after = self.store.load()
        self.assertEqual(after['stable'], self.before['stable'])
        self.assertNotEqual(after['active']['containerEpoch'], self.before['active']['containerEpoch'])
        self.assertEqual(after['pending'], None)
        self.assertTrue(self.converge()['ok'])
        self.assertEqual(len(self.runtime.setters), 1)

    def test_missing_stage_does_not_generate_or_publish_a_default(self):
        self.runtime.staged = None
        with patch.object(identity.DeviceIdentityStore, 'mark_applied') as mark:
            result = self.converge()
        self.assertFalse(result['ok'])
        self.assertEqual(result['error'], 'staged_hostname_invalid')
        self.assertEqual(self.runtime.setters, [])
        mark.assert_not_called()
        self.assertEqual(self.store.load()['stable'], self.before['stable'])

    def test_invalid_staged_values_are_rejected_before_setter(self):
        for value in ('', 'has spaces', '-bad', 'a\nsecond', 'x' * 64):
            with self.subTest(value=value):
                self.runtime.staged = value
                self.assertEqual(self.converge()['error'], 'staged_hostname_invalid')
        self.assertEqual(self.runtime.setters, [])

    def test_wrong_owner_mode_and_link_count_are_rejected(self):
        for meta in ('2000 0 600 1', '0 2000 600 1', '0 0 644 1', '0 0 600 2'):
            with self.subTest(metadata=meta):
                self.runtime.metadata = meta
                self.assertEqual(self.converge()['error'], 'staged_hostname_invalid')
        self.assertEqual(self.runtime.setters, [])

    def test_property_read_failure_does_not_write(self):
        self.runtime.fail_property_read = True
        self.assertEqual(self.converge()['error'], 'staged_hostname_readback_failed')
        self.assertEqual(self.runtime.setters, [])

    def test_negative_setter_never_commits_applied_boot(self):
        self.runtime.cid = 'b' * 64
        self.runtime.setter_ok = False
        with patch.object(identity.DeviceIdentityStore, 'mark_applied') as mark:
            result = self.converge()
        self.assertEqual(result['error'], 'staged_hostname_apply_failed')
        mark.assert_not_called()
        self.assertEqual(self.store.load()['active'], self.before['active'])
        self.assertIsNotNone(self.store.load()['pending'])

    def test_false_positive_ack_requires_actual_property_readback(self):
        self.runtime.setter_publishes = False
        with patch.object(identity.DeviceIdentityStore, 'mark_applied') as mark:
            result = self.converge()
        self.assertEqual(result['error'], 'staged_hostname_readback_failed')
        mark.assert_not_called()
        self.assertEqual(len(self.runtime.setters), 1)

    def test_changed_staged_target_after_setter_is_not_accepted(self):
        self.runtime.stage_changes_after_setter = True
        self.assertEqual(self.converge()['error'], 'staged_hostname_readback_failed')
        self.assertEqual(len(self.runtime.setters), 1)

    def test_unknown_ack_propagates_without_second_attempt_or_commit(self):
        self.runtime.setter_exception = TimeoutError('lost ACK')
        with patch.object(identity.DeviceIdentityStore, 'mark_applied') as mark:
            with self.assertRaises(TimeoutError):
                self.converge()
        self.assertEqual(len(self.runtime.setters), 1)
        mark.assert_not_called()

    def test_pending_gen_journals_keep_existing_owner(self):
        for name in (identity.REGENERATE_FILENAME, *identity._LEGACY_JOURNAL_MARKERS):
            with self.subTest(journal=name):
                path = self.context.state_root / name
                path.write_bytes(b'owned pending GEN')
                self.assertTrue(self.converge()['ok'])
                self.assertEqual(path.read_bytes(), b'owned pending GEN')
                path.unlink()
        self.assertEqual(self.runtime.setters, [])

    def test_pending_stable_target_is_not_republished_or_replaced(self):
        self.store.prepare_regeneration_stable('f' * 32)
        target = copy.deepcopy(self.store.load()['pendingStable'])
        self.assertTrue(self.converge()['ok'])
        self.assertEqual(self.store.load()['pendingStable'], target)
        self.assertEqual(self.runtime.setters, [])

    def test_legacy_non_bluejay_convergence_does_not_enter_new_path(self):
        profile = copy.deepcopy(PROFILE)
        profile['build']['device'] = 'raven'
        self.runtime.staged = None
        self.assertTrue(identity.converge_instance_identity(
            self.context, self.runtime, self.runtime, profile)['ok'])
        self.assertEqual(self.runtime.setters, [])
        self.assertEqual(self.runtime.reads, [])

    def test_explicit_legacy_stable_rotation_keeps_its_existing_semantics(self):
        self.assertTrue(self.converge(rotate_stable=True)['ok'])
        self.assertEqual(self.runtime.setters, [])
        self.assertNotEqual(self.store.load()['stable'], self.before['stable'])

    def test_fresh_native_seed_converges_then_late_guard_is_read_only(self):
        self.store.path.unlink()
        stable = self.store.initialize()['stable']
        expected = identity.initial_runtime_identity(stable, PROFILE['build']['model'])
        self.runtime.staged = expected['hostname']
        self.runtime.device_name = expected['device_name']
        self.runtime.fsid = expected['statfs_fsid']
        self.assertTrue(self.converge()['ok'])
        self.assertEqual(self.runtime.setters, [
            ('net.hostname', expected['hostname']), ('device_name', expected['device_name'])])
        self.assertEqual(self.store.load()['stable'], stable)
        self.assertEqual(self.runtime.current, expected['hostname'])

    def test_generated_shell_reads_regular_file_and_rejects_symlink_and_fifo(self):
        self.runtime.current = self.runtime.staged
        self.assertTrue(self.converge()['ok'])
        command = next(args[-1] for args in self.runtime.reads if args[0] == 'sh')
        root = Path(self.temp.name)
        target = root / 'hostname'
        target.write_text('android-real-file\n')
        tools = root / 'tools'
        tools.mkdir()
        marker = root / 'stat-called'
        tool = tools / 'stat'
        tool.write_text('#!/bin/sh\nprintf called > ' + shlex.quote(str(marker))
                        + '\nprintf "0 0 600 1\\n"\n')
        tool.chmod(0o755)
        command = command.replace('p=/data/local/tmp/xenoid-profile/hostname;',
                                  'p=' + shlex.quote(str(target)) + ';')
        env = dict(os.environ, PATH=str(tools) + os.pathsep + os.environ.get('PATH', ''))
        result = subprocess.run(['/bin/sh', '-c', command], env=env,
                                capture_output=True, timeout=2)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b'0 0 600 1\nandroid-real-file\n')
        self.assertTrue(marker.exists())
        target.unlink()
        for kind in ('symlink', 'fifo'):
            with self.subTest(kind=kind):
                marker.unlink(missing_ok=True)
                if kind == 'symlink':
                    backing = root / 'backing'
                    backing.write_text('android-not-admitted\n')
                    target.symlink_to(backing)
                else:
                    os.mkfifo(target)
                result = subprocess.run(['/bin/sh', '-c', command], env=env,
                                        capture_output=True, timeout=2)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(marker.exists())
                target.unlink()


if __name__ == '__main__':
    unittest.main()
