import io
from contextlib import redirect_stdout
import json
import os
import runpy
import tempfile
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from xenoid.artifacts import BUILDER_SCHEMA, CONSUMER_TARGETS, TARGETS
from xenoid.backend import RuntimeManager
from xenoid.config import XenoidConfig, config_dict, _validate_config, InstanceError, new_instance_config
from xenoid.convergence import ConvergencePlanner, ConvergenceError, _PHASE_INDEX, _plan_may_mutate
from xenoid.graphics import RendererOverlay, BASE_SHA256, TARGET, zygote_mapping_proof, renderer_environment


SHA = 'a' * 64
OTHER = 'b' * 64
DESCRIPTOR = 'c' * 64
IID = '1b606f69-1baf-448c-831e-1d761d856901'
CID = 'd' * 64


def config(label='Mali-G78'):
    return XenoidConfig(instance_name='phone-test', instance_id=IID, graphics_renderer=label)


def row(dev=1797, ino=35, digest=SHA):
    return {'ok': True, 'device': dev, 'inode': ino, 'sha256': digest}


def record(source=SHA):
    return {'schema': 'dev.xenoid.renderer-binding/v1', 'instanceId': IID, 'containerId': CID,
            'sourceSha256': source, 'manifestSha256': DESCRIPTOR,
            'source': {'device': 1797, 'inode': 35}, 'base': {'device': 1796, 'inode': 4107}}


def overlay(label='Mali-G78', rec=None, target=None, mappings=False):
    manager = SimpleNamespace(cfg=config(label), context=SimpleNamespace(instance_id=IID),
                              soft_reboot=Mock(return_value={'ok': True}),
                              _owned_container_record=lambda: ({'Id': CID}, None))
    value = RendererOverlay(manager)
    value._artifacts = Mock(return_value=({'sha256': SHA}, {'sha256': DESCRIPTOR}))
    value._verify_control = Mock()
    value._verify_manifest = Mock()
    value._read_record = Mock(return_value=rec)
    value._write_record = Mock()
    value._stat = Mock(side_effect=lambda path: target if path == TARGET else row())
    value._helper = Mock(return_value={'ok': True, 'state': 'bound' if label else 'native', 'changed': False})
    value._mappings = Mock(return_value={'ok': mappings})
    value.observe = Mock(return_value={'ok': True})
    return value, manager


class RendererMappingTests(unittest.TestCase):
    def test_every_zygote_must_map_current_inode(self):
        output = 'BEGIN 123 124\nPID 123\n1000-2000 r-xp 0 07:05 35 ' + TARGET + '\nPID 124\n1000-2000 r--p 0 07:05 35 ' + TARGET + '\nEND 123 124'
        self.assertTrue(zygote_mapping_proof(output, os.makedev(7, 5), 35)['ok'])
        self.assertFalse(zygote_mapping_proof(output.replace('07:05 35', '07:04 4107', 1), os.makedev(7, 5), 35)['ok'])

    def test_truncated_complete_first_pid_never_accepts(self):
        output = 'BEGIN 123 124\nPID 123\n1000-2000 r-xp 0 07:05 35 ' + TARGET
        self.assertFalse(zygote_mapping_proof(output, os.makedev(7, 5), 35)['ok'])
        self.assertFalse(zygote_mapping_proof(output + '\nEND 123 124', os.makedev(7, 5), 35)['ok'])

    def test_absent_or_duplicate_zygote_is_not_evidence(self):
        for text in ('', 'PID 123\n', 'PID 123\nPID 123\n'):
            self.assertFalse(zygote_mapping_proof(text, 1797, 35)['ok'])

    def test_prefixed_mapped_filename_and_deleted_inode_cannot_change_identity(self):
        line = 'BEGIN 123\nPID 123\n1000-2000 r-xp 0 07:05 35 ' + TARGET + ' (deleted)\nEND 123'
        self.assertTrue(zygote_mapping_proof(line, os.makedev(7, 5), 35)['ok'])


class RendererConfigTests(unittest.TestCase):
    def test_default_preserves_legacy_serialization(self):
        self.assertNotIn('graphics_renderer', config_dict(config()))
        self.assertNotIn('cold_profile', config_dict(config()))
        self.assertEqual(renderer_environment(config()), {'XENOID_GLES_RENDERER': 'Mali-G78'})

    def test_null_roundtrip_disables_without_native_candidate(self):
        self.assertIsNone(config_dict(config(None))['graphics_renderer'])
        self.assertEqual(renderer_environment(config(None))['XENOID_GLES_RENDERER'], 'Mali-G78')

    def test_explicit_disabled_new_instance_survives_init(self):
        cfg = new_instance_config("phone-test", IID, {"graphics_renderer": None})
        self.assertIsNone(cfg.graphics_renderer)

    def test_label_validation(self):
        for label in ('', 'native', ' leading', 'trailing ', '\n', '\x7f', 'گرافیک', 'x' * 97, True):
            with self.subTest(label=repr(label)), self.assertRaises(InstanceError):
                _validate_config(config(label))
        _validate_config(config('Mali-G78'))
        _validate_config(config(None))

    def test_live_targets_do_not_enter_boot_context_or_builder_contract(self):
        self.assertEqual(BUILDER_SCHEMA, 2)
        for target in ('rendererControl', 'glesRenderer'):
            self.assertIn(target, CONSUMER_TARGETS['liveDeploy'])
            self.assertNotIn(target, CONSUMER_TARGETS['runtimeContext'])
            self.assertIn(target, CONSUMER_TARGETS['release'])
        self.assertNotIn('runtime/redroid/profile-gles-renderer.py', TARGETS['glesRenderer'].sources)
        self.assertIn('scripts/build-native-for-arch.sh', TARGETS['rendererControl'].sources)
        self.assertTrue(TARGETS['glesRenderer'].outputs[0].preserve_elf)
        self.assertFalse(TARGETS['rendererControl'].outputs[0].preserve_elf)
        self.assertLess(_PHASE_INDEX['control_ready'], _PHASE_INDEX['graphics_ready'])
        self.assertLess(_PHASE_INDEX['graphics_ready'], _PHASE_INDEX['identity_converged'])


class RendererCliTests(unittest.TestCase):
    def test_existing_convergence_defers_before_config_write(self):
        from xenoid.cli import cmd_renderer_configure
        args = SimpleNamespace(config=config())
        with patch('xenoid.cli.runtime'), patch('xenoid.convergence.ConvergenceJournal') as journal, patch('xenoid.cli.save_config') as save, redirect_stdout(io.StringIO()) as output:
            journal.for_manager.return_value.load.return_value = {'planDigest': SHA}
            self.assertEqual(cmd_renderer_configure(args), 1)
            self.assertEqual(json.loads(output.getvalue())['error'], 'graphics_convergence_pending')
            save.assert_not_called()

    def test_native_gen_rejection_does_not_resume_or_replace_targets(self):
        from xenoid.cli import _reject_unrelated_regeneration_mutation, cmd_renderer_configure
        args = SimpleNamespace(context=SimpleNamespace(), func=cmd_renderer_configure)
        with patch('xenoid.cli.RegenerationJournal') as journal:
            fixed = {'phase': 'storage_identity_committed', 'transactionId': 'f' * 32}
            journal.return_value.load.return_value = dict(fixed)
            result = _reject_unrelated_regeneration_mutation(args)
            self.assertEqual(result['error'], 'device_regeneration_pending')
            self.assertEqual(result['transactionId'], fixed['transactionId'])
            journal.return_value.save.assert_not_called()


class RendererPlannerTests(unittest.TestCase):
    def test_existing_image_only_deploys_components_without_recreate(self):
        observation = {
            'instanceId': IID,
            'runtime': {'state': 'running', 'containerId': CID,
                        'ownershipValid': True, 'integrationIdentityValid': True,
                        'storageValid': True, 'createSpecMatches': True,
                        'networkMatches': True, 'volumeMatches': True,
                        'imageInputSha256': SHA, 'imageBootInputSha256': OTHER,
                        'bootSeedRequired': False},
            'components': {name: {'state': 'matching'} for name in
                           ('daemon', 'identity', 'location', 'proxy', 'keybox', 'camera', 'google', 'protection')},
            'desiredImageInputSha256': SHA, 'desiredImageBootInputSha256': OTHER,
            'artifactTargets': [], 'artifactRecords': [], 'acceptanceChecks': ['container'],
        }
        observation['components']['deploy'] = {'glesRenderer': {'state': 'drift'}, 'rendererControl': {'state': 'drift'}}
        manager = SimpleNamespace(context=SimpleNamespace(instance_id=IID))
        plan = ConvergencePlanner(manager).inspect(observation=observation)
        self.assertEqual(plan.runtime_action, 'reuse')
        self.assertEqual(plan.image_action, 'reuse-selected')
        self.assertEqual(plan.recreate_reasons, ())
        self.assertEqual(dict(plan.deploy_components), {'glesRenderer': 'deploy', 'rendererControl': 'deploy'})
        self.assertTrue(_plan_may_mutate(plan))
        observation['components']['deploy'] = {'glesRenderer': {'state': 'matching'}, 'rendererControl': {'state': 'matching'}}
        warm = ConvergencePlanner(manager).inspect(observation=observation)
        self.assertEqual(dict(warm.deploy_components), {})
        self.assertFalse(_plan_may_mutate(warm))


class RendererConvergeTests(unittest.TestCase):
    def test_warm_repeated_up_does_not_restart(self):
        value, manager = overlay(rec=record(), target=row(), mappings=True)
        self.assertTrue(value.converge([])['ok'])
        manager.soft_reboot.assert_not_called()
        value._write_record.assert_not_called()

    def test_old_mapping_requires_one_official_restart_without_ssaid_reset(self):
        value, manager = overlay(rec=record(), target=row(), mappings=False)
        result = value.converge([])
        self.assertTrue(result['ok'])
        self.assertTrue(result['restarted'])
        manager.soft_reboot.assert_called_once_with(require_health=False, reset_ssaid=False)

    def test_cold_restart_adopts_only_exact_original_and_source(self):
        value, manager = overlay(rec=record(), target=row(1800, 4107, BASE_SHA256), mappings=False)
        self.assertTrue(value.converge([])['ok'])
        committed = value._write_record.call_args.args[0]
        self.assertEqual(committed['base']['device'], 1800)
        self.assertEqual(committed['sourceSha256'], SHA)
        self.assertEqual(committed['containerId'], CID)

    def test_rollback_detaches_verified_owned_mount_and_restarts(self):
        value, manager = overlay(label=None, rec=record(), target=row(), mappings=False)
        self.assertTrue(value.converge([])['ok'])
        value._helper.assert_called_once_with('detach', record())
        manager.soft_reboot.assert_called_once_with(require_health=False, reset_ssaid=False)

    def test_unowned_target_never_gets_detached(self):
        value, manager = overlay(rec=None, target=row(digest=OTHER))
        self.assertEqual(value.converge([])['error'], 'graphics_unowned_target')
        value._helper.assert_not_called()
        manager.soft_reboot.assert_not_called()

    def test_changed_container_never_mutates(self):
        value, manager = overlay(rec=record(), target=row())
        self.assertEqual(value.converge([], 'e' * 64)['error'], 'convergence_state_conflict')
        value._helper.assert_not_called()
        manager.soft_reboot.assert_not_called()

    def test_record_is_durable_before_new_bind(self):
        value, _ = overlay(rec=None, target=row(1796, 4107, BASE_SHA256))
        order = []
        value._write_record.side_effect = lambda rec: order.append('record')
        value._helper.side_effect = lambda *args: order.append('bind') or {'ok': True}
        self.assertTrue(value.converge([])['ok'])
        self.assertEqual(order, ['record', 'bind'])

    def test_failed_restart_remains_retryable_with_owned_record(self):
        value, manager = overlay(rec=record(), target=row())
        manager.soft_reboot.return_value = {'ok': False}
        self.assertEqual(value.converge([])['error'], 'graphics_restart_failed')
        value._write_record.assert_not_called()
        value._helper.assert_called_once_with('bind', record())

    def test_restart_failure_preserves_only_sanitized_official_cause(self):
        value, manager = overlay(rec=record(), target=row())
        manager.soft_reboot.return_value = {
            'ok': False, 'code': 'soft_reboot_netd_control_failed',
            'controlOp': 'cellular-ready-addresses', 'controlErrno': 110,
            'rootExitCode': 1, 'token': 'private token', 'stdout': 'private output',
        }
        result = value.converge([])
        self.assertEqual(result['error'], 'graphics_restart_failed')
        self.assertEqual(result['errorCode'], 'soft_reboot_netd_control_failed')
        self.assertEqual(result['softReboot'], {
            'ok': False, 'code': 'soft_reboot_netd_control_failed',
            'controlOp': 'cellular-ready-addresses', 'controlErrno': 110,
            'rootExitCode': 1,
        })
        self.assertNotIn('private', json.dumps(result))

    def test_root_wrapper_requires_public_exit_field(self):
        manager = SimpleNamespace(daemon_client=lambda **kwargs: SimpleNamespace(root_exec=lambda command: {'ok': True, 'exitCode': 0, 'stdout': '{}'}))
        self.assertFalse(RendererOverlay(manager)._root('true')['ok'])
        manager.daemon_client = lambda **kwargs: SimpleNamespace(root_exec=lambda command: {'ok': True, 'exit': 0, 'stdout': '{}'})
        self.assertTrue(RendererOverlay(manager)._root('true')['ok'])

    def test_record_refuses_symlink_and_wrong_owner_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            value = RendererOverlay(SimpleNamespace(context=SimpleNamespace(state_root=Path(temporary), instance_id=IID)))
            value._write_record(record())
            self.assertEqual(value._read_record(), record())
            value._record_path().chmod(0o644)
            with self.assertRaises(ValueError):
                value._read_record()

    def test_pending_gen_is_detected_without_mutating_targets(self):
        manager = object.__new__(RuntimeManager)
        manager.context = SimpleNamespace()
        with patch('xenoid.backend.RegenerationJournal') as journal:
            state = {'transactionId': 'f' * 32, 'profile': {'fixed': True}}
            journal.return_value.load.return_value = state
            self.assertTrue(manager._graphics_deferred())
            self.assertEqual(state, {'transactionId': 'f' * 32, 'profile': {'fixed': True}})
            journal.return_value.load.return_value = None
            self.assertFalse(manager._graphics_deferred())

class RendererPendingAcceptanceTests(unittest.TestCase):
    def observe(self, deploy, mode='convergence-accept'):
        from xenoid.live_observe import LiveAcceptance
        # Exercise the real acceptance entry point with an explicitly failed
        # graphics observation. Other owners are independent existing checks.
        snapshot = {'runtime': {'state': 'running'}, 'components': {'graphics': {'ok': False, 'error': 'graphics_not_configured'}}}
        manager = SimpleNamespace(observe_convergence=lambda **kwargs: snapshot)
        observer = LiveAcceptance(manager)
        observer._host_checks = Mock(return_value={})
        observer._daemon_checks = Mock(return_value={})
        observer._proxy_checks = Mock()
        observer._google_and_protection_checks = Mock()
        return observer.observe(manager, {'componentActions': {'deploy': deploy}}, mode, None, None)

    def test_old_pending_gen_acceptance_does_not_require_new_graphics(self):
        for mode in ('convergence-resolve', 'convergence-accept'):
            with self.subTest(mode=mode):
                result = self.observe({'input': 'reuse'}, mode)
                self.assertTrue(result['ok'])
                self.assertNotIn('graphics', result['checks'])

    def test_new_graphics_plan_cannot_accept_failed_graphics(self):
        result = self.observe({'glesRenderer': 'deploy'})
        self.assertFalse(result['ok'])
        self.assertEqual(result['errorCode'], 'graphics_not_ready')

    def test_doctor_still_observes_graphics_even_without_deploy_plan(self):
        result = self.observe({}, 'doctor-runtime')
        self.assertFalse(result['ok'])
        self.assertEqual(result['errorCode'], 'graphics_not_ready')


class RendererColdBootCompatibilityTests(unittest.TestCase):
    # Reproduces a qualified stopped-runtime observation; device ownership and
    # storage identifiers are synthetic. Content/boot digests retain the exact
    # full-input-only drift that exposed the cold-start planner regression.
    OLD_INPUT = '90e4c79acd8ad29b7cd94d5176890a138265c00b2657f87474c2cc5101ac2a0d'
    NEW_INPUT = '8dcd318c38f22f146c4066126b9b8e2af9062083025c5cd987076d8862c9dd3c'
    BOOT = 'a768b099b872cab946763cbcc5ec110edd8c7f497b61febc9fc1210b98bee7a3'

    def setUp(self):
        self.manager = SimpleNamespace(context=SimpleNamespace(instance_id=IID))
        self.observation = {
            'instanceId': IID,
            'runtime': {'state': 'stopped', 'containerId': CID,
                        'ownershipValid': True, 'integrationIdentityValid': True,
                        'storageValid': True, 'createSpecMatches': True,
                        'networkMatches': True, 'volumeMatches': True,
                        'storageMigrationRequired': False,
                        'imageInputSha256': self.OLD_INPUT, 'imageBootInputSha256': self.BOOT,
                        'bootSeedRequired': False},
            'components': {name: {'state': 'matching'} for name in
                           ('identity', 'location', 'proxy', 'keybox', 'camera', 'google', 'protection')},
            'desiredImageInputSha256': self.NEW_INPUT, 'desiredImageBootInputSha256': self.BOOT,
            'artifactTargets': [], 'artifactRecords': [], 'acceptanceChecks': ['container'],
        }
        self.observation['components']['daemon'] = {'state': 'unknown'}
        self.observation['components']['deploy'] = {'glesRenderer': {'state': 'unknown'}, 'rendererControl': {'state': 'unknown'}}

    def plan(self):
        return ConvergencePlanner(self.manager).inspect(observation=self.observation)

    def test_boot_equivalent_apk_only_drift_starts_owned_container_and_resolves_live(self):
        plan = self.plan()
        self.assertEqual(plan.runtime_action, 'start')
        self.assertEqual(plan.image_action, 'reuse-selected')
        self.assertEqual(plan.boot_seed_action, 'none')
        self.assertEqual(plan.recreate_reasons, ())
        self.assertTrue(plan.live_observation_required)
        self.assertEqual(plan.daemon_action, 'inspect')
        self.assertIsNone(plan.deploy_components)
        self.assertEqual(plan.desired_image_boot_input_sha256, self.BOOT)

    def test_different_boot_contract_keeps_recreate(self):
        self.observation['runtime']['imageBootInputSha256'] = '9' * 64
        plan = self.plan()
        self.assertEqual(plan.runtime_action, 'recreate')
        self.assertEqual(plan.image_action, 'ensure-desired')
        self.assertIn('runtime_boot_image_mismatch', plan.recreate_reasons)
        self.assertIn('runtime_image_input_mismatch', plan.recreate_reasons)

    def test_missing_boot_contract_never_qualifies_compatibility(self):
        self.observation['runtime']['imageBootInputSha256'] = None
        self.assertEqual(self.plan().runtime_action, 'recreate')
        self.observation['desiredImageBootInputSha256'] = None
        with self.assertRaises(ConvergenceError):
            self.plan()

    def test_other_contract_mismatches_keep_recreate_even_when_boot_matches(self):
        for field in ('createSpecMatches', 'networkMatches', 'volumeMatches'):
            with self.subTest(field=field):
                self.observation['runtime'][field] = False
                plan = self.plan()
                self.assertEqual(plan.runtime_action, 'recreate')
                self.assertIn('runtime_image_input_mismatch', plan.recreate_reasons)
                self.observation['runtime'][field] = True
        self.observation['runtime']['storageMigrationRequired'] = True
        plan = self.plan()
        self.assertEqual(plan.runtime_action, 'recreate')
        self.assertIn('storage_v4_migration_required', plan.recreate_reasons)

    def test_unproven_owner_storage_integration_or_daemon_contract_stays_rejected(self):
        for field in ('ownershipValid', 'integrationIdentityValid', 'storageValid'):
            with self.subTest(field=field):
                self.observation['runtime'][field] = False
                with self.assertRaises(ConvergenceError):
                    self.plan()
                self.observation['runtime'][field] = True
        self.observation['components']['daemon']['state'] = 'incompatible'
        with self.assertRaises(ConvergenceError) as error:
            self.plan()
        self.assertEqual(error.exception.code, 'daemon_seed_contract_incompatible')


class RendererSavedStartRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Reuse the upstream runtime-free owner fixture, not a second executor.
        cls.contract = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/test-convergence.py'))

    def pending(self, root):
        from xenoid.convergence import ConvergenceExecutor, ConvergenceJournal
        fixture = self.contract
        snapshot = fixture['healthy_snapshot']()
        snapshot['runtime']['state'] = 'stopped'
        snapshot['controlPlane']['ready'] = False
        snapshot['components']['deploy']['glesRenderer'] = 'unknown'
        snapshot['components']['graphics'] = {'ok': False}
        manager = fixture['FakeManager'](root, snapshot)
        old_control = manager.reconcile_control_plane

        def control():
            answer = old_control()
            if answer['ok']:
                manager.snapshot['controlPlane']['ready'] = True
            return answer

        def graphics(**kwargs):
            answer = manager._result('graphics')
            if answer['ok']:
                manager.snapshot['components']['graphics'] = {'ok': True}
            return answer

        manager.reconcile_control_plane = control
        manager.reconcile_graphics = graphics
        manager.fail_once['graphics'] = 'soft_reboot_netd_control_failed'
        class Acceptance(fixture['FakeAcceptance']):
            def observe(self, **kwargs):
                answer = super().observe(**kwargs)
                if kwargs['mode'] == 'convergence-resolve':
                    actions = dict(kwargs['expected']['componentActions'])
                    for name, action in actions.items():
                        if name != 'deploy' and action in {None, 'inspect'}:
                            actions[name] = 'reconcile' if name == 'daemon' else 'reuse'
                    if actions['deploy'] is None:
                        actions['deploy'] = {name: 'deploy' if name == 'glesRenderer' else 'reuse'
                                             for name in manager.snapshot['components']['deploy']}
                    answer['componentActions'] = actions
                return answer
        acceptance = Acceptance(manager)
        first = ConvergenceExecutor(manager, live_acceptance=acceptance).run(skip_build=True)
        self.assertFalse(first['ok'])
        self.assertEqual(first['error'], 'soft_reboot_netd_control_failed')
        journal = ConvergenceJournal.for_manager(manager)
        state = journal.load()
        self.assertEqual(state['phase'], 'control_ready')
        self.assertEqual(state['plan']['runtimeAction'], 'start')
        return manager, journal, state, acceptance

    def test_stopped_saved_start_keeps_fixed_journal_and_revalidates_control(self):
        from xenoid.convergence import ConvergenceExecutor
        with tempfile.TemporaryDirectory() as temporary:
            manager, journal, before, acceptance = self.pending(Path(temporary))
            manager.snapshot['runtime']['state'] = 'stopped'
            manager.snapshot['controlPlane']['ready'] = False
            controls = []
            original = manager.reconcile_control_plane

            def checked_control():
                retained = journal.load()
                self.assertEqual(retained, before)
                controls.append(True)
                return original()

            manager.reconcile_control_plane = checked_control
            result = ConvergenceExecutor(manager, live_acceptance=acceptance).run(skip_build=True)
            self.assertTrue(result['ok'], result)
            self.assertTrue(result['resumed'])
            self.assertEqual(result['initialPlanDigest'], before['planDigest'])
            self.assertEqual(controls, [True])
            self.assertEqual(manager.snapshot['runtime']['containerId'], before['oldContainerId'])
            self.assertFalse(journal.exists())
            names = [name for name, _ in manager.calls]
            self.assertEqual(names.count('start'), 2)
            self.assertEqual(names.count('control'), 2)
            self.assertFalse(set(names) & {'remove', 'create', 'storage', 'seed-start'})

    def test_already_running_saved_start_with_stale_control_revalidates_without_restart(self):
        from xenoid.convergence import ConvergenceExecutor
        with tempfile.TemporaryDirectory() as temporary:
            manager, journal, before, acceptance = self.pending(Path(temporary))
            manager.snapshot['controlPlane']['ready'] = False
            result = ConvergenceExecutor(manager, live_acceptance=acceptance).run(skip_build=True)
            self.assertTrue(result['ok'], result)
            names = [name for name, _ in manager.calls]
            self.assertEqual(names.count('start'), 1)
            self.assertEqual(names.count('control'), 2)

    def test_control_revalidation_failure_does_not_rewind_or_rewrite_fixed_journal(self):
        from xenoid.convergence import ConvergenceExecutor
        with tempfile.TemporaryDirectory() as temporary:
            manager, journal, before, acceptance = self.pending(Path(temporary))
            manager.snapshot['runtime']['state'] = 'stopped'
            manager.snapshot['controlPlane']['ready'] = False
            manager.fail_once['control'] = 'bootstrap_unavailable'
            result = ConvergenceExecutor(manager, live_acceptance=acceptance).run(skip_build=True)
            self.assertFalse(result['ok'])
            self.assertEqual(result['error'], 'bootstrap_unavailable')
            self.assertEqual(journal.load(), before)

    def test_changed_runtime_is_rejected_before_saved_start_or_control(self):
        from xenoid.convergence import ConvergenceExecutor
        with tempfile.TemporaryDirectory() as temporary:
            manager, journal, before, acceptance = self.pending(Path(temporary))
            manager.snapshot['runtime']['containerId'] = 'e' * 64
            manager.snapshot['runtime']['state'] = 'stopped'
            count = len(manager.calls)
            result = ConvergenceExecutor(manager, live_acceptance=acceptance).run(skip_build=True)
            self.assertFalse(result['ok'])
            self.assertEqual(result['error'], 'convergence_state_conflict')
            self.assertEqual(journal.load(), before)
            self.assertFalse({'start', 'control'} & {name for name, _ in manager.calls[count:]})


if __name__ == '__main__':
    unittest.main()
