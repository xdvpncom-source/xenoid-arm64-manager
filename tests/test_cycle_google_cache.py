"""Editable workflow v13: cache-only Google cleanup after committed regeneration."""
import contextlib
import hashlib
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch
from test_vpn_cycle import Clock,FakeDevice

SOURCE=Path(__file__).resolve().parents[1]/'qa/vpn-cycle.py'
spec=importlib.util.spec_from_file_location('vpn_cache_cycle_v13',SOURCE)
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)


def committed(**changes):
    return {'ok':True,'phase':'committed','runtimeOnly':True,'containerRecreated':False,
            'native_ready':True,'profile_ready':True,'container_id_preserved':True,
            'instance_id_preserved':True,'resource_tag_preserved':True,
            'identities_changed':True,'input_ready':True,'foreground_ready':True,**changes}


class CacheDevice(FakeDevice):
    """Model SDK durable receipts, separate from the editable workflow logic."""
    def __init__(self,clock):
        super().__init__(clock);self.regenerate_results=[];self.cache_results={};self.native_rotation_keys=set()
        self.receipts={};self.cleanup_overrides=[];self._inside_regenerate=False
    def regenerate(self,*,timeout,operation_key,post_cache=None):
        import copy
        self.trace.append(('regenerate',self.clock.now,timeout,operation_key,copy.deepcopy(post_cache)))
        receipt=self.receipts.setdefault(operation_key,{'post_cache':copy.deepcopy(post_cache),'completed':set()})
        if receipt['post_cache']!=post_cache:raise RuntimeError('regeneration_post_cache_plan_changed')
        answer=self.regenerate_results.pop(0) if self.regenerate_results else committed()
        if isinstance(answer,BaseException):raise answer
        if answer.get('phase')=='committed' and operation_key not in self.native_rotation_keys:
            self.native_rotation_keys.add(operation_key);self.trace.append(('native_rotation',self.clock.now,operation_key))
            self.display_size=None;self.display_density=None
        if post_cache is not None and answer.get('input_ready') and answer.get('foreground_ready'):
            self._inside_regenerate=True
            try:
                for package in post_cache['packages']:
                    if package in receipt['completed']:continue
                    result=self.clear_cache(package,user=post_cache['user'],timeout=post_cache['timeout'])
                    if (not isinstance(result,dict) or result.get('ok') is not True or result.get('package')!=package
                            or result.get('cache_only') is not True or result.get('data_cleared') is not False
                            or result.get('uid_verified') is not True):
                        raise RuntimeError('invalid cache-only success')
                    receipt['completed'].add(package)
            except (RuntimeError,OSError) as error:
                raise RuntimeError('Official regeneration pending: regeneration_post_cache_pending') from error
            finally:self._inside_regenerate=False
            cleanup={'ok':True,'cache_only':True,'data_cleared':False,'packages':list(post_cache['packages']),'user':post_cache['user']}
            if self.cleanup_overrides:cleanup=self.cleanup_overrides.pop(0)
            answer={**answer,'cache_cleanup':cleanup}
        return answer
    def clear_cache(self,package,*,user='current',timeout=None):
        if not self._inside_regenerate:raise AssertionError('Workflow must pass a durable plan, not clear cache externally')
        self.trace.append(('cache',self.clock.now,package,user,timeout))
        results=self.cache_results.get(package,[])
        answer=results.pop(0) if results else {'ok':True,'package':package,'user':user,'cache_only':True,'data_cleared':False,'uid_verified':True}
        if isinstance(answer,BaseException):raise answer
        return answer


class GoogleCacheWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.stack=contextlib.ExitStack();self.addCleanup(self.stack.close)
        values={'REGENERATE_ENABLED':True,'REGENERATE_RANDOM_RANGE':(1,10),'REGENERATE_TRIGGER':5,
                'REGENERATE_TIMEOUT':0,'REGENERATE_RETRY_SECONDS':15,'DISPLAY_FPS':20,'DISPLAY_SCALE':'minimum',
                'MIN_DISPLAY_SIDE':200,'MIN_DISPLAY_DPI':72,'GOOGLE_CACHE_PACKAGES':('com.google.android.gms','com.google.android.gsf'),
                'GOOGLE_CACHE_USER':'current','GOOGLE_CACHE_TIMEOUT':0,'GOOGLE_CACHE_RETRY_SECONDS':15,
                'GOOGLE_DATA_RESET_ENABLED':False}
        for name,value in values.items():self.stack.enter_context(patch.object(m,name,value))
        self.clock=Clock();self.stack.enter_context(patch.object(m,'time',self.clock))
        self.device=CacheDevice(self.clock);self.cycle=m.Cycle(self.device,'qa.vpn')
    def regenerate(self):
        with patch.object(m.random,'randint',return_value=5):return self.cycle.regenerate_after_success()
    def cache_calls(self):return [row for row in self.device.trace if row[0]=='cache']
    def regen_calls(self):return [row for row in self.device.trace if row[0]=='regenerate']
    def test_defaults_clear_only_gms_and_gsf(self):
        self.assertTrue(self.regenerate())
        self.assertEqual([row[2] for row in self.cache_calls()],['com.google.android.gms','com.google.android.gsf'])
        self.assertFalse(any(row[2]=='com.android.vending' for row in self.cache_calls()))
        self.assertFalse(any(row[0]=='clear' for row in self.device.trace))
    def test_nonmatching_draws_never_clear_google_cache(self):
        for value in (1,2,3,4,6,7,8,9,10):
            with self.subTest(value=value),patch.object(m.random,'randint',return_value=value):
                self.assertFalse(self.cycle.regenerate_after_success())
        self.assertEqual(self.cache_calls(),[]);self.assertEqual(self.regen_calls(),[])
    def test_disabled_regeneration_never_draws_or_clears_cache(self):
        with patch.object(m,'REGENERATE_ENABLED',False),patch.object(m.random,'randint') as draw:
            self.assertFalse(self.cycle.regenerate_after_success());draw.assert_not_called()
        self.assertEqual(self.cache_calls(),[])
    def test_empty_package_tuple_disables_only_cache_phase(self):
        with patch.object(m,'GOOGLE_CACHE_PACKAGES',()):self.assertTrue(self.regenerate())
        self.assertEqual(len(self.regen_calls()),1);self.assertEqual(self.cache_calls(),[])
    def test_full_cycle_completes_only_after_native_ready_display_and_cache(self):
        def draw(low,high):return 97 if (low,high)==(90,120) else 5
        with patch.object(m.random,'randint',side_effect=draw):self.cycle.run()
        trace=self.device.trace
        regen=next(i for i,row in enumerate(trace)if row[0]=='regenerate')
        first_cache=next(i for i,row in enumerate(trace)if row[0]=='cache')
        last_display_read=max(i for i,row in enumerate(trace)if row[0]=='adb' and row[2]==['shell','wm','density'])
        self.assertLess(regen,first_cache);self.assertLess(first_cache,last_display_read)
        self.assertEqual(trace[-1][2],'QA cycle completed successfully')
        self.assertEqual([row[2]for row in trace if row[0]=='clear'],['qa.vpn'])
        self.assertEqual(len(self.regen_calls()),1);self.assertEqual(len(self.cache_calls()),2)
    def test_regeneration_pending_does_not_touch_google_cache(self):
        self.device.regenerate_results=[RuntimeError('pending'),OSError('reply lost'),committed()]
        self.regenerate();self.assertEqual(len(self.regen_calls()),3)
        self.assertEqual(len({row[3]for row in self.regen_calls()}),1)
        trace=self.device.trace;first_cache=next(i for i,row in enumerate(trace)if row[0]=='cache')
        self.assertEqual(len([row for row in trace[:first_cache]if row[0]=='regenerate']),3)
    def test_cache_reply_loss_retries_cache_without_new_rotation_draw_or_key(self):
        self.device.cache_results['com.google.android.gms']=[OSError('response lost')]
        with patch.object(m.random,'randint',return_value=5)as draw,patch.object(m.uuid,'uuid4')as uuid:
            uuid.return_value.hex='a'*32;self.cycle.regenerate_after_success()
        draw.assert_called_once_with(1,10);uuid.assert_called_once_with()
        self.assertEqual(len(self.regen_calls()),2)
        self.assertEqual(len({row[3]for row in self.regen_calls()}),1)
        self.assertEqual([row[2]for row in self.cache_calls()],['com.google.android.gms','com.google.android.gms','com.google.android.gsf'])
        self.assertEqual(self.regen_calls()[0][3],'a'*32)
        self.assertEqual(len([r for r in self.device.trace if r[0]=='native_rotation']),1)
        self.assertTrue(any(row[0]=='log' and 'regeneration_post_cache_pending'in row[2]for row in self.device.trace))
    def test_later_package_failure_does_not_reclear_successful_earlier_package(self):
        self.device.cache_results['com.google.android.gsf']=[RuntimeError('pending'),OSError('reply lost')]
        self.regenerate()
        self.assertEqual([row[2]for row in self.cache_calls()],['com.google.android.gms','com.google.android.gsf','com.google.android.gsf','com.google.android.gsf'])
        self.assertEqual([row[1]for row in self.cache_calls()],[0,0,15,30])
        self.assertEqual(len(self.regen_calls()),3)
        self.assertEqual(len({row[3]for row in self.regen_calls()}),1)
    def test_cache_wait_sends_no_ui_adb_fps_app_or_data_actions(self):
        self.device.cache_results['com.google.android.gms']=[RuntimeError('pending'),OSError('pending')]
        self.regenerate();trace=self.device.trace
        first=next(i for i,row in enumerate(trace)if row[0]=='cache')
        last=max(i for i,row in enumerate(trace)if row[0]=='cache')
        self.assertFalse(any(row[0]in{'adb','inspect','tap','open','close','clear','fps'}for row in trace[first:last+1]))
        self.assertEqual(len({row[3]for row in self.regen_calls()}),1)
    def test_unverified_or_data_clearing_reply_is_never_accepted(self):
        for bad in (None,{'ok':True},{'ok':True,'package':'wrong','cache_only':True,'data_cleared':False,'uid_verified':True},
                    {'ok':True,'package':'com.google.android.gms','cache_only':False,'data_cleared':False},
                    {'ok':True,'package':'com.google.android.gms','cache_only':True,'data_cleared':True}):
            with self.subTest(bad=bad):
                device=CacheDevice(self.clock);device.cache_results['com.google.android.gms']=[bad]
                with patch.object(m.random,'randint',return_value=5):m.Cycle(device,'qa.vpn').regenerate_after_success()
                self.assertEqual(len([r for r in device.trace if r[0]=='regenerate']),2)
                self.assertEqual(len({r[3]for r in device.trace if r[0]=='regenerate'}),1)
                self.assertEqual(len([r for r in device.trace if r[0]=='cache']),3)
    def test_input_and_foreground_readiness_required_before_cache(self):
        for field in ('input_ready','foreground_ready'):
            with self.subTest(field=field):
                device=CacheDevice(self.clock);device.regenerate_results=[committed(**{field:False}),committed()]
                with patch.object(m.random,'randint',return_value=5):m.Cycle(device,'qa.vpn').regenerate_after_success()
                calls=[r for r in device.trace if r[0]=='regenerate'];self.assertEqual(len(calls),2);self.assertEqual(calls[0][3],calls[1][3])
                self.assertEqual(len([r for r in device.trace if r[0]=='cache']),2)
    def test_admin_can_opt_in_play_store_and_set_user_timeout_pause(self):
        self.device.cache_results['com.google.android.gsf']=[RuntimeError('retry')]
        with patch.object(m,'GOOGLE_CACHE_PACKAGES',('com.google.android.gms','com.google.android.gsf','com.android.vending')),patch.object(m,'GOOGLE_CACHE_USER',10),patch.object(m,'GOOGLE_CACHE_TIMEOUT',42),patch.object(m,'GOOGLE_CACHE_RETRY_SECONDS',.5):
            self.regenerate()
        self.assertEqual([r[2]for r in self.cache_calls()],['com.google.android.gms','com.google.android.gsf','com.google.android.gsf','com.android.vending'])
        self.assertTrue(all(r[3:]==(10,42)for r in self.cache_calls()));self.assertEqual(self.clock.now,.5)
    def test_duplicate_package_configuration_clears_each_once(self):
        with patch.object(m,'GOOGLE_CACHE_PACKAGES',('com.google.android.gms','com.google.android.gms')):self.regenerate()
        self.assertEqual(len(self.cache_calls()),1)
    def test_invalid_cache_configuration_rejected_before_draw(self):
        for field,value in (('GOOGLE_CACHE_PACKAGES','com.google.android.gms'),('GOOGLE_CACHE_PACKAGES',('bad;command',)),('GOOGLE_CACHE_USER',True),('GOOGLE_CACHE_USER',-1),('GOOGLE_CACHE_TIMEOUT',-1),('GOOGLE_CACHE_TIMEOUT',True),('GOOGLE_CACHE_RETRY_SECONDS',float('nan')),('GOOGLE_CACHE_RETRY_SECONDS',-1)):
            with self.subTest(field=field,value=value),patch.object(m,field,value),patch.object(m.random,'randint')as draw:
                with self.assertRaises(ValueError):self.cycle.regenerate_after_success()
                draw.assert_not_called()
        self.assertEqual(self.cache_calls(),[]);self.assertEqual(self.regen_calls(),[])
    def test_failed_qa_and_cleanup_never_clear_google_cache(self):
        self.cycle.dismiss_ads=lambda:(_ for _ in()).throw(RuntimeError('QA failed'))
        with patch.object(m.random,'randint',return_value=97):
            with self.assertRaises(RuntimeError):self.cycle.run()
        self.assertEqual(self.cache_calls(),[]);self.assertEqual(self.regen_calls(),[])
    def test_interrupted_cache_phase_never_emits_qa_success(self):
        self.device.cache_results['com.google.android.gms']=[RuntimeError('still pending')]
        original_clear=self.device.clear_cache
        def interrupted(*args,**kwargs):
            original_clear(*args,**kwargs)
            raise KeyboardInterrupt('administrator stopped pending cache cleanup')
        self.device.clear_cache=interrupted
        with patch.object(m.random,'randint',side_effect=lambda low,high:97 if (low,high)==(90,120) else 5):
            with self.assertRaises(KeyboardInterrupt):self.cycle.run()
        self.assertFalse(any(r[0]=='log' and r[2]=='QA cycle completed successfully'for r in self.device.trace))
        self.assertEqual(len([r for r in self.device.trace if r[0]=='native_rotation']),1)

    def test_first_sdk_call_carries_the_complete_script_provided_durable_plan(self):
        self.regenerate()
        self.assertEqual(self.regen_calls()[0][4],{'packages':['com.google.android.gms','com.google.android.gsf'],'user':'current','timeout':0})
        self.assertNotIn('.clear_cache(',SOURCE.read_text())
    def test_missing_or_wrong_aggregate_cache_proof_replays_same_receipt(self):
        for bad in (None,{'ok':True},{'ok':True,'cache_only':True,'data_cleared':False,'packages':[],'user':'current'},
                    {'ok':True,'cache_only':True,'data_cleared':True,'packages':['com.google.android.gms','com.google.android.gsf'],'user':'current'}):
            with self.subTest(bad=bad):
                device=CacheDevice(self.clock);device.cleanup_overrides=[bad]
                with patch.object(m.random,'randint',return_value=5):m.Cycle(device,'qa.vpn').regenerate_after_success()
                calls=[r for r in device.trace if r[0]=='regenerate']
                self.assertEqual(len(calls),2);self.assertEqual(calls[0][3:],calls[1][3:])
                self.assertEqual(len([r for r in device.trace if r[0]=='native_rotation']),1)
                self.assertEqual(len([r for r in device.trace if r[0]=='cache']),2)

    def test_bundled_production_source_matches_reviewed_unseeded_workflow(self):
        self.assertEqual(hashlib.sha256(SOURCE.read_bytes()).hexdigest(),'8c9c2007b1947b06fa3ac5efa7621585c63bdc3939c2bbb1d4e515b7c2f666d9')
        self.assertNotIn('random.seed(',SOURCE.read_text())


if __name__=='__main__':unittest.main()
