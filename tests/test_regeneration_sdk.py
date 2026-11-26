"""SDK receipts and lifecycle checks. Native journal internals have separate integration tests."""
import contextlib
import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'qa'))
import script_host as h
import script_runtime as sdk

UUID='11111111-1111-1111-1111-111111111111'
BASE={'run_id':'managed-run','resource_tag':'0123456789ab','instance_id':UUID,'expected_container_id':'a'*64,
      'script_id':1,'version':11,'language':'python','source':'pass','repeat_count':0,'initial_delay':0,'interval':0,'timeout':0}

class NativeFailure(RuntimeError):
    code='native_phase_pending'

class ConvergenceError(RuntimeError):
    def __init__(self,code,phase=None):super().__init__(code);self.code=code;self.phase=phase

class Manager:
    def __init__(self,root):
        self.context=types.SimpleNamespace(instance_id=UUID,resource_tag=BASE['resource_tag'],state_root=root)
        self.cfg=types.SimpleNamespace();self.lease=object();self.journal=None;self.digest='a'*64
        self.requests=[];self.held=False;self.rotations=0;self.commands=0;self.verifications=0
        self.failure=None;self.google_ready=True;self.operation_lock_held=False;self.cid='a'*64
        self.image='sha256:'+'f'*64
    @contextlib.contextmanager
    def instance_operation_lock(self,timeout_seconds=None):
        self.requests.append(timeout_seconds);self.held=True
        try:yield
        finally:self.held=False
    def _owned_container_record(self,timeout):
        return {'Id':self.cid,'State':{'Running':True},'Image':self.image,
                'Mounts':[{'Type':'volume','Name':'device-data','Source':'/data/device','Destination':'/data','RW':True}]},None
    @property
    def adb_target(self):return '127.0.0.1:5555'


def native_cli(manager):
    cli=types.SimpleNamespace()
    @contextlib.contextmanager
    def command_budget(seconds):
        manager.command_budgets=getattr(manager,'command_budgets',[])+[seconds]
        yield
    cli.command_timeout=command_budget
    class Journal:
        def __init__(self,context):self.context=context
        def validate(self,state):
            if state['instanceId']!=self.context.instance_id:raise NativeFailure('invalid')
            return copy.deepcopy(state)
        def load(self):return copy.deepcopy(manager.journal)
        def _write(self,state):manager.journal=self.validate(state);return self.load()
        def prepare(self,transaction,before,target):return self._write({'instanceId':UUID,'transactionId':transaction,'phase':'prepared','before':before,'target':target})
        def advance(self,phase):
            state=self.load();state['phase']=phase;return self._write(state)
        def clear(self):
            assert self.load()['phase']=='committed'
            manager.journal=None
    cli.RegenerationJournal=Journal
    cli.DeviceIdentityStore=lambda context:types.SimpleNamespace(load=lambda:{'stable':{'digest':manager.digest}})
    cli.stable_identity_digest=lambda stable:stable['digest']
    def success(state,**kwargs):
        return {'schema':'dev.xenoid.device-regenerate/v3','ok':True,'phase':'committed','runtimeOnly':True,'containerRecreated':False}
    cli._regeneration_success_result=success
    def verify(current,store,state):
        manager.verifications+=1
        if manager.digest!=state['target']['stableDigest']:raise NativeFailure('target mismatch')
        return {'googleIdentity':{'ok':True}}
    cli._verify_regeneration_target_state=verify
    cli._regeneration_google_runtime_postcondition=lambda current:{'ok':manager.google_ready,'error':'google_not_ready'}
    cli._regeneration_runtime_preflight=lambda current:{'ok':True}
    def command(args):
        manager.commands+=1
        assert manager.held and manager.operation_lock_held
        assert cli.runtime(args) is manager
        assert args._operation_lock_held and not args.dry_run
        journal=cli.RegenerationJournal(manager.context)
        state=journal.load()
        if state is None:
            manager.rotations+=1
            target=chr(ord('a')+manager.rotations)*64
            state=journal.prepare(str(manager.rotations)*32,{'stableDigest':manager.digest,'bootId':'old-boot'},
                                  {'stableDigest':target,'bootId':'new-boot'})
        if manager.failure=='prepared':manager.failure=None;raise NativeFailure('https://secret:password@example.test')
        manager.digest=state['target']['stableDigest']
        if state['phase']!='committed':state=journal.advance('committed')
        if manager.failure=='committed':manager.failure=None;raise NativeFailure('private native state')
        journal.clear()
        if manager.failure=='cleared':manager.failure=None;raise NativeFailure('private native state')
        cli.print_json(success(state));return 0
    cli.cmd_device_regenerate=command
    return cli


def prep_plan(**changes):
    return types.SimpleNamespace(**{'resolution':'complete','boot_seed_action':'none','image_action':'reuse-selected',
        'runtime_action':'reuse','desired_image_input_sha256':'c'*64,'desired_image_boot_input_sha256':'d'*64,
        'plan_digest':'e'*64,**changes})


def prep_observation(manager):
    return {'instanceId':UUID,'selectedImageRecord':{'imageId':manager.image,'inputSha256':'c'*64,'bootInputSha256':'d'*64},
        'runtime':{'containerId':manager.cid,'ownershipValid':True,'integrationIdentityValid':True,'storageValid':True,
                   'imageInputSha256':'c'*64,'imageBootInputSha256':'d'*64,'dataUuid':UUID,
                   'rootfsUuid':'22222222-2222-2222-2222-222222222222'}}


def prep_journal(plan,manager):
    return {'instanceId':UUID,'plan':vars(plan).copy(),'completed':['image_ensured','container_removed',
        'storage_converged','container_created','runtime_started','proxy_converged'],
        'newContainerId':manager.cid,'regenerationTransactionId':None,'bootSeedTarget':None,
        'selectedImageInputSha256':'c'*64,'selectedImageBootInputSha256':'d'*64,'observedDataUuid':UUID,
        'observedRootfsUuid':'22222222-2222-2222-2222-222222222222'}


def cli_with_preparation(manager,store):
    cli=native_cli(manager);manager.prepared=False;manager.up_calls=0;manager.executed_phases=[]
    manager.observation=prep_observation(manager);manager.up_plan=prep_plan();manager.up_journal=None
    cli._regeneration_runtime_preflight=lambda current:({'ok':True} if manager.prepared else
        {'ok':False,'error':'device_regeneration_runtime_not_converged','convergence':{'plan':vars(manager.up_plan).copy()}})
    cli.ConvergencePlan=types.SimpleNamespace(from_dict=lambda value:types.SimpleNamespace(**value))
    class Executor:
        def __init__(self,current):
            self.manager=current;self.journal=types.SimpleNamespace(load=lambda:copy.deepcopy(manager.up_journal))
        def _observe(self,skip_build):return copy.deepcopy(manager.observation)
        def _execute_plan(self,plan,**kwargs):
            manager.up_calls+=1
            self._phase_call('google_converged',getattr(manager,'up_method','reconcile_google'),None,None)
            manager.prepared=True;manager.up_journal=None
            return {'ok':True}
        def _phase_call(self,phase,method,reporter,deadline,**kwargs):
            manager.executed_phases.append(method);return {'ok':True}
    cli.ConvergenceExecutor=Executor;cli._write_up_progress=lambda event:None
    def up(args):
        assert manager.held and manager.operation_lock_held and args.skip_build
        assert store.load('key')['preparation']['status']=='pending'
        executor=cli.ConvergenceExecutor(manager)
        try:
            result=executor._execute_plan(manager.up_plan,retained=copy.deepcopy(manager.up_journal),
                initial_observation=copy.deepcopy(manager.observation),reporter=None,deadline=None,regeneration_capability=None)
            if getattr(manager,'followup_recreate',False):
                dangerous=copy.copy(manager.up_plan);dangerous.runtime_action='recreate'
                result=executor._execute_plan(dangerous,retained=None,initial_observation=copy.deepcopy(manager.observation),
                    reporter=None,deadline=None,regeneration_capability=None)
        except ConvergenceError as error:result={'ok':False,'error':error.code}
        cli.print_json(result);return 0 if result['ok'] else 1
    cli.cmd_up=up
    return cli

class RegenerationRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);h.prepare(self.root)
        with h.db(self.root) as conn:
            conn.execute('INSERT INTO runs(id,created,config,state,iteration,current,updated,resume_at) VALUES(?,?,?,?,?,?,?,?)', (BASE['run_id'],h.now(),json.dumps(BASE),'running',1,1,h.now(),0))
        self.manager=Manager(self.root/'native')
        self.broker=h.Broker(self.manager,lambda *_:None,config=BASE.copy(),app_cache={'old':'cache'})
        self.broker.regeneration_receipts=h.RegenerationReceipts(self.root,BASE['run_id'],1)
        self.broker.regeneration_cli=lambda:native_cli(self.manager)
        self.broker.display_raw_docker=lambda *_args,**_kwargs:contextlib.nullcontext()
        self.broker.display_bootstrap=lambda *_args:None
        self.broker.owned_call=lambda *_args,**_kwargs:{'ok':True}
        readiness=patch.object(h.Broker,'ensure_native_interaction',return_value={'ready':True,'foreground':{'ready':True}})
        self.readiness=readiness.start();self.addCleanup(readiness.stop)

    def test_sdk_default_new_key_per_call_and_explicit_key_retained(self):
        with patch.object(sdk,'request',side_effect=lambda action,args:(action,args)):
            first=sdk.Device().regenerate();second=sdk.Device().regenerate()
            self.assertEqual(first[0],'regenerate');self.assertEqual(first[1][0],0)
            self.assertNotEqual(first[1][1],second[1][1])
            self.assertEqual(sdk.Device().regenerate(180,'retained'),('regenerate',[180,'retained']))

    def test_bash_device_and_official_xenoid_alias_use_same_sdk_receipt_key(self):
        with patch.object(sdk,'request',side_effect=lambda action,args:(action,args)):
            self.assertEqual(sdk.cli('device',['regenerate','180','retained']),('regenerate',[180,'retained']))
            self.assertEqual(sdk.cli('xenoid',['device','regenerate','0','retained']),('regenerate',[0,'retained']))

    def test_committed_result_and_cache_invalidation(self):
        self.broker.resolved_app='old';self.broker.window_offer={'stale':True};self.broker.url='https://private.test'
        result=self.broker.dispatch('regenerate',[0,'key'])
        self.assertTrue(result['ok'] and result['native_ready'] and result['profile_ready'])
        self.assertTrue(result['identities_changed'] and result['boot_id_changed'])
        self.assertEqual(result['phase'],'committed');self.assertFalse(result['containerRecreated'])
        self.assertEqual(self.manager.rotations,1);self.assertIsNone(self.manager.journal)
        self.assertEqual(self.manager.requests,[None]);self.assertFalse(self.manager.operation_lock_held)
        self.assertEqual(self.broker.app_cache,{});self.assertIsNone(self.broker.window_offer)
        self.assertIsNone(self.broker.resolved_app);self.assertIsNone(self.broker.url)
        self.assertFalse(self.broker._hierarchy_focus_observed)

    def test_same_key_replays_durable_receipt_without_second_native_rotation(self):
        first=self.broker.regenerate(180,'same-key')
        self.assertEqual(self.manager.requests,[180])
        # A new Broker models a disconnected/reconnected SDK request.
        later=h.Broker(self.manager,lambda *_:None,config=BASE.copy())
        later.regeneration_receipts=h.RegenerationReceipts(self.root,BASE['run_id'],1)
        later.display_raw_docker=lambda *_args,**_kwargs:contextlib.nullcontext()
        later.display_bootstrap=lambda *_args:None
        result=later.regenerate(0,'same-key')
        self.assertTrue(result['replayed']);self.assertEqual(result['stable_digest_after'],first['stable_digest_after'])
        self.assertEqual(self.manager.commands,1);self.assertEqual(self.manager.rotations,1)

    def test_input_readiness_failure_keeps_native_target_pending_and_retry_does_not_rotate_again(self):
        import qa_input_ready
        self.readiness.side_effect=[qa_input_ready.NativeInputBindingError('reader not ready'),{'ready':True,'foreground':{'ready':True}}]
        with self.assertRaises(h.RegenerationPending) as raised:self.broker.regenerate(0,'input-retry')
        self.assertEqual(raised.exception.code,'native_input_binding_invalid')
        saved=self.broker.regeneration_receipts.load('input-retry')
        self.assertEqual(saved['status'],'pending');self.assertEqual(saved['native_journal']['phase'],'committed')
        self.assertEqual(self.manager.rotations,1);self.assertIsNone(self.manager.journal)
        result=self.broker.regenerate(0,'input-retry')
        self.assertTrue(result['input_ready'] and result['foreground_ready']);self.assertEqual(self.manager.rotations,1);self.assertEqual(self.manager.commands,1)

    def test_committed_replay_rechecks_input_without_a_second_native_rotation(self):
        import qa_input_ready
        self.broker.regenerate(0,'replay-input')
        self.readiness.side_effect=qa_input_ready.NativeInputBindingError('reader missing')
        with self.assertRaises(h.RegenerationPending):self.broker.regenerate(0,'replay-input')
        self.assertEqual(self.manager.rotations,1);self.assertEqual(self.manager.commands,1)
        self.readiness.side_effect=None
        self.assertTrue(self.broker.regenerate(0,'replay-input')['replayed']);self.assertEqual(self.manager.rotations,1)

    @staticmethod
    def cache_result(package,user=0):
        return {'ok':True,'package':package,'user':user,'uid_verified':True,'cache_only':True,'data_cleared':False}

    def test_post_cache_plan_is_durable_before_first_native_command(self):
        from unittest.mock import MagicMock
        plan={'packages':['org.example.one'],'user':0,'timeout':0}
        cli=native_cli(self.manager);original=cli.cmd_device_regenerate
        def command(args):
            self.assertEqual(self.broker.regeneration_receipts.load('plan-first')['post_cache'],plan)
            return original(args)
        cli.cmd_device_regenerate=command;self.broker.regeneration_cli=lambda:cli
        self.broker.clear_cache=MagicMock(return_value=self.cache_result('org.example.one'))
        result=self.broker.regenerate(0,'plan-first',plan)
        self.assertEqual(result['cache_cleanup']['packages'],plan['packages']);self.assertTrue(result['cache_cleanup']['cache_only'])

    def test_partial_cache_failure_resumes_only_unfinished_package_without_new_rotation(self):
        from unittest.mock import MagicMock
        plan={'packages':['org.example.one','org.example.two'],'user':0,'timeout':0}
        self.broker.clear_cache=MagicMock(side_effect=[self.cache_result('org.example.one'),RuntimeError('uncertain cache reply')])
        with self.assertRaises(h.RegenerationPending) as raised:self.broker.regenerate(0,'partial-cache',plan)
        self.assertEqual(raised.exception.code,'regeneration_post_cache_pending')
        saved=self.broker.regeneration_receipts.load('partial-cache')
        self.assertEqual(saved['status'],'pending');self.assertEqual(saved['native_journal']['phase'],'committed')
        self.assertEqual(list(saved['post_cache_completed']),['org.example.one'])
        self.broker.clear_cache.side_effect=None;self.broker.clear_cache.return_value=self.cache_result('org.example.two')
        result=self.broker.regenerate(0,'partial-cache',plan)
        self.assertEqual(self.broker.clear_cache.call_count,3);self.assertEqual(self.broker.clear_cache.call_args.args[0],'org.example.two')
        self.assertEqual(self.manager.rotations,1);self.assertEqual(self.manager.commands,1)
        self.assertEqual(result['cache_cleanup']['packages'],plan['packages'])

    def test_supervisor_resumes_saved_cache_plan_from_sqlite_before_next_app_iteration(self):
        from unittest.mock import MagicMock
        plan={'packages':['org.example.one','org.example.two'],'user':0,'timeout':0}
        self.broker.clear_cache=MagicMock(side_effect=[self.cache_result('org.example.one'),RuntimeError('process interrupted')])
        with self.assertRaises(h.RegenerationPending):self.broker.regenerate(0,'restart-cache',plan)
        with patch.object(h,'trusted_device',return_value=self.manager),patch.object(h.Broker,'regeneration_cli',return_value=native_cli(self.manager)),patch.object(h.Broker,'display_raw_docker',return_value=contextlib.nullcontext()),patch.object(h.Broker,'display_bootstrap'),patch.object(h.Broker,'clear_cache',return_value=self.cache_result('org.example.two')) as clear:
            self.assertTrue(h.resume_regeneration_receipts(self.root,1,BASE,1,{}))
            clear.assert_called_once();self.assertEqual(clear.call_args.args[0],'org.example.two')
        saved=self.broker.regeneration_receipts.load('restart-cache');self.assertEqual(saved['status'],'committed');self.assertEqual(self.manager.rotations,1)

    def test_panel_command_timeout_is_saved_before_native_and_survives_pinned_supervisor_config(self):
        from unittest.mock import MagicMock
        plan={'packages':['org.example.one','org.example.two'],'user':0,'timeout':None}
        self.broker.config['command_timeout']=180
        self.broker.clear_cache=MagicMock(side_effect=[self.cache_result('org.example.one'),RuntimeError('interrupted')])
        with self.assertRaises(h.RegenerationPending):self.broker.regenerate(0,'saved-command-timeout',plan)
        self.assertEqual(self.broker.regeneration_receipts.load('saved-command-timeout')['post_cache_command_timeout'],180)
        with patch.object(h,'trusted_device',return_value=self.manager),patch.object(h.Broker,'regeneration_cli',return_value=native_cli(self.manager)),patch.object(h.Broker,'display_raw_docker',return_value=contextlib.nullcontext()),patch.object(h.Broker,'display_bootstrap'),patch.object(h.Broker,'clear_cache',return_value=self.cache_result('org.example.two')) as clear:
            self.assertTrue(h.resume_regeneration_receipts(self.root,1,BASE,1,{}))
            self.assertEqual(clear.call_args.kwargs['timeout'],180)
        self.assertEqual(self.manager.rotations,1)

    def test_native_commit_before_any_cache_restarts_same_target_not_second_gen(self):
        from unittest.mock import MagicMock
        plan={'packages':['org.example.one'],'user':0,'timeout':0}
        self.broker.clear_cache=MagicMock(side_effect=RuntimeError('worker stopped after native commit'))
        with self.assertRaises(h.RegenerationPending):self.broker.regenerate(0,'gap-cache',plan)
        self.assertIsNone(self.manager.journal);self.assertEqual(self.manager.rotations,1)
        self.broker.clear_cache.side_effect=None;self.broker.clear_cache.return_value=self.cache_result('org.example.one')
        self.assertTrue(self.broker.regenerate(0,'gap-cache',plan)['cache_cleanup']['ok'])
        self.assertEqual(self.manager.commands,1);self.assertEqual(self.manager.rotations,1)

    def test_committed_post_cache_replay_does_not_clear_again(self):
        from unittest.mock import MagicMock
        plan={'packages':['org.example.one'],'user':0,'timeout':0}
        self.broker.clear_cache=MagicMock(return_value=self.cache_result('org.example.one'))
        first=self.broker.regenerate(0,'done-cache',plan);later=self.broker.regenerate(0,'done-cache',plan)
        self.assertEqual(first['cache_cleanup'],later['cache_cleanup']);self.assertTrue(later['replayed']);self.broker.clear_cache.assert_called_once()

    def test_same_key_cannot_change_recorded_cache_obligation(self):
        from unittest.mock import MagicMock
        plan={'packages':['org.example.one'],'user':0,'timeout':0};self.broker.clear_cache=MagicMock(return_value=self.cache_result('org.example.one'))
        self.broker.regenerate(0,'fixed-plan',plan)
        for changed in (None,{'packages':['org.example.two'],'user':0,'timeout':0},{'packages':['org.example.one'],'user':1,'timeout':0}):
            with self.assertRaises(h.RegenerationPending) as raised:self.broker.regenerate(0,'fixed-plan',changed)
            self.assertEqual(raised.exception.code,'regeneration_post_cache_plan_changed')
        self.assertEqual(self.manager.rotations,1)

    def test_current_user_resolved_and_saved_before_first_clear_survives_restart(self):
        from unittest.mock import MagicMock
        import subprocess
        plan={'packages':['org.example.one','org.example.two'],'user':'current','timeout':0}
        self.broker.native_display_exec=MagicMock(return_value=subprocess.CompletedProcess([],0,b'0\n',b''))
        def first(package,**kwargs):
            self.assertEqual(self.broker.regeneration_receipts.load('pinned-user')['post_cache_resolved_user'],0)
            if package.endswith('two'):raise RuntimeError('interrupted')
            return self.cache_result(package)
        self.broker.clear_cache=MagicMock(side_effect=first)
        with self.assertRaises(h.RegenerationPending):self.broker.regenerate(0,'pinned-user',plan)
        self.broker.native_display_exec.side_effect=AssertionError('must not resolve a new current user')
        self.broker.clear_cache.side_effect=lambda package,**kwargs:self.cache_result(package)
        result=self.broker.regenerate(0,'pinned-user',plan)
        self.assertEqual(result['cache_cleanup']['user'],'current');self.assertEqual(result['cache_cleanup']['resolved_user'],0)
        self.assertEqual(self.broker.clear_cache.call_args.kwargs['user'],0)

    def test_finite_overall_deadline_clips_unlimited_cache_budget(self):
        from unittest.mock import MagicMock
        import time
        plan={'packages':['org.example.one'],'user':0,'timeout':0};start=time.monotonic()
        self.broker.clear_cache=MagicMock(return_value=self.cache_result('org.example.one'))
        self.broker.regenerate(180,'clipped-cache',plan)
        deadline=self.broker.clear_cache.call_args.kwargs['_deadline']
        self.assertGreater(deadline,start+179);self.assertLessEqual(deadline,start+181)
        self.assertEqual(self.broker.clear_cache.call_args.kwargs['timeout'],0)

    def test_legacy_receipt_without_plan_is_compatible_and_never_clears_google_implicitly(self):
        from unittest.mock import MagicMock
        self.broker.clear_cache=MagicMock(side_effect=AssertionError('no implicit cache plan'))
        self.broker.regenerate(0,'legacy-cache')
        saved=self.broker.regeneration_receipts.load('legacy-cache');saved.pop('post_cache');self.broker.regeneration_receipts.save('legacy-cache',saved)
        self.assertTrue(self.broker.regenerate(0,'legacy-cache')['replayed']);self.broker.clear_cache.assert_not_called()

    def test_sdk_and_bash_send_explicit_plan_while_legacy_calls_keep_two_arguments(self):
        plan={'packages':['org.example.one'],'user':'current','timeout':0}
        with patch.object(sdk,'request',side_effect=lambda action,args:(action,args)):
            self.assertEqual(sdk.Device().regenerate(0,'key',post_cache=plan),('regenerate',[0,'key',plan]))
            self.assertEqual(sdk.cli('device',['regenerate','0','key',json.dumps(plan)]),('regenerate',[0,'key',plan]))
            self.assertEqual(sdk.Device().regenerate(0,'key'),('regenerate',[0,'key']))

    def test_two_explicit_calls_with_new_keys_in_same_iteration_are_allowed(self):
        self.broker.regenerate(0,'one');self.broker.regenerate(0,'two')
        self.assertEqual(self.manager.rotations,2)
        self.assertTrue(self.broker.regenerate(0,'one')['replayed'])
        self.assertEqual(self.manager.rotations,2)

    def test_prepared_pending_transaction_resumes_the_same_native_target(self):
        self.manager.failure='prepared'
        with self.assertRaises(h.RegenerationPending):self.broker.regenerate(0,'key')
        before=self.broker.regeneration_receipts.load('key')['native_journal']
        self.assertEqual(before['phase'],'prepared')
        result=self.broker.regenerate(0,'key')
        self.assertTrue(result['ok']);self.assertEqual(self.manager.rotations,1)
        self.assertEqual(self.broker.regeneration_receipts.load('key')['native_journal']['transactionId'],before['transactionId'])

    def test_native_commit_checkpoint_is_durable_before_native_journal_clear(self):
        self.manager.failure='committed'
        with self.assertRaises(h.RegenerationPending):self.broker.regenerate(0,'key')
        self.assertEqual(self.broker.regeneration_receipts.load('key')['native_journal']['phase'],'committed')
        self.assertIsNotNone(self.manager.journal)
        self.assertTrue(self.broker.regenerate(0,'key')['ok']);self.assertEqual(self.manager.rotations,1)

    def test_native_journal_unlinked_but_response_lost_reverifies_without_new_command(self):
        self.manager.failure='cleared'
        with self.assertRaises(h.RegenerationPending):self.broker.regenerate(0,'key')
        self.assertIsNone(self.manager.journal)
        self.assertEqual(self.broker.regeneration_receipts.load('key')['native_journal']['phase'],'committed')
        result=self.broker.regenerate(0,'key')
        self.assertTrue(result['ok']);self.assertEqual(self.manager.commands,1);self.assertEqual(self.manager.rotations,1)
        self.assertEqual(self.manager.verifications,1)

    def test_cleaned_native_journal_still_requires_live_google_readiness(self):
        self.manager.failure='cleared'
        with self.assertRaises(h.RegenerationPending):self.broker.regenerate(0,'key')
        self.manager.google_ready=False
        with self.assertRaisesRegex(h.RegenerationPending,'google_not_ready'):self.broker.regenerate(0,'key')
        self.assertEqual(self.manager.commands,1)
        self.manager.google_ready=True
        self.assertTrue(self.broker.regenerate(0,'key')['ok'])

    def test_missing_uncommitted_native_journal_fails_closed(self):
        self.manager.failure='prepared'
        with self.assertRaises(h.RegenerationPending):self.broker.regenerate(0,'key')
        self.manager.journal=None
        with self.assertRaisesRegex(h.RegenerationPending,'checkpoint_without_journal'):self.broker.regenerate(0,'key')
        self.assertEqual(self.manager.commands,1)

    def test_changed_native_transaction_fails_closed(self):
        self.manager.failure='prepared'
        with self.assertRaises(h.RegenerationPending):self.broker.regenerate(0,'key')
        self.manager.journal['transactionId']='different-transaction'
        with self.assertRaisesRegex(h.RegenerationPending,'transaction_changed'):self.broker.regenerate(0,'key')
        self.assertEqual(self.manager.commands,1)

    def test_ambiguous_identity_change_without_checkpoint_never_starts_new_rotation(self):
        binding=self.broker.regeneration_binding(self.manager)
        self.broker.regeneration_receipts.save('key',{'status':'pending','binding':binding,'before_digest':'b'*64,'timeout':0})
        with self.assertRaisesRegex(h.RegenerationPending,'outcome_ambiguous'):self.broker.regenerate(0,'key')
        self.assertEqual(self.manager.commands,0)

    def test_foreign_container_and_immutable_instance_are_never_adopted(self):
        self.manager.cid='b'*64
        with self.assertRaises(h.RuntimeIdentityError):self.broker.regenerate(0,'key')
        self.manager.cid='a'*64;self.manager.context.instance_id='22222222-2222-2222-2222-222222222222'
        with self.assertRaises(h.RuntimeIdentityError):self.broker.regenerate(0,'key')
        self.assertEqual(self.manager.commands,0)

    def test_native_error_messages_are_not_exposed_to_scripts_or_receipts(self):
        self.manager.failure='prepared'
        with self.assertRaises(h.RegenerationPending) as raised:self.broker.regenerate(0,'key')
        self.assertNotIn('password',str(raised.exception));self.assertNotIn('example.test',str(raised.exception))
        self.assertEqual(self.broker.regeneration_receipts.load('key')['last_error'],'native_phase_pending')

    def test_lock_acquisition_errors_are_sanitized_before_any_native_action(self):
        @contextlib.contextmanager
        def busy(**kwargs):
            raise NativeFailure('https://secret:password@example.test')
            yield
        self.manager.instance_operation_lock=busy
        with self.assertRaises(h.RegenerationPending) as raised:self.broker.regenerate(180,'key')
        self.assertNotIn('password',str(raised.exception));self.assertEqual(raised.exception.code,'native_phase_pending')
        self.assertEqual(self.manager.commands,0)

    def test_finite_budget_checkpoints_then_resumes_without_redrawing_native_target(self):
        with patch.object(h.time,'monotonic',side_effect=[0,0,2]):
            with self.assertRaisesRegex(h.RegenerationPending,'regeneration_timeout'):
                self.broker.regenerate(1,'key')
        self.assertEqual(self.broker.regeneration_receipts.load('key')['native_journal']['phase'],'prepared')
        self.assertEqual(self.manager.digest,'a'*64)
        self.assertTrue(self.broker.regenerate(0,'key')['ok'])
        self.assertEqual(self.manager.rotations,1)

    def test_invalid_timeout_and_key_do_not_issue_native_commands(self):
        for timeout,key in ((True,'key'),(-1,'key'),(1.5,'key'),(0,''),(0,None),(0,'space key')):
            with self.subTest(timeout=timeout,key=key),self.assertRaises(ValueError):self.broker.regenerate(timeout,key)
        self.assertEqual(self.manager.commands,0)

    def test_old_pending_receipt_resumes_before_new_run_on_same_immutable_device(self):
        self.manager.failure='prepared'
        with self.assertRaises(h.RegenerationPending):self.broker.regenerate(0,'key')
        current={**BASE,'run_id':'new-run'}
        with h.db(self.root) as conn:
            conn.execute('UPDATE runs SET current=0 WHERE current=1')
            conn.execute('INSERT INTO runs(id,created,config,state,iteration,current,updated,resume_at) VALUES(?,?,?,?,?,?,?,?)',
                         (current['run_id'],h.now(),json.dumps(current),'queued',0,1,h.now(),0))
        with patch.object(h,'trusted_device',return_value=self.manager), \
             patch.object(h.Broker,'regeneration_cli',side_effect=lambda:native_cli(self.manager)), \
             patch.object(h.Broker,'display_raw_docker',return_value=contextlib.nullcontext()), \
             patch.object(h.Broker,'display_bootstrap',return_value=None):
            self.assertFalse(h.resume_regeneration_receipts(self.root,1,current,0,{}))
        self.assertEqual(self.broker.regeneration_receipts.load('key')['status'],'committed')
        self.assertEqual(self.manager.rotations,1)

    def test_old_reserved_revision_resumes_with_original_gate_context(self):
        old={**BASE,'source':'print("old revision")','identity_reservations':True}
        self.broker.config=old.copy()
        with h.db(self.root) as conn:
            conn.execute('UPDATE runs SET config=? WHERE current=1',(json.dumps(old),))
        with patch('identity_reservations.NativeGate') as gate:
            self.manager.failure='prepared'
            with self.assertRaises(h.RegenerationPending):self.broker.regenerate(0,'old-context')
            current={**BASE,'run_id':'new-run','source':'print("new revision")','identity_reservations':False}
            with h.db(self.root) as conn:
                conn.execute('UPDATE runs SET current=0 WHERE current=1')
                conn.execute('INSERT INTO runs(id,created,config,state,iteration,current,updated,resume_at) VALUES(?,?,?,?,?,?,?,?)',
                             (current['run_id'],h.now(),json.dumps(current),'queued',0,1,h.now(),0))
            with patch.object(h,'trusted_device',return_value=self.manager), \
                 patch.object(h.Broker,'regeneration_cli',side_effect=lambda:native_cli(self.manager)), \
                 patch.object(h.Broker,'display_raw_docker',return_value=contextlib.nullcontext()), \
                 patch.object(h.Broker,'display_bootstrap',return_value=None):
                self.assertFalse(h.resume_regeneration_receipts(self.root,1,current,0,{}))
            replay=gate.call_args
            self.assertEqual(replay.args[1]['source'],old['source'])
            self.assertTrue(replay.args[1]['identity_reservations'])
            self.assertEqual(replay.kwargs['context'],self.broker.regeneration_receipts.load('old-context')['identity_reservation_context'])
        self.assertEqual(self.broker.regeneration_receipts.load('old-context')['status'],'committed')
        self.assertEqual(self.manager.rotations,1)

    def test_fresh_safe_up_preparation_is_staged_before_official_command_and_regeneration(self):
        cli=cli_with_preparation(self.manager,self.broker.regeneration_receipts)
        self.broker.regeneration_cli=lambda:cli
        result=self.broker.regenerate(0,'key')
        self.assertTrue(result['ok']);self.assertEqual(self.manager.up_calls,1);self.assertEqual(self.manager.rotations,1)
        self.assertEqual(self.broker.regeneration_receipts.load('key')['preparation']['status'],'committed')

    def test_historical_recreate_can_resume_only_completed_container_and_image_steps(self):
        cli=cli_with_preparation(self.manager,self.broker.regeneration_receipts)
        self.manager.up_plan=prep_plan(runtime_action='recreate',image_action='ensure-desired')
        # The plan's PREVIOUS stock selection is deliberately irrelevant to the
        # validated AFTER-build selection and current container image.
        self.manager.up_plan.selected_image_record={'imageId':'sha256:'+'0'*64}
        self.manager.up_journal=prep_journal(self.manager.up_plan,self.manager)
        self.broker.regeneration_cli=lambda:cli
        self.assertTrue(self.broker.regenerate(0,'key')['ok']);self.assertEqual(self.manager.up_calls,1)

    def test_incomplete_historical_recreate_is_rejected_before_up_or_regeneration(self):
        cli=cli_with_preparation(self.manager,self.broker.regeneration_receipts)
        self.manager.up_plan=prep_plan(runtime_action='recreate',image_action='ensure-desired')
        self.manager.up_journal=prep_journal(self.manager.up_plan,self.manager)
        self.manager.up_journal['completed'].remove('container_created')
        self.broker.regeneration_cli=lambda:cli
        with self.assertRaisesRegex(h.RegenerationPending,'requires_recreation'):self.broker.regenerate(0,'key')
        self.assertEqual(self.manager.up_calls,0);self.assertEqual(self.manager.commands,0)

    def test_fresh_recreate_or_new_image_never_prepares_or_rotates(self):
        for changes in ({'runtime_action':'recreate'},{'image_action':'ensure-desired'},
                        {'desired_image_boot_input_sha256':'0'*64},{'boot_seed_action':'initialize_replace'}):
            with self.subTest(changes=changes):
                cli=cli_with_preparation(self.manager,self.broker.regeneration_receipts)
                self.manager.up_plan=prep_plan(**changes);self.broker.regeneration_cli=lambda:cli
                with self.assertRaises(h.RegenerationPending):self.broker.regenerate(0,'key')
                self.assertEqual(self.manager.up_calls,0);self.assertEqual(self.manager.commands,0)

    def test_late_native_followup_recreate_is_stopped_before_dangerous_execution(self):
        cli=cli_with_preparation(self.manager,self.broker.regeneration_receipts)
        self.manager.followup_recreate=True;self.broker.regeneration_cli=lambda:cli
        with self.assertRaisesRegex(h.RegenerationPending,'requires_recreation'):self.broker.regenerate(0,'key')
        self.assertEqual(self.manager.up_calls,1);self.assertEqual(self.manager.commands,0)
        self.assertEqual(self.manager.executed_phases,['reconcile_google'])

    def test_actual_dangerous_phase_is_fenced_even_if_plan_preview_claims_reuse(self):
        cli=cli_with_preparation(self.manager,self.broker.regeneration_receipts)
        self.manager.up_method='create_owned_container';self.broker.regeneration_cli=lambda:cli
        with self.assertRaisesRegex(h.RegenerationPending,'requires_maintenance'):self.broker.regenerate(0,'key')
        self.assertEqual(self.manager.executed_phases,[]);self.assertEqual(self.manager.commands,0)

    def test_data_or_actual_image_mismatch_is_not_adopted_by_up_preparation(self):
        binding={**self.broker.regeneration_binding(self.manager),'image_id':self.manager.image}
        for mutate in (lambda obs:obs['runtime'].update(dataUuid=None),
                       lambda obs:obs['selectedImageRecord'].update(imageId='sha256:'+'0'*64),
                       lambda obs:obs['runtime'].update(containerId='b'*64)):
            observation=prep_observation(self.manager);mutate(observation)
            with self.assertRaises(h.RegenerationPending):h.Broker.regeneration_preparation_plan(prep_plan(),None,observation,binding)

    def test_native_regeneration_resume_skips_normal_up_preparation(self):
        self.manager.failure='prepared'
        with self.assertRaises(h.RegenerationPending):self.broker.regenerate(0,'key')
        cli=native_cli(self.manager)
        cli._regeneration_runtime_preflight=lambda _manager:(_ for _ in ()).throw(AssertionError('UP preflight must not run during native regeneration resume'))
        self.broker.regeneration_cli=lambda:cli
        self.assertTrue(self.broker.regenerate(0,'key')['ok']);self.assertEqual(self.manager.rotations,1)

    def test_pending_receipt_of_replaced_immutable_device_is_not_resumed(self):
        binding={'instance_id':'22222222-2222-2222-2222-222222222222','resource_tag':BASE['resource_tag'],'container_id':'a'*64}
        self.broker.regeneration_receipts.save('key',{'status':'pending','binding':binding,'before_digest':'a'*64,'timeout':0})
        with patch.object(h,'trusted_device',return_value=self.manager):
            self.assertFalse(h.resume_regeneration_receipts(self.root,1,BASE,0,{}))
        self.assertEqual(self.manager.commands,0)

    def test_sandbox_iteration_metadata_is_optional_and_backward_compatible(self):
        old=h.sandbox_command(1,self.manager,BASE,Path('/fixture'),'script.py','unit')
        new=h.sandbox_command(1,self.manager,BASE,Path('/fixture'),'script.py','unit',iteration=2)
        self.assertFalse(any('XENOID_ITERATION=' in value for value in old))
        self.assertIn('XENOID_ITERATION=2',new)

    def test_official_environment_loader_uses_canonical_sudo_and_only_native_build_keys(self):
        env=self.root/'env.sh';env.write_text('# fixture');env.chmod(0o600)
        output=b'HOME=/root\0PATH=/official/bin:/sbin:/bin\0JAVA_HOME=/official/java\0CARGO_HOME=/official/cargo\0SECRET_FIXTURE=not-adopted\0'
        with patch.object(h,'NATIVE_ENV_FILE',env),patch.dict(os.environ,{'TMPDIR':'/old/tmp','PATH':'/systemd/path'},clear=False),patch.object(h.subprocess,'run',return_value=types.SimpleNamespace(returncode=0,stdout=output)) as run:
            self.assertTrue(h.adopt_native_environment())
            self.assertEqual(run.call_args.args[0][:7],['sudo','-n','-H','--','/bin/bash','-e','-c'])
            self.assertEqual(run.call_args.args[0][-1],str(env))
            self.assertEqual(os.environ['JAVA_HOME'],'/official/java');self.assertNotIn('TMPDIR',os.environ)
            self.assertEqual(os.environ['PATH'],'/official/bin:/sbin:/bin');self.assertEqual(os.environ['HOME'],'/root')
            self.assertNotIn('SECRET_FIXTURE',os.environ)

    def test_official_environment_loader_covers_native_base_and_target_keys_and_unsets_absent(self):
        native_keys={'HOME','PATH','TMPDIR','JAVA_HOME','RUSTUP_HOME','CARGO_HOME','ANDROID_HOME','ANDROID_SDK_ROOT',
            'ANDROID_NDK_HOME','ANDROID_NDK_ROOT','XENOID_NDK_VERSION','ANDROID_API','ANDROID_BUILD_TOOLS',
            'XENOID_ANDROID_API','DEVELOPER_DIR','SDKROOT','DOCKER_HOST','DOCKER_CONTEXT','COLIMA_PROFILE',
            'XENOID_DOCKER_CONTEXT','XENOID_STANDALONE_APK','XENOID_PROXY_CC'}
        env=self.root/'env.sh';env.write_text('# fixture');env.chmod(0o600)
        supplied={key:'canonical-'+key for key in native_keys if key!='TMPDIR'}
        output=b''.join(key.encode()+b'='+value.encode()+b'\0' for key,value in supplied.items())
        with patch.object(h,'NATIVE_ENV_FILE',env),patch.dict(os.environ,{key:'inherited' for key in native_keys},clear=False),patch.object(h.subprocess,'run',return_value=types.SimpleNamespace(returncode=0,stdout=output)):
            self.assertTrue(h.adopt_native_environment())
            for key,value in supplied.items():self.assertEqual(os.environ[key],value)
            self.assertNotIn('TMPDIR',os.environ)

    def test_official_environment_loader_absent_on_legacy_hosts_is_compatible(self):
        with patch.object(h,'NATIVE_ENV_FILE',self.root/'absent'),patch.object(h.subprocess,'run') as run:
            self.assertFalse(h.adopt_native_environment());run.assert_not_called()

    def test_official_environment_loader_canonical_command_failure_is_explicit(self):
        env=self.root/'env.sh';env.write_text('# fixture');env.chmod(0o600)
        with patch.object(h,'NATIVE_ENV_FILE',env),patch.object(h.subprocess,'run',return_value=types.SimpleNamespace(returncode=1,stdout=b'')):
            with self.assertRaisesRegex(RuntimeError,'official native toolchain environment could not be loaded'):h.adopt_native_environment()

    def test_existing_version7_and11_configs_validate_unchanged(self):
        for version in (7,11):self.assertEqual(h.validate({**BASE,'version':version})['version'],version)


class SupervisorRecoveryTests(unittest.TestCase):
    """Execute the real supervisor against durable SQLite, not just the resume helper."""
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);h.prepare(self.root);self.manager=Manager(self.root/'native')
        self.config={**BASE,'repeat_count':2,'interval':0}
        with h.db(self.root) as conn:
            conn.execute('INSERT INTO runs(id,created,config,state,iteration,current,updated,resume_at) VALUES(?,?,?,?,?,?,?,?)',
                         (self.config['run_id'],h.now(),json.dumps(self.config),'running',1,1,h.now(),0))
        self.broker=h.Broker(self.manager,None,config=self.config)
        self.broker.regeneration_receipts=h.RegenerationReceipts(self.root,BASE['run_id'],1)
        for target, replacement in [('trusted_device',lambda *_args:self.manager)]:
            patcher=patch.object(h,target,side_effect=replacement);patcher.start();self.addCleanup(patcher.stop)
        for target,replacement in [('regeneration_cli',lambda *_args:native_cli(self.manager)),
                                   ('display_raw_docker',lambda *_args,**_kwargs:contextlib.nullcontext()),
                                   ('display_bootstrap',lambda *_args:None),('ensure_native_interaction',lambda *_args,**_kwargs:{'ready':True})]:
            patcher=patch.object(h.Broker,target,replacement);patcher.start();self.addCleanup(patcher.stop)
        signals=patch.object(h.signal,'signal');signals.start();self.addCleanup(signals.stop)
        boot=patch.object(h.Broker,'adb',return_value='1');boot.start();self.addCleanup(boot.stop)
    def state(self):
        with h.db(self.root) as conn:return dict(conn.execute('SELECT * FROM runs WHERE current=1').fetchone())
    def config_save(self,**values):
        self.config.update(values)
        with h.db(self.root) as conn:conn.execute('UPDATE runs SET config=? WHERE current=1',(json.dumps(self.config),))
    def cache_result(self,package):return {'ok':True,'package':package,'user':0,'uid_verified':True,'cache_only':True,'data_cleared':False}
    def partial_cache(self,timeout=0):
        from unittest.mock import MagicMock
        plan={'packages':['org.example.one','org.example.two'],'user':0,'timeout':0}
        self.broker.clear_cache=MagicMock(side_effect=[self.cache_result('org.example.one'),RuntimeError('interrupted')])
        with self.assertRaises(h.RegenerationPending):self.broker.regenerate(timeout,'fixed-original-key',plan)
        return plan
    def test_startup_cache_pending_retries_same_key_partial_progress_then_runs_next_iteration(self):
        plan=self.partial_cache();calls=[];sleeps=[];original=h.Broker.regenerate
        def replay(broker,*args,**kwargs):calls.append((args,kwargs));return original(broker,*args,**kwargs)
        def pause(delay):self.assertFalse(self.manager.held);sleeps.append(delay)
        def app(*_args,**_kwargs):
            self.assertEqual(self.broker.regeneration_receipts.load('fixed-original-key')['status'],'committed');return 0
        with patch.object(h.Broker,'regenerate',replay),patch.object(h.Broker,'clear_cache',side_effect=[RuntimeError('temporary PM busy'),self.cache_result('org.example.two')]) as clear,patch.object(h.time,'sleep',side_effect=pause),patch.object(h,'run_iteration',side_effect=app) as run:
            self.assertIsNone(h.supervisor(self.root,1))
        self.assertEqual(self.state()['state'],'completed');self.assertEqual(self.state()['iteration'],2)
        self.assertEqual([call[0] for call in calls],[(0,'fixed-original-key',plan)]*2)
        self.assertEqual([call.args[0] for call in clear.call_args_list],['org.example.two']*2)
        self.assertEqual(sleeps,[0]);run.assert_called_once();self.assertEqual(run.call_args.args[4],2)
        self.assertEqual(self.manager.rotations,1);self.assertEqual(self.manager.commands,1)
    def test_startup_native_pending_retries_same_target_then_continues(self):
        self.manager.failure='prepared'
        with self.assertRaises(h.RegenerationPending):self.broker.regenerate(0,'fixed-native-key')
        self.manager.failure='prepared'
        with patch.object(h.time,'sleep') as pause,patch.object(h,'run_iteration',return_value=0) as run:
            h.supervisor(self.root,1)
        self.assertEqual(self.state()['state'],'completed');self.assertEqual(self.manager.rotations,1)
        self.assertEqual(self.manager.commands,3);pause.assert_called_once_with(0);run.assert_called_once()
    def test_finite_recovery_uses_one_deadline_clips_lock_cache_and_stops_without_app(self):
        self.partial_cache(timeout=10);self.config_save(interval=2);clock=[0.0];deadlines=[];self.manager.requests.clear()
        def fail(package,**kwargs):deadlines.append(kwargs['_deadline']);clock[0]+=3;raise RuntimeError('still busy')
        def pause(delay):self.assertFalse(self.manager.held);clock[0]+=delay
        with patch.object(h.time,'monotonic',side_effect=lambda:clock[0]),patch.object(h.time,'sleep',side_effect=pause),patch.object(h.Broker,'clear_cache',side_effect=fail),patch.object(h,'run_iteration') as app:
            h.supervisor(self.root,1)
        self.assertEqual(self.state()['state'],'failed');self.assertEqual(clock[0],10)
        self.assertEqual(deadlines,[10,10]);self.assertEqual(self.manager.requests,[10,5]);self.assertEqual(self.manager.command_budgets,[10,5]);app.assert_not_called()
        receipt=self.broker.regeneration_receipts.load('fixed-original-key');self.assertEqual(receipt['status'],'pending')
        self.assertEqual(list(receipt['post_cache_completed']),['org.example.one']);self.assertEqual(self.manager.rotations,1)
    def test_stop_persisted_during_wait_exits_without_retry_or_failed_state(self):
        self.partial_cache();self.config_save(interval=7)
        def stop(delay):self.assertEqual(delay,7);h.update(self.root,BASE['run_id'],'stopped',1,'admin stop')
        with patch.object(h.Broker,'clear_cache',side_effect=RuntimeError('busy')) as clear,patch.object(h.time,'sleep',side_effect=stop),patch.object(h,'run_iteration') as app:
            h.supervisor(self.root,1)
        self.assertEqual(self.state()['state'],'stopped');clear.assert_called_once();app.assert_not_called()
        self.assertEqual(self.broker.regeneration_receipts.load('fixed-original-key')['status'],'pending')
    def test_replaced_current_config_during_wait_stops_old_worker(self):
        self.partial_cache()
        def replace(delay):
            changed={**self.config,'version':99}
            with h.db(self.root) as conn:conn.execute('UPDATE runs SET config=? WHERE current=1',(json.dumps(changed),))
        with patch.object(h.Broker,'clear_cache',side_effect=RuntimeError('busy')) as clear,patch.object(h.time,'sleep',side_effect=replace),patch.object(h,'run_iteration') as app:
            h.supervisor(self.root,1)
        self.assertEqual(self.state()['state'],'waiting');clear.assert_called_once();app.assert_not_called()
    def test_replaced_current_run_during_wait_stops_old_worker(self):
        self.partial_cache()
        def replace(delay):
            with h.db(self.root) as conn:conn.execute('UPDATE runs SET id=? WHERE current=1',('replacement-run',))
        with patch.object(h.Broker,'clear_cache',side_effect=RuntimeError('busy')) as clear,patch.object(h.time,'sleep',side_effect=replace),patch.object(h,'run_iteration') as app:
            h.supervisor(self.root,1)
        self.assertEqual(self.state()['id'],'replacement-run');self.assertEqual(self.state()['state'],'waiting');clear.assert_called_once();app.assert_not_called()
    def test_sigterm_interrupts_backoff_leaves_receipt_pending(self):
        self.partial_cache();self.config_save(interval=1000)
        with patch.object(h.Broker,'clear_cache',side_effect=RuntimeError('busy')) as clear,patch.object(h.time,'sleep',side_effect=h.Shutdown),patch.object(h,'run_iteration') as app:
            h.supervisor(self.root,1)
        self.assertEqual(self.state()['state'],'waiting');clear.assert_called_once();app.assert_not_called()
        self.assertEqual(self.broker.regeneration_receipts.load('fixed-original-key')['status'],'pending')
    def test_binding_failure_remains_fatal_without_retry(self):
        self.partial_cache();self.manager.cid='b'*64
        with patch.object(h.time,'sleep') as pause,patch.object(h,'run_iteration') as app:
            h.supervisor(self.root,1)
        self.assertEqual(self.state()['state'],'failed');pause.assert_not_called();app.assert_not_called()
    def test_new_supervisor_after_interrupted_cache_wait_resumes_saved_key(self):
        self.partial_cache()
        with patch.object(h.Broker,'clear_cache',side_effect=RuntimeError('busy')),patch.object(h.time,'sleep',side_effect=h.Shutdown):
            h.supervisor(self.root,1)
        with patch.object(h.Broker,'clear_cache',return_value=self.cache_result('org.example.two')) as clear,patch.object(h,'run_iteration',return_value=0):
            h.supervisor(self.root,1)
        self.assertEqual(self.state()['state'],'completed');clear.assert_called_once();self.assertEqual(self.manager.rotations,1)

if __name__=='__main__':unittest.main()
