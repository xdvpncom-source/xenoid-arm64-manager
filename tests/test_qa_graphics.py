"""Renderer defaults remain independent of identity, with idle-only reloads."""
import contextlib
import copy
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'qa'))
import qa_graphics as g
import qa_resource_runtime as r


class GraphicsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / 'state'
        self.runs = self.root / 'runs'
        for key, value in [('STATE', self.state), ('RUNS', self.runs), ('CONFIG', self.root / 'settings.json'), ('OWNER', os.getuid())]:
            item = patch.object(g, key, value); item.start(); self.addCleanup(item.stop)
        self.manager = types.SimpleNamespace(
            context=types.SimpleNamespace(state_root=self.root/'native', project_root=self.root, instance_id='iid', resource_tag='tag'),
            instance_operation_lock=lambda **kwargs: contextlib.nullcontext(),
            _owned_container_record=lambda **kwargs: ({'Id': 'c'*64, 'State': {'Running': True}}, None))
        self.manager.context.state_root.mkdir()
        self.status = {'schema': 'dev.xenoid.renderer/v1', 'ok': True, 'state': 'bound',
                       'reportedLabel': 'Mali-G78', 'mapped': True, 'owned': True,
                       'readonly': True, 'zygoteCount': 2, 'sourceSha256': 'a'*64,
                       'runtime': {'containerId':'c'*64, 'initPid':123, 'startedAt':'boot-1', 'initStartTicks':456}}

    def patches(self):
        stack = contextlib.ExitStack()
        stack.enter_context(patch.object(r, 'native_manager', return_value=self.manager))
        stack.enter_context(patch.object(g, 'worker_inactive', return_value=True))
        stack.enter_context(patch.object(g, 'source_generation', return_value='generation-54'))
        stack.enter_context(patch.object(g, 'pending', return_value=False))
        return stack

    def test_default_and_explicit_native_do_not_touch_identity_files(self):
        identity = self.root / 'recorded-plan.json'
        identity.write_text('{"fingerprint":"fixed-build","target":"unchanged"}\n')
        before = identity.read_bytes()
        self.assertEqual(g.load_settings(), {'renderer_label':'Mali-G78'})
        with patch.object(g, 'read_private', return_value=None):
            receipt = g.configure({'renderer_label':None})
        self.assertEqual(receipt['settings'], {'renderer_label':None})
        self.assertEqual(identity.read_bytes(), before)
        self.assertEqual((g.CONFIG.stat().st_mode & 0o777), 0o600)

    def test_validator_and_legacy_missing_field(self):
        self.assertEqual(g.validate_qa_graphics({}), g.DEFAULT_QA_GRAPHICS)
        self.assertEqual(g.validate_qa_graphics({'renderer_label':'native'}), {'renderer_label':None})
        for value in [None, [], {'unknown':1}, {'renderer_label':''}, {'renderer_label':' x'},
                      {'renderer_label':'x\n'}, {'renderer_label':'é'}, {'renderer_label':'x'*97}]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                g.validate_qa_graphics(value)

    def test_worker_gate_prevents_reload_even_before_systemd_worker_is_visible(self):
        with g.lifecycle_gate(3, worker=True):
            with self.assertRaises(BlockingIOError):
                with g.lifecycle_gate(3):
                    self.fail('exclusive reload entered while worker owns shared gate')
        with g.lifecycle_gate(3):
            pass

    def test_active_worker_defers_without_native_calls(self):
        with self.patches(), patch.object(g, 'worker_inactive', return_value=False), patch.object(g, 'native_document') as call:
            result = g.reconcile_one('phone-3', g.DEFAULT_QA_GRAPHICS)
        self.assertTrue(result['deferred']); call.assert_not_called()

    def test_native_profile_or_receipt_pending_never_calls_configure(self):
        with self.patches(), patch.object(g, 'pending', return_value=True), patch.object(g, 'native_document') as call:
            result = g.reconcile_one('phone-3', g.DEFAULT_QA_GRAPHICS)
        self.assertTrue(result['deferred']); call.assert_not_called()

    def test_receipts_read_only_pending_without_native_journal_is_not_missed(self):
        directory = self.runs / '3'; directory.mkdir(parents=True)
        path = directory / 'runs.db'
        with sqlite3.connect(path) as connection:
            connection.execute('CREATE TABLE regeneration_receipts(document TEXT)')
            connection.execute('INSERT INTO regeneration_receipts VALUES(?)', (json.dumps({'status':'pending','target':{'pinned':1}}),))
        before = path.read_bytes()
        self.assertTrue(g.pending_receipts(3))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(directory.iterdir()), [path])

    def test_success_reads_live_proof_and_never_repeats_up_on_same_or_new_epoch(self):
        with self.patches(), patch.object(g, 'native_document', return_value=(self.status,0)) as call:
            self.assertFalse(g.reconcile_one('phone-3', g.DEFAULT_QA_GRAPHICS)['deferred'])
            second = copy.deepcopy(self.status)
            second['runtime'].update(initPid=789, startedAt='boot-2', initStartTicks=999)
            call.return_value = (second,0)
            self.assertFalse(g.reconcile_one('phone-3', g.DEFAULT_QA_GRAPHICS)['deferred'])
        self.assertEqual([c.args[2] for c in call.call_args_list], [['status'],['status']])
        marker = json.loads((self.state/'3.json').read_text())
        self.assertEqual(marker['runtime'], second['runtime'])
        self.assertEqual(marker['source_generation'], 'generation-54')
        self.assertEqual(marker['settings_sha256'], g.digest(g.DEFAULT_QA_GRAPHICS))

    def test_mismatched_proof_uses_only_native_configure_and_requires_fresh_readback(self):
        bad = dict(self.status, reportedLabel='old')
        with self.patches(), patch.object(g, 'native_document', side_effect=[(bad,0),({'ok':True},0),(self.status,0)]) as call:
            result = g.reconcile_one('phone-3',g.DEFAULT_QA_GRAPHICS)
        self.assertTrue(result['ok'])
        self.assertEqual([c.args[2] for c in call.call_args_list], [['status'],['configure','--json-stdin'],['status']])
        self.assertEqual(call.call_args_list[1].args[3], g.DEFAULT_QA_GRAPHICS)

    def test_missing_readonly_or_epoch_proof_cannot_succeed(self):
        for change in [{'readonly':False},{'owned':False},{'mapped':False},{'runtime':{}},{'zygoteCount':0}]:
            with self.subTest(change=change):
                self.assertFalse(g.proof(dict(self.status,**change),g.DEFAULT_QA_GRAPHICS))

    def test_native_rejection_preserves_pending_transaction(self):
        journal = self.manager.context.state_root / 'device-regenerate-v3.json'
        journal.write_text('{"target":"never-rewrite"}')
        before = journal.read_bytes()
        with self.patches(), patch.object(g,'native_document',side_effect=[({'ok':False},1),
                ({'ok':False,'error':'device_regeneration_pending'},1)]):
            result = g.reconcile_one('phone-3',g.DEFAULT_QA_GRAPHICS)
        self.assertTrue(result['deferred']); self.assertEqual(journal.read_bytes(),before)

    def boundary_marker(self):
        marker = {'schema':g.SCHEMA, 'settings_sha256':g.digest(g.DEFAULT_QA_GRAPHICS),
                  'source_generation':'generation-54', 'renderer_proof_sha256':'verified',
                  'instance_id':'iid', 'resource_tag':'tag', 'runtime':self.status['runtime']}
        g.atomic_json(self.state/'3.json',marker)
        self.manager._owned_container_record = lambda **kwargs: ({'Id':'c'*64,
            'State':{'Running':True,'Pid':123,'StartedAt':'boot-1'}},None)
        return marker

    def test_worker_warm_epoch_has_no_native_calls(self):
        self.boundary_marker()
        with self.patches(), patch.object(g,'worker_owner'), patch.object(g,'process_start_ticks',return_value=456), \
                patch.object(g,'native_document') as call, g.lifecycle_gate(3,worker=True) as fd:
            result = g.worker_boundary(3,{},self.manager,fd)
            self.assertTrue(result['cached_runtime_epoch'])
            call.assert_not_called()
            with self.assertRaises(BlockingIOError):
                with g.lifecycle_gate(3): pass

    def test_worker_cold_restart_or_changed_defaults_invalidates_cache(self):
        marker = self.boundary_marker()
        for variant in [{'runtime':dict(marker['runtime'],startedAt='old')},
                        {'settings_sha256':'old'}, {'source_generation':'old'}]:
            g.atomic_json(self.state/'3.json',dict(marker,**variant))
            with self.patches(), patch.object(g,'worker_owner'), patch.object(g,'process_start_ticks',return_value=456), \
                    patch.object(g,'reconcile_one',return_value={'ok':True,'deferred':False}) as call, \
                    g.lifecycle_gate(3,worker=True) as fd:
                self.assertTrue(g.worker_boundary(3,{},self.manager,fd)['ok'])
                call.assert_called_once_with('phone-3',g.DEFAULT_QA_GRAPHICS,owned_worker=True,expected_binding={'instance_id':'iid','resource_tag':'tag','container_id':'c'*64})

    def test_worker_upgrade_busy_and_exception_restore_shared_gate(self):
        self.boundary_marker()
        with self.patches(), patch.object(g,'worker_owner'), g.lifecycle_gate(3,worker=True) as fd:
            with g.lifecycle_gate(3,worker=True):
                self.assertTrue(g.worker_boundary(3,{},self.manager,fd)['deferred'])
            with patch.object(g,'load_settings',side_effect=ValueError('broken')):
                self.assertFalse(g.worker_boundary(3,{},self.manager,fd)['ok'])
            with self.assertRaises(BlockingIOError):
                with g.lifecycle_gate(3): pass

    def test_worker_owner_mismatch_and_pending_block_without_ui_or_native_calls(self):
        with self.patches(), patch.object(g,'worker_owner',side_effect=ValueError('owner')), \
                patch.object(g,'native_document') as call, g.lifecycle_gate(3,worker=True) as fd:
            self.assertFalse(g.worker_boundary(3,{},self.manager,fd)['ok'])
            call.assert_not_called()
        with self.patches(), patch.object(g,'worker_owner'), patch.object(g,'pending',return_value=True), \
                patch.object(g,'native_document') as call, g.lifecycle_gate(3,worker=True) as fd:
            self.assertTrue(g.worker_boundary(3,{},self.manager,fd)['deferred'])
            call.assert_not_called()

    def test_replacement_container_is_rejected_before_native_calls(self):
        self.boundary_marker()
        with self.patches(), patch.object(g,'worker_owner'), patch.object(g,'native_document') as call, \
                g.lifecycle_gate(3,worker=True) as fd:
            result=g.worker_boundary(3,{'expected_container_id':'d'*64},self.manager,fd)
            self.assertFalse(result['ok']); call.assert_not_called()

    def test_timer_accepts_worker_cached_proof_without_exclusive_gate_or_native_calls(self):
        self.boundary_marker()
        with self.patches(), patch.object(g,'process_start_ticks',return_value=456), \
                patch.object(g,'native_document') as call, g.lifecycle_gate(3,worker=True):
            result=g.reconcile_one('phone-3',g.DEFAULT_QA_GRAPHICS)
            self.assertTrue(result['cached_runtime_epoch']); call.assert_not_called()

    def test_owner_accepts_legacy_missing_iid_without_rewriting_config_and_rejects_wrong_pid(self):
        group='/system.slice/xenoid-panel-script@3.service'
        cgroup=self.root/'cgroups'/group.lstrip('/')
        cgroup.mkdir(parents=True)
        (cgroup/'cgroup.procs').write_text(str(os.getpid()))
        folder=self.runs/'3';folder.mkdir(parents=True)
        config={'run_id':'r1','resource_tag':'tag'}
        with sqlite3.connect(folder/'runs.db') as connection:
            connection.execute('CREATE TABLE runs(id TEXT,config TEXT,current INTEGER)')
            connection.execute('INSERT INTO runs VALUES(?,?,1)',('r1',json.dumps(config)))
        raw=(folder/'runs.db').read_bytes()
        response=types.SimpleNamespace(returncode=0,stdout='ActiveState=active\nSubState=running\nMainPID='+str(os.getpid())+'\nControlPID=0\nControlGroup='+group+'\n')
        original=Path.read_text
        def read(path,*args,**kwargs):
            if str(path)=='/proc/self/cgroup':return '0::'+group+'\n'
            return original(path,*args,**kwargs)
        with patch.object(g,'CGROUP_ROOT',self.root/'cgroups'), patch.object(g.subprocess,'run',return_value=response), \
                patch.object(Path,'read_text',read):
            g.worker_owner(3,config,self.manager)
            self.assertEqual((folder/'runs.db').read_bytes(),raw)
            response.stdout=response.stdout.replace('MainPID='+str(os.getpid()),'MainPID=999999')
            with self.assertRaises(ValueError):g.worker_owner(3,config,self.manager)

    def test_settings_ack_cannot_clear_a_newer_panel_save(self):
        g.configure({'renderer_label':'one'})
        old = g.load_document()
        g.configure({'renderer_label':'two'})
        self.assertFalse(g.acknowledge(old))
        current = g.load_document()
        self.assertTrue(current['pending'])
        self.assertEqual(current['settings'],{'renderer_label':'two'})
        self.assertTrue(g.acknowledge(current))
        self.assertFalse(g.load_document()['pending'])

    def test_idle_timer_returns_before_registry_import(self):
        import io
        with patch.object(g,'load_document',return_value={'pending':False}), patch.object(g.os,'geteuid',return_value=0), \
                patch.dict(sys.modules,{'qa_resource_runtime':None}), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(g.main(['reconcile','--all','--pending-only']),0)
        self.assertEqual(json.loads(output.getvalue())['skipped'],'no_pending_graphics')

    def test_orphan_child_cgroup_blocks_external_reload(self):
        group = '/system.slice/xenoid-panel-script@3.service'
        path = self.root / 'cgroups' / group.lstrip('/')
        path.mkdir(parents=True)
        (path/'cgroup.procs').write_text('999\n')
        response = types.SimpleNamespace(returncode=0, stdout='ActiveState=failed\nSubState=failed\nMainPID=0\nControlPID=0\nControlGroup='+group+'\n')
        with patch.object(g,'CGROUP_ROOT',self.root/'cgroups'), patch.object(g.subprocess,'run',return_value=response):
            self.assertFalse(g.worker_inactive(3))
            (path/'cgroup.procs').write_text('')
            self.assertTrue(g.worker_inactive(3))


if __name__ == '__main__':
    unittest.main()
