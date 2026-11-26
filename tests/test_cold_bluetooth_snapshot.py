"""Cold boot Bluetooth baselines use the actual, proven native address.

Run the production snapshot method and the production read-only shell proof.
No Android component, generated identity, or reservation is changed by tests.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import types
from collections.abc import Mapping
from typing import Any
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'nativebundle/source/src/xenoid/backend.py'
TREE = ast.parse(SOURCE.read_text())
CLASS = next(n for n in TREE.body if isinstance(n, ast.ClassDef) and n.name == 'RuntimeManager')
METHODS = [n for n in CLASS.body if isinstance(n, ast.FunctionDef)
           and n.name in {'regeneration_snapshot', '_cold_bluetooth_baseline_matches'}]
PACKAGE = '_cold_bluetooth_snapshot_test'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class IdentityError(RuntimeError):
    def __init__(self, code, detail):
        super().__init__(detail); self.code = code


class IdentityStore:
    def __init__(self, context): self.context = context
    def load(self): return copy.deepcopy(self.context.identity)


class LocationStore:
    def __init__(self, *_args): pass
    def load(self): return {'simEpoch': 'f' * 32, 'active': {'profileDigest': 'e' * 64}}


class GoogleStore:
    def __init__(self, *_args): pass
    def load(self):
        return {'provider': 'none', 'release': 'none', 'specSha256': 'b' * 64,
                'dataCompatibilitySha256': 'c' * 64}


PACKAGE_MODULE = types.ModuleType(PACKAGE)
PACKAGE_MODULE.__path__ = []
LOCATION_MODULE = types.ModuleType(PACKAGE + '.location')
LOCATION_MODULE.LocationStateStore = LocationStore
NS = {'__package__': PACKAGE, 'Any': Any, 'Mapping': Mapping, 're': re, 'shlex': shlex,
      'IdentityError': IdentityError, 'DeviceIdentityStore': IdentityStore,
      'container_epoch': lambda cid: 'owned-container-epoch',
      '_RUNTIME_SCHEMA_LABEL': 'schema', '_RUNTIME_INPUT_LABEL': 'input', '_RUNTIME_BOOT_INPUT_LABEL': 'boot',
      '_SHA256_PATTERN': re.compile(r'[0-9a-f]{64}'), 'GoogleBindingStore': GoogleStore,
      'GoogleServicesError': RuntimeError, 'PROVIDER_NONE': 'none'}
exec(compile(ast.Module(body=METHODS, type_ignores=[]), str(SOURCE), 'exec'), NS)


class Model:
    regeneration_snapshot = NS['regeneration_snapshot']
    _cold_bluetooth_baseline_matches = NS['_cold_bluetooth_baseline_matches']

    def __init__(self, *, flag='null', address='02:11:22:33:44:55', cold=True, proof=True):
        self.cid = '1' * 64
        self.image = 'sha256:' + '2' * 64
        self.cfg = types.SimpleNamespace(cold_profile={'build': {'device': 'bluejay'}} if cold else None,
                                         network_dns_servers=[], google_services_provider='none')
        self.context = types.SimpleNamespace(state_root=Path('/not-used'), identity={
            'stable': {'androidId': '0123456789abcdef', 'serial': 'TESTSERIAL',
                       'imei': '351804610000001', 'imeisv': '01'},
            'active': {'bootId': '00000000-0000-4000-8000-000000000001',
                       'containerEpoch': 'owned-container-epoch'}})
        self.lease = object(); self.calls = []; self.proof = proof
        self.surfaces = {'settings get secure bluetooth_address': address,
                         'settings get secure bluetooth_addr_valid': flag,
                         'settings get global device_name': 'Pixel 6a test',
                         'getprop net.hostname': 'android-test',
                         'cat /data/local/tmp/xenoid-profile/statfs_fsid 2>/dev/null || true': '1' * 16,
                         'cat /data/local/tmp/xenoid-profile/drm_device_unique_id 2>/dev/null || true': '2' * 32}

    def ensure_instance_lease(self): pass
    def _owned_container_record(self, **_kwargs):
        return {'Id': self.cid, 'Image': self.image, 'State': {'Running': True}}, None
    def _inspect_docker_object(self, _kind, _identity):
        return {'Id': self.image, 'Config': {'Labels': {'schema': '1', 'input': 'a' * 64, 'boot': 'b' * 64}}}, None
    def _container_matches_lease(self, *_args, **_kwargs): return True
    def assert_regeneration_single_user(self): pass
    def daemon_client(self, **_kwargs):
        return types.SimpleNamespace(bootstrap_status=lambda **_kw: {'ok': True, 'runtimeEpoch': 'd' * 64})
    def shared_protection_status(self):
        return {'ok': True, 'engineId': 'engine-test', 'expectedDigest': 'e' * 64}
    def _regeneration_digest(self, value): return digest(value)
    def docker_exec(self, args, **_kwargs):
        self.calls.append(args)
        command = args[-1]
        if command in self.surfaces:
            return {'ok': True, 'stdout': self.surfaces[command]}
        assert command.startswith('set -eu; d=/data/local/tmp/xenoid-profile;')
        return {'ok': self.proof, 'stdout': 'cold_bluetooth_baseline_verified\n' if self.proof else ''}
    def snapshot(self):
        with patch.dict(sys.modules, {PACKAGE: PACKAGE_MODULE, PACKAGE + '.location': LOCATION_MODULE}), \
             patch.dict(NS, {'stable_identity_digest': digest}):
            return self.regeneration_snapshot()


class ColdSnapshotTests(unittest.TestCase):
    def test_actual_snapshot_retains_proven_cold_address_with_missing_validity_flag(self):
        expected = Model(flag='1').snapshot()
        for flag in ('', 'null'):
            with self.subTest(flag=flag):
                model = Model(flag=flag)
                result = model.snapshot()
                self.assertEqual(result['bluetoothAddress'], model.surfaces['settings get secure bluetooth_address'])
                self.assertEqual(result['stableDigest'], digest(model.context.identity['stable']))
                self.assertEqual(result['bootId'], model.context.identity['active']['bootId'])
                self.assertEqual(result['deviceName'], 'Pixel 6a test')
                self.assertEqual(result, expected)
                self.assertTrue(any('cold_bluetooth_baseline_verified' in c[-1] for c in model.calls))
                self.assertFalse(any('setprop' in c[-1] or ' put ' in c[-1] for c in model.calls))

    def test_normal_initialized_flag_uses_same_address_without_extra_proof(self):
        model = Model(flag='1'); result = model.snapshot()
        self.assertEqual(result['bluetoothAddress'], model.surfaces['settings get secure bluetooth_address'])
        self.assertFalse(any('cold_bluetooth_baseline_verified' in c[-1] for c in model.calls))

    def test_explicit_invalid_or_unsupported_flags_do_not_get_cold_exception(self):
        for flag in ('0', 'false', 'true', '2', 'unknown'):
            model = Model(flag=flag)
            self.assertEqual(model.snapshot()['bluetoothAddress'], '02:00:00:00:00:00')
            self.assertFalse(any('cold_bluetooth_baseline_verified' in c[-1] for c in model.calls))

    def test_legacy_runtime_missing_flag_is_unchanged(self):
        model = Model(cold=False)
        self.assertEqual(model.snapshot()['bluetoothAddress'], '02:00:00:00:00:00')
        self.assertFalse(any('cold_bluetooth_baseline_verified' in c[-1] for c in model.calls))

    def test_missing_wrong_or_unreadable_native_proof_cannot_admit_address(self):
        model = Model(proof=False)
        self.assertEqual(model.snapshot()['bluetoothAddress'], '02:00:00:00:00:00')

    def test_malformed_and_placeholder_address_cannot_use_missing_flag_exception(self):
        for address in ('null', '', 'not a mac', '02:00:00:00:00:00', '00:00:00:00:00:00',
                        'FF:FF:FF:FF:FF:FF', "02:11:22:33:44:55'; touch /tmp/escape #"):
            with self.subTest(address=address):
                model = Model(address=address)
                self.assertEqual(model.snapshot()['bluetoothAddress'], '02:00:00:00:00:00')
                self.assertFalse(any('cold_bluetooth_baseline_verified' in c[-1] for c in model.calls))


class NativeProofShellTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.profile = self.root / 'profile'; self.profile.mkdir(mode=0o700)
        self.address = '02:11:22:33:44:55'
        self.leaf = self.profile / 'bluetooth_address'; self.leaf.write_text(self.address + '\n'); self.leaf.chmod(0o600)
        self.props = self.root / 'props.json'
        self.values = {'persist.vendor.bt.rootcanal_mac_address': self.address,
                       'vendor.bt.rootcanal_mac_address': self.address, 'vendor.bt.xenoid_address_ready': '1'}
        self.bin = self.root / 'bin'; self.bin.mkdir()
        getprop = self.bin / 'getprop'
        getprop.write_text('#!' + sys.executable + '\nimport json,os,sys\nprint(json.load(open(os.environ["PROOF_PROPS"])).get(sys.argv[1],""))\n')
        getprop.chmod(0o755)
        stat = self.bin / 'stat'
        stat.write_text('#!' + sys.executable + '\nimport os,stat,sys\np=sys.argv[-1];s=os.stat(p);fmt=sys.argv[2]\nprint(s.st_size if fmt=="%s" else "0:0:"+oct(stat.S_IMODE(s.st_mode))[2:]+(":"+str(s.st_nlink) if fmt.endswith(":%h") else ""))\n')
        stat.chmod(0o755)
        self.model = Model()
        self.commands = []
        def actual(args, **_kwargs):
            command = args[-1].replace('d=/data/local/tmp/xenoid-profile;', 'd=' + shlex.quote(str(self.profile)) + ';')
            self.commands.append(command)
            self.props.write_text(json.dumps(self.values))
            result = subprocess.run(['sh', '-c', command], capture_output=True, text=True,
                                    env={**os.environ, 'PATH': str(self.bin) + os.pathsep + os.environ['PATH'],
                                         'PROOF_PROPS': str(self.props)}, timeout=8)
            return {'ok': result.returncode == 0, 'stdout': result.stdout}
        self.model.docker_exec = actual

    def check(self): return self.model._cold_bluetooth_baseline_matches(self.address)

    def test_actual_shell_proves_all_surfaces_without_changing_them(self):
        before = self.leaf.read_bytes()
        self.assertTrue(self.check())
        self.assertEqual(self.leaf.read_bytes(), before)
        self.assertEqual(self.leaf.stat().st_mode & 0o777, 0o600)
        self.assertFalse(any('setprop' in command or 'settings' in command for command in self.commands))

    def test_each_wrong_controller_property_is_rejected(self):
        for key in tuple(self.values):
            with self.subTest(key=key):
                old = self.values[key]; self.values[key] = 'wrong'
                self.assertFalse(self.check()); self.values[key] = old

    def test_wrong_sidecar_mode_size_value_and_extra_links_are_rejected(self):
        self.leaf.chmod(0o644); self.assertFalse(self.check()); self.leaf.chmod(0o600)
        self.leaf.write_text(self.address); self.assertFalse(self.check())
        self.leaf.write_text('02:AA:BB:CC:DD:EE\n'); self.assertFalse(self.check())
        self.leaf.write_text(self.address+'\n'); link=self.root/'extra'; os.link(self.leaf,link)
        self.assertFalse(self.check()); link.unlink()
        self.assertTrue(self.check())

    def test_symlink_leaf_and_directory_are_rejected(self):
        moved=self.root/'leaf'; self.leaf.rename(moved); self.leaf.symlink_to(moved)
        self.assertFalse(self.check()); self.leaf.unlink(); moved.rename(self.leaf)
        directory=self.root/'elsewhere'; self.profile.rename(directory); self.profile.symlink_to(directory,target_is_directory=True)
        self.assertFalse(self.check())


if __name__ == '__main__': unittest.main()
