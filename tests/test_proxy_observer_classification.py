"""Exercise actual patched native observer classification with native strict proofs.

The pinned observer method is included for offline CI. The shipped native patch
is applied to it; only unrelated observer IO is omitted by compiling its proxy
AST block. All acceptance predicates execute the complete native fixture.
"""
import ast
import base64
from collections.abc import Mapping
import copy
import gzip
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import test_proxy_counter_acceptance as acceptance
from native_backend_history_fixture import OLD_HISTORY

ROOT = Path(__file__).resolve().parents[1]
OLD_OBSERVER_SHA256 = '5abe1d53901108c4c60687068de16410c36ff2071d494fffcce0217b141665c1'
OLD_OBSERVER_GZIP = (
    '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'
)

OLD_OBSERVER = gzip.decompress(base64.b64decode(OLD_OBSERVER_GZIP)).decode()


def patched_observer():
    candidate = os.environ.get('XENOID_PROXY_OBSERVER_CANDIDATE')
    if candidate:
        source = Path(candidate).read_text()
        return source if source.startswith('class ') or not source.startswith('    def ') else 'class RuntimeManager:\n' + source
    with tempfile.TemporaryDirectory() as directory:
        temporary = Path(directory)
        path = temporary / 'src/xenoid/backend.py'
        path.parent.mkdir(parents=True)
        path.write_text('class RuntimeManager:\n' + OLD_HISTORY + '\n' + OLD_OBSERVER)
        subprocess.run(['git', 'apply', '--include=src/xenoid/backend.py',
                        str(ROOT / 'patches/proxy-live-acceptance.patch')],
                       cwd=temporary, check=True, capture_output=True)
        return path.read_text()


def proxy_classifier(source):
    tree = ast.parse(source)
    observer = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.FunctionDef) and node.name == 'observe_convergence')
    def assigns(node, name):
        return isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
    start = next(i for i, node in enumerate(observer.body) if assigns(node, 'proxy_daemon'))
    end = next(i for i, node in enumerate(observer.body) if assigns(node, 'pending'))
    prototype = ast.parse('def classify(self, proxy_desired, bootstrap, proxy_engine):\n    pass').body[0]
    prototype.body = ast.parse('components = bootstrap.get("components", {})').body + copy.deepcopy(observer.body[start:end]) + [ast.Return(ast.Name('proxy_state', ast.Load()))]
    module = ast.fix_missing_locations(ast.Module(body=[prototype], type_ignores=[]))
    namespace = {'__name__': 'observer_classification_fixture.backend',
                 '__package__': 'observer_classification_fixture', 'Mapping': Mapping}
    exec(compile(module, 'actual-native-observer-proxy-block', 'exec'), namespace)
    return namespace['classify']


NATIVE_SIMPLE_ACTION_SOURCE = 'def _simple_action(state: str, *, converge: str) -> str:\n        if state == "matching":\n            return "reuse"\n        if state == "unknown":\n            return "inspect"\n        if state in {"drift", "pending"}:\n            return converge\n        raise ConvergenceError("convergence_observation_invalid")'

NEW_SOURCE = patched_observer()
CLASSIFY = proxy_classifier(NEW_SOURCE)
OLD_CLASSIFY = proxy_classifier('class RuntimeManager:\n' + OLD_OBSERVER)


class ProxyObserverClassificationTests(unittest.TestCase):
    def setUp(self):
        package = types.ModuleType('observer_classification_fixture')
        package.__path__ = []
        self.enterContext(patch.dict(sys.modules, {
            package.__name__: package,
            package.__name__ + '.live_observe': acceptance.NEW,
        }))
        self.snapshot, self.daemon = acceptance.fixture(host_count=100, report_count=50)
        self.daemon['bootstrap']['runtimeEpoch'] = acceptance.EPOCH
        self.host = acceptance.current_host(100)

    def classify(self, *, host=None, classifier=None):
        manager = acceptance.Host(self.host if host is None else host)
        state = (classifier or CLASSIFY)(manager, copy.deepcopy(self.daemon['proxy']),
                                        copy.deepcopy(self.daemon['bootstrap']),
                                        copy.deepcopy(self.snapshot['components']['proxy']['engine']))
        return state, manager

    def test_pinned_real_observer_source_hash(self):
        self.assertEqual(hashlib.sha256(OLD_OBSERVER.encode()).hexdigest(), OLD_OBSERVER_SHA256)

    def test_old_native_observer_reproduces_permanent_enabled_proxy_drift(self):
        state, manager = self.classify(classifier=OLD_CLASSIFY)
        self.assertEqual(state, 'pending')
        self.assertEqual(manager.calls, [])

    def test_enabled_verified_proxy_matches_despite_host_only_flag_false(self):
        self.assertIs(self.host['dataPlaneVerified'], False)
        state, manager = self.classify()
        self.assertEqual(state, 'matching')
        self.assertEqual(manager.calls, ['host_after_daemon_report'])

    def test_native_planner_reuses_matching_proof_and_reconciles_pending_proof(self):
        namespace = {'ConvergenceError': RuntimeError}
        exec(compile(NATIVE_SIMPLE_ACTION_SOURCE, 'pinned-native-planner-simple-action', 'exec'), namespace)
        action = namespace['_simple_action']
        self.assertEqual(action(self.classify()[0], converge='reconcile'), 'reuse')
        self.daemon['proxy']['report']['dataPlaneVerified'] = False
        self.assertEqual(action(self.classify()[0], converge='reconcile'), 'reconcile')

    def test_missing_or_unverified_daemon_report_remains_pending_with_active_host(self):
        for broken in ('absent', 'unverified', 'nonactive', 'unreadable'):
            with self.subTest(broken=broken):
                saved = copy.deepcopy(self.daemon)
                if broken == 'absent': self.daemon['proxy']['report'] = None
                elif broken == 'unverified': self.daemon['proxy']['report']['dataPlaneVerified'] = False
                elif broken == 'nonactive': self.daemon['proxy']['report']['phase'] = 'applying'
                else: self.daemon['proxy']['stateReadable'] = False
                self.assertEqual(self.classify()[0], 'pending')
                self.daemon = saved

    def test_report_and_probe_error_codes_remain_pending(self):
        for name in ('report', 'probe'):
            with self.subTest(name=name):
                self.daemon['proxy'][name]['errorCode'] = 'fixture_probe_failed'
                self.assertEqual(self.classify()[0], 'pending')
                self.daemon['proxy'][name].pop('errorCode')

    def test_stale_or_missing_check_ids_and_capabilities_remain_pending(self):
        mutations = (
            lambda d: d['report'].update(checkId=1),
            lambda d: d['probe'].update(checkId=1),
            lambda d: d.update(checkId=0),
            lambda d: d.pop('probe'),
            lambda d: d['report']['capabilities'].update(v4TcpProxy=False),
            lambda d: d['probe']['capabilities'].update(v4DnsProxy=False),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                saved = copy.deepcopy(self.daemon)
                mutate(self.daemon['proxy'])
                self.assertEqual(self.classify()[0], 'pending')
                self.daemon = saved

    def test_host_or_report_generation_mismatch_remains_pending(self):
        changed = copy.deepcopy(self.host)
        changed['generation'] += 1
        self.assertEqual(self.classify(host=changed)[0], 'pending')
        self.daemon['proxy']['report']['generation'] += 1
        self.assertEqual(self.classify()[0], 'pending')

    def test_host_or_bootstrap_runtime_epoch_mismatch_remains_pending(self):
        changed = copy.deepcopy(self.host)
        changed['runtimeEpoch'] = 'b'*64
        self.assertEqual(self.classify(host=changed)[0], 'pending')
        self.daemon['bootstrap']['runtimeEpoch'] = 'b'*64
        self.assertEqual(self.classify()[0], 'pending')

    def test_current_counter_reset_is_pending_despite_larger_cached_snapshot(self):
        reset = acceptance.current_host(10)
        state, manager = self.classify(host=reset)
        self.assertEqual(state, 'pending')
        self.assertEqual(manager.calls, ['host_after_daemon_report'])

    def test_invalid_counter_shapes_and_types_remain_pending(self):
        mutations = (lambda h: h['counters'].pop('ingress'),
                     lambda h: h['counters'].update(ingress=True),
                     lambda h: h['counters'].update(ingress=100.0),
                     lambda h: h['counters'].update(ingress=-1))
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                changed = copy.deepcopy(self.host)
                mutate(changed)
                self.assertEqual(self.classify(host=changed)[0], 'pending')

    def test_live_host_or_daemon_quarantine_remains_pending(self):
        changed = copy.deepcopy(self.host)
        changed.update(quarantined=True, phase='quarantined')
        self.assertEqual(self.classify(host=changed)[0], 'pending')
        self.daemon['proxy']['quarantined'] = True
        self.assertEqual(self.classify()[0], 'pending')

    def test_missing_proxy_bootstrap_component_remains_pending(self):
        self.daemon['bootstrap']['components'].pop('proxy')
        self.assertEqual(self.classify()[0], 'unknown')

    def test_fresh_host_read_failure_does_not_reuse_cached_proof(self):
        manager = acceptance.Host(self.host, RuntimeError('fixture transport failure'))
        state = CLASSIFY(manager, self.daemon['proxy'], self.daemon['bootstrap'], self.host)
        self.assertEqual(state, 'pending')

    def test_disabled_proxy_matches_only_verified_current_off_proof(self):
        self.daemon['proxy'].update(enabled=False)
        self.host.update(phase='off', dataPlaneVerified=True)
        self.assertEqual(self.classify()[0], 'matching')
        for key, value in (('dataPlaneVerified', False), ('generation', 100),
                           ('runtimeEpoch', 'b'*64), ('phase', 'active')):
            with self.subTest(key=key):
                changed = copy.deepcopy(self.host)
                changed[key] = value
                self.assertEqual(self.classify(host=changed)[0], 'pending')

    def test_shipped_patch_does_not_modify_native_regeneration_strict_guard(self):
        source = (ROOT / 'patches/proxy-live-acceptance.patch').read_text()
        self.assertNotIn('diff --git a/src/xenoid/cli.py ', source)


if __name__ == '__main__':
    unittest.main()
