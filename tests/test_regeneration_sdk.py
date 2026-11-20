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
        self.manager=Manager(self.root/'native')
        self.broker=h.Broker(self.manager,lambda *_:None,config=BASE.copy(),app_cache={'old':'cache'})
        self.broker.regeneration_receipts=h.RegenerationReceipts(self.root,BASE['run_id'],1)
        self.broker.regeneration_cli=lambda:native_cli(self.manager)
        self.broker.display_raw_docker=lambda *_args,**_kwargs:contextlib.nullcontext()
        self.broker.display_bootstrap=lambda *_args:None
        self.broker.owned_call=lambda *_args,**_kwargs:{'ok':True}

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
        with patch.object(h,'trusted_device',return_value=self.manager), \
             patch.object(h.Broker,'regeneration_cli',side_effect=lambda:native_cli(self.manager)), \
             patch.object(h.Broker,'display_raw_docker',return_value=contextlib.nullcontext()), \
             patch.object(h.Broker,'display_bootstrap',return_value=None):
            self.assertFalse(h.resume_regeneration_receipts(self.root,1,current,0,{}))
        self.assertEqual(self.broker.regeneration_receipts.load('key')['status'],'committed')
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

if __name__=='__main__':unittest.main()
