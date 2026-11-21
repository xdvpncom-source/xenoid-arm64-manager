"""Actual SQLite post-Google recovery and Android command ordering."""
import contextlib
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'qa'))
import qa_google_reset as g
import script_host as h
import script_runtime as sdk
import test_regeneration_sdk as r

PLAN = {'clear_packages':['com.google.android.gms','com.google.android.gsf'],
        'user':0,'delay_seconds':0,'timeout':0,'poll_seconds':1}
CACHE = {'packages':PLAN['clear_packages'],'user':0,'timeout':0}

class Android:
    def __init__(self):self.calls=[];self.enabled=False;self.fail=None;self.boot='new-boot';self.before_clear=None
    def __call__(self,args,**kwargs):
        self.calls.append(list(args));rc=0;out=''
        if args == ['cat','/proc/sys/kernel/random/boot_id']:out=self.boot
        elif args == ['am','get-current-user']:out='0'
        elif args[:5] == ['cmd','package','list','packages','-U']:out='package:'+args[-1]+' uid:10001'
        elif args[:2] == ['pm','clear']:
            if self.before_clear:self.before_clear(args[-1])
            if self.fail==args[-1]:self.fail=None;rc=1;out='not cleared'
            else:self.enabled=False;out='Success'
        elif args[:3] == ['cmd','activity','start-service']:self.enabled=True;out='Starting service'
        elif args[:2] == ['sh','-c']:out='enabled'if self.enabled else'disabled'
        else:raise AssertionError(args)
        return subprocess.CompletedProcess(args,rc,out.encode(),b'')
    def clears(self):return [c[-1]for c in self.calls if c[:2]==['pm','clear']]
    def provisions(self):return [c for c in self.calls if c[:3]==['cmd','activity','start-service']]

class Clock:
    def __init__(self):self.value=100.;self.waits=[];self.stopped=False;self.stop_at=None
    def time(self):return self.value
    def monotonic(self):return self.value
    def is_set(self):return self.stopped
    def wait(self,seconds):
        self.waits.append(seconds);self.value+=seconds
        if self.stop_at is not None and self.value>=self.stop_at:self.stopped=True
        return self.stopped

class ModuleTests(unittest.TestCase):
    def test_stage_order_delays_are_persisted_and_no_data_clear_after_registration(self):
        clock=Clock();android=Android();receipt={};saved=[];commands=[]
        def run(args):commands.append((clock.value,list(args)));return android(args)
        with patch.object(g,'time',clock):
            answer=g.complete(g.normalize_plan({**PLAN,'delay_seconds':2}),receipt,
                              lambda:saved.append(copy.deepcopy(receipt)),run,clock)
        clears=[x for x in commands if x[1][:2]==['pm','clear']];provision=next(x for x in commands if x[1][:3]==['cmd','activity','start-service'])
        self.assertEqual([x[1][-1]for x in clears],PLAN['clear_packages'])
        self.assertGreaterEqual(clears[0][0]-100,2);self.assertGreaterEqual(clears[1][0]-clears[0][0],2)
        self.assertGreaterEqual(provision[0]-clears[1][0],2)
        self.assertTrue(any(x['post_google_progress'].get('registration_started')and not x['post_google_progress'].get('checkin_requested')for x in saved))
        self.assertTrue(answer['registration_enabled']and answer['checkin_requested']);self.assertIsNone(answer['network_registered'])
        old=android.clears()[:]
        with patch.object(g,'time',clock):g.complete(g.normalize_plan({**PLAN,'delay_seconds':2}),receipt,lambda:None,android,clock)
        self.assertEqual(android.clears(),old);self.assertEqual(len(android.provisions()),1)
    def test_stop_during_delay_and_resume_waits_only_remaining_time(self):
        clock=Clock();clock.stop_at=101;receipt={};android=Android();plan=g.normalize_plan({**PLAN,'delay_seconds':2})
        with patch.object(g,'time',clock):
            with self.assertRaisesRegex(RuntimeError,'stopping'):g.complete(plan,receipt,lambda:None,android,clock)
            self.assertEqual(android.clears(),[]);self.assertEqual(receipt['post_google_progress']['waits']['after_cache']['not_before'],102)
            clock.stopped=False;clock.stop_at=None;g.complete(plan,receipt,lambda:None,android,clock)
        self.assertEqual(android.clears(),PLAN['clear_packages'])
    def test_finite_deadline_clips_delay(self):
        clock=Clock();android=Android()
        with patch.object(g,'time',clock):
            with self.assertRaises(TimeoutError):g.complete(g.normalize_plan({**PLAN,'delay_seconds':2}),{},lambda:None,android,clock,deadline=101)
        self.assertLessEqual(clock.value,101.0001);self.assertFalse(android.calls)
    def test_corrupt_registration_boundary_never_reclears(self):
        receipt={'post_google_progress':{'resolved_user':0,'registration_started':True,'cleared_packages':{},'waits':{}}};android=Android()
        with self.assertRaisesRegex(RuntimeError,'inconsistent'):g.complete(g.normalize_plan(PLAN),receipt,lambda:None,android,threading.Event())
        self.assertFalse(android.calls)
    def test_unsupported_or_duplicate_plan_and_delay_validation(self):
        self.assertEqual(g.normalize_plan({**PLAN,'clear_packages':PLAN['clear_packages']*2}),PLAN)
        for changes in ({'delay_seconds':float('nan')},{'delay_seconds':-1},{'poll_seconds':0},{'timeout':True},{'clear_packages':['bad;command']},{'other':True}):
            with self.assertRaises(ValueError):g.normalize_plan({**PLAN,**changes})
    def test_plan_timeout_bounds_whole_stage_and_is_not_renewed_on_retry(self):
        clock=Clock();android=Android();receipt={};plan=g.normalize_plan({**PLAN,'delay_seconds':2,'timeout':1})
        with patch.object(g,'time',clock):
            with self.assertRaises(TimeoutError):g.complete(plan,receipt,lambda:None,android,clock)
            self.assertEqual(receipt['post_google_progress']['not_after'],101)
            clock.value=105
            with self.assertRaises(TimeoutError):g.complete(plan,receipt,lambda:None,android,clock)
        self.assertEqual(receipt['post_google_progress']['not_after'],101);self.assertFalse(android.calls)
    def test_completed_poststage_returns_saved_proof_after_its_deadline_without_io(self):
        clock=Clock();android=Android();receipt={};plan=g.normalize_plan({**PLAN,'timeout':1})
        with patch.object(g,'time',clock):
            first=g.complete(plan,receipt,lambda:None,android,clock)
            clock.value=200
            calls=copy.deepcopy(android.calls)
            second=g.complete(plan,receipt,lambda:None,android,clock)
        self.assertEqual(first,second);self.assertEqual(android.calls,calls)

    def test_exit_zero_error_is_not_registration_request_success(self):
        android=Android();receipt={}
        def run(args):
            if args[:3]==['cmd','activity','start-service']:
                return subprocess.CompletedProcess(args,0,b'Error: Not found; no service started.',b'')
            return android(args)
        with self.assertRaisesRegex(RuntimeError,'request_failed'):g.complete(g.normalize_plan(PLAN),receipt,lambda:None,run,threading.Event())
        self.assertFalse(receipt['post_google_progress'].get('checkin_requested'));self.assertFalse(receipt['post_google_progress'].get('completed'))
    def test_interrupted_provision_retries_registration_only(self):
        android=Android();clock=Clock();receipt={};blocked=[True]
        def run(args):
            answer=android(args)
            if blocked[0]and args[:3]==['cmd','activity','start-service']:android.enabled=False
            return answer
        with patch.object(g,'time',clock):
            with self.assertRaisesRegex(RuntimeError,'switch_pending'):g.complete(g.normalize_plan(PLAN),receipt,lambda:None,run,clock)
            clears=android.clears()[:];blocked[0]=False
            answer=g.complete(g.normalize_plan(PLAN),receipt,lambda:None,run,clock)
        self.assertTrue(answer['registration_enabled']);self.assertEqual(android.clears(),clears);self.assertEqual(len(android.provisions()),2)

    def test_uid_change_does_not_record_clear_success(self):
        android=Android();count=[0]
        def run(args):
            result=android(args)
            if args[:5]==['cmd','package','list','packages','-U']:
                count[0]+=1
                if count[0]>1:return subprocess.CompletedProcess(args,0,('package:'+args[-1]+' uid:20002').encode(),b'')
            return result
        receipt={}
        with self.assertRaisesRegex(RuntimeError,'uid_changed'):g.complete(g.normalize_plan(PLAN),receipt,lambda:None,run,threading.Event())
        self.assertFalse(receipt['post_google_progress']['cleared_packages']);self.assertFalse(android.provisions())

class RuntimeTests(unittest.TestCase):
    def setUp(self):
        r.RegenerationRuntimeTests.setUp(self)
        self.android=Android();self.broker.native_display_exec=lambda manager,args,**kwargs:self.android(args,**kwargs)
        self.broker.clear_cache=Mock(side_effect=lambda package,**kwargs:r.RegenerationRuntimeTests.cache_result(package,kwargs['user']))
        self.plan=g.normalize_plan(PLAN)
    def test_native_checkpoint_and_cache_precede_first_full_clear(self):
        def before(package):
            receipt=self.broker.regeneration_receipts.load('google')
            self.assertEqual(receipt['native_completion']['result']['phase'],'committed')
            self.assertEqual(set(receipt['post_cache_completed']),set(PLAN['clear_packages']))
        self.android.before_clear=before
        result=self.broker.regenerate(0,'google',CACHE,self.plan)
        self.assertTrue(result['cache_cleanup']['cache_only']);self.assertFalse(result['cache_cleanup']['data_cleared'])
        self.assertTrue(result['google_reset']['data_cleared']);self.assertTrue(result['google_reset']['registration_enabled'])
        self.assertEqual(self.manager.rotations,1)
    def test_partial_clear_resumes_only_remaining_and_skips_old_google_verifier(self):
        self.android.fail=PLAN['clear_packages'][1]
        with self.assertRaises(h.RegenerationPending)as raised:self.broker.regenerate(0,'partial',CACHE,self.plan)
        self.assertEqual(raised.exception.code,'regeneration_post_google_pending')
        saved=self.broker.regeneration_receipts.load('partial');self.assertEqual(list(saved['post_google_progress']['cleared_packages']),PLAN['clear_packages'][:1])
        cli=r.native_cli(self.manager);cli._verify_regeneration_target_state=Mock(side_effect=AssertionError('pre-clear Google target must not be checked'))
        cli._regeneration_google_runtime_postcondition=Mock(side_effect=AssertionError('old offline Google mode must not be checked'))
        self.broker.regeneration_cli=lambda:cli
        answer=self.broker.regenerate(0,'partial',CACHE,self.plan)
        self.assertTrue(answer['google_reset']['ok']);self.assertEqual(self.android.clears(),[PLAN['clear_packages'][0],PLAN['clear_packages'][1],PLAN['clear_packages'][1]])
        self.assertEqual(self.manager.rotations,1);self.assertEqual(self.manager.commands,1);self.assertEqual(self.broker.clear_cache.call_count,2)
    def test_committed_lost_response_replay_never_clears_or_registers_again(self):
        first=self.broker.regenerate(0,'complete',CACHE,self.plan);calls=copy.deepcopy(self.android.calls)
        second=self.broker.regenerate(0,'complete',CACHE,self.plan)
        self.assertTrue(second['replayed']);self.assertEqual(first['google_reset'],second['google_reset']);self.assertEqual(self.android.calls,calls)
    def test_native_boot_change_on_partial_poststage_never_clears_again(self):
        self.android.fail=PLAN['clear_packages'][1]
        with self.assertRaises(h.RegenerationPending):self.broker.regenerate(0,'boot',CACHE,self.plan)
        clears=self.android.clears()[:];self.android.boot='foreign-boot'
        with self.assertRaises(h.RegenerationPending)as raised:self.broker.regenerate(0,'boot',CACHE,self.plan)
        self.assertEqual(raised.exception.code,'regeneration_native_boot_changed');self.assertEqual(self.android.clears(),clears)
    def test_registration_started_with_missing_cache_record_fails_before_cache_reclear(self):
        self.broker.regenerate(0,'corrupt',CACHE,self.plan)
        saved=self.broker.regeneration_receipts.load('corrupt');saved['status']='pending';saved['post_cache_completed'].pop(PLAN['clear_packages'][0]);self.broker.regeneration_receipts.save('corrupt',saved)
        before=self.broker.clear_cache.call_count;clears=self.android.clears()[:]
        with self.assertRaises(h.RegenerationPending)as raised:self.broker.regenerate(0,'corrupt',CACHE,self.plan)
        self.assertEqual(raised.exception.code,'post_google_cache_progress_inconsistent')
        self.assertEqual(self.broker.clear_cache.call_count,before);self.assertEqual(self.android.clears(),clears)
    def test_fresh_completion_rechecks_official_input_after_data_reset(self):
        self.readiness.reset_mock()
        self.broker.regenerate(0,'recheck',CACHE,self.plan)
        self.assertEqual(self.readiness.call_count,2)

    def test_same_key_plan_cannot_change_and_old_call_shapes_remain(self):
        self.broker.regenerate(0,'fixed',CACHE,self.plan)
        with self.assertRaises(h.RegenerationPending)as raised:self.broker.regenerate(0,'fixed',CACHE,{**self.plan,'delay_seconds':2})
        self.assertEqual(raised.exception.code,'regeneration_post_google_plan_changed')
        with patch.object(sdk,'request',side_effect=lambda action,args:(action,args)):
            self.assertEqual(sdk.Device().regenerate(0,'a'),('regenerate',[0,'a']))
            self.assertEqual(sdk.Device().regenerate(0,'a',post_cache=CACHE),('regenerate',[0,'a',CACHE]))
            self.assertEqual(sdk.Device().regenerate(0,'a',post_google=self.plan),('regenerate',[0,'a',None,self.plan]))
            self.assertEqual(sdk.cli('device',['regenerate','0','a',json.dumps(CACHE),json.dumps(self.plan)]),('regenerate',[0,'a',CACHE,self.plan]))
    def test_real_sqlite_supervisor_resume_completes_before_next_app_without_new_gen(self):
        self.android.fail=PLAN['clear_packages'][1]
        with self.assertRaises(h.RegenerationPending):self.broker.regenerate(0,'resume',CACHE,self.plan)
        with patch.object(h,'trusted_device',return_value=self.manager),patch.object(h.Broker,'regeneration_cli',return_value=r.native_cli(self.manager)),patch.object(h.Broker,'display_raw_docker',return_value=contextlib.nullcontext()),patch.object(h.Broker,'display_bootstrap'),patch.object(h.Broker,'native_display_exec',side_effect=lambda manager,args,**kwargs:self.android(args,**kwargs)),patch.object(h.Broker,'clear_cache',side_effect=AssertionError('cache already completed')):
            self.assertTrue(h.resume_regeneration_receipts(self.root,1,r.BASE,1,{}))
        receipt=self.broker.regeneration_receipts.load('resume');self.assertEqual(receipt['status'],'committed');self.assertEqual(self.manager.rotations,1)
    def _complete_previous(self):
        self.broker.regenerate(0,'previous',CACHE,self.plan)
        cli=r.native_cli(self.manager)
        client=types.SimpleNamespace(google_identity_inspect=Mock(return_value={'ok':False,'error':'google_identity_gsf_id_invalid'}),google_identity_activate=Mock())
        target='1234567890';client.google_identity_activate.side_effect=lambda target,**kwargs:(setattr(self.android,'enabled',False)or{'ok':True,'offlineSeeded':True,'gsfAndroidIdSha256':h.hashlib.sha256(target.encode()).hexdigest()})
        cli.GoogleBindingStore=lambda *_:types.SimpleNamespace(regeneration_gsf_android_id=lambda tx:target)
        self.manager.daemon_client=lambda **kwargs:client
        return cli,client
    def test_next_fresh_gen_restores_only_recorded_previous_baseline_before_new_rotation(self):
        cli,client=self._complete_previous();self.broker.regeneration_cli=lambda:cli
        result=self.broker.regenerate(0,'next',CACHE,self.plan)
        client.google_identity_activate.assert_called_once();self.assertEqual(self.manager.rotations,2)
        self.assertTrue(self.broker.regeneration_receipts.load('next')['google_reset_preparation']['completed'])
        self.assertTrue(result['google_reset']['registration_enabled'])
    def test_changed_core_never_uses_previous_google_seed(self):
        cli,client=self._complete_previous();self.broker.regeneration_cli=lambda:cli
        self.manager.digest='e'*64
        self.broker.regenerate(0,'other-core')
        client.google_identity_activate.assert_not_called()
    def test_already_restored_baseline_with_disabled_switch_is_not_activated_twice(self):
        cli,client=self._complete_previous();self.broker.regeneration_cli=lambda:cli;self.android.enabled=False
        client.google_identity_inspect.return_value={'ok':True,'offlineSeeded':True}
        self.broker.regenerate(0,'next')
        client.google_identity_activate.assert_not_called();self.assertEqual(self.manager.rotations,2)

if __name__=='__main__':unittest.main()
