"""Only the official PackageManager cache API may mutate; no data-delete fallback."""
import contextlib
import subprocess
import sys
import time
from pathlib import Path
import unittest
from unittest.mock import MagicMock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'qa'))
import script_host as h
import script_runtime as sdk
PACKAGE='com.google.android.gms'
HELP='  clear [--user USER_ID] [--cache-only] PACKAGE\n    --cache-only: only clear cache data\n'

class CacheOnlyTests(unittest.TestCase):
 def setUp(self):
  self.m=MagicMock();self.m.instance_operation_lock.side_effect=lambda **kw:contextlib.nullcontext();self.m._owned_container_record.return_value=({'Id':'a'*64},None)
  self.b=h.Broker(self.m,lambda *_args:None,config={'expected_container_id':'a'*64,'command_timeout':0})
  self.b.owned_manager=lambda *,deadline=...,allow_stopping=False:contextlib.nullcontext(self.m)
  self.calls=[];self.uid_values=['10024','10024'];self.help=HELP;self.clear_code=0;self.clear_output=b'Success\n';self.help_code=255
  def execute(manager,args,**options):
   self.calls.append((list(args),options));out=b'';code=0
   if args==['am','get-current-user']:out=b'0\n'
   elif args[:5]==['cmd','package','list','packages','-U']:out=('package:'+args[-1]+' uid:'+self.uid_values.pop(0)+'\n').encode()
   elif args==['cmd','package','help']:out=self.help.encode();code=self.help_code
   elif args[:3]==['cmd','package','clear']:out=self.clear_output;code=self.clear_code
   else:raise AssertionError(args)
   return subprocess.CompletedProcess(args,code,out,b'')
  self.b.native_display_exec=MagicMock(side_effect=execute)
  self.b.adb=MagicMock(side_effect=AssertionError('no adb/delete fallback'))
 def mutators(self):return [args for args,_ in self.calls if args[:3]==['cmd','package','clear']]
 def test_sdk_explicit_keyword_serialization(self):
  with patch.object(sdk,'request',side_effect=lambda action,args:(action,args)):
   self.assertEqual(sdk.Device().clear_cache(PACKAGE),('clear-cache',[PACKAGE,'current',None]))
   self.assertEqual(sdk.Device().clear_cache(PACKAGE,user=10,timeout=0),('clear-cache',[PACKAGE,10,0]))
 def test_real_android13_help_exit255_is_accepted_but_clear_requires_success(self):
  result=self.b.dispatch('clear-cache',[PACKAGE,'current',None])
  self.assertEqual(result,{'ok':True,'package':PACKAGE,'user':0,'uid_verified':True,'cache_only':True,'data_cleared':False})
  self.assertEqual(self.mutators(),[['cmd','package','clear','--cache-only','--user','0',PACKAGE]])
  self.b.adb.assert_not_called()
 def test_nonzero_user_is_supported_without_user0_policy(self):
  result=self.b.clear_cache(PACKAGE,user=10);self.assertEqual(result['user'],10)
  self.assertNotIn(['am','get-current-user'],[args for args,_ in self.calls]);self.assertIn('--cache-only',self.mutators()[0])
 def test_optional_user_none_respects_panel_android_user(self):
  self.b.config['android_user']=10
  self.assertEqual(self.b.clear_cache(PACKAGE,user=None)['user'],10)
 def test_any_installed_package_allowed_not_only_google(self):
  result=self.b.clear_cache('org.example.qa');self.assertEqual(result['package'],'org.example.qa')
 def test_invalid_package_user_timeout_never_executes(self):
  for package,user,timeout in [('bad;rm -rf /','current',None),(PACKAGE,-1,None),(PACKAGE,True,None),(PACKAGE,'0',None),(PACKAGE,'current',-1)]:
   with self.subTest(package=package,user=user,timeout=timeout),self.assertRaises(ValueError):self.b.clear_cache(package,user=user,timeout=timeout)
  self.b.native_display_exec.assert_not_called()
 def test_zero_timeout_unlimited_and_large_admin_budget_not_capped(self):
  self.b.clear_cache(PACKAGE,timeout=0)
  self.assertTrue(all(options['deadline'] is None for _,options in self.calls))
  self.uid_values=['10024','10024'];self.calls=[];start=time.monotonic();self.b.clear_cache(PACKAGE,timeout=100000)
  self.assertTrue(all(options['deadline']>=start+99999 for _,options in self.calls))
 def test_parent_regeneration_deadline_clips_explicit_unlimited_cache_timeout(self):
  end=time.monotonic()+30
  self.b.clear_cache(PACKAGE,timeout=0,_deadline=end)
  self.assertTrue(all(options['deadline']==end for _,options in self.calls))
  self.calls=[]
  with self.assertRaises(h.TransportUnavailable):self.b.clear_cache(PACKAGE,timeout=0,_deadline=time.monotonic()-1)
  self.assertEqual(self.calls,[])
 def test_unsupported_flag_never_falls_through_to_full_data_clear(self):
  self.help='  clear [--user USER_ID] PACKAGE\n'
  with self.assertRaises(h.TransportUnavailable):self.b.clear_cache(PACKAGE)
  self.assertEqual(self.mutators(),[])
 def test_missing_or_duplicate_uid_never_clears(self):
  for text in ('','package:'+PACKAGE+' uid:10024\npackage:'+PACKAGE+' uid:10025\n'):
   original=self.b.native_display_exec.side_effect
   def execute(manager,args,**options):
    if args[:5]==['cmd','package','list','packages','-U']:return subprocess.CompletedProcess(args,0,text.encode(),b'')
    return original(manager,args,**options)
   self.b.native_display_exec.side_effect=execute
   with self.assertRaises(h.TransportUnavailable):self.b.clear_cache(PACKAGE)
   self.assertEqual(self.mutators(),[]);self.b.native_display_exec.side_effect=original
 def test_clear_failure_has_no_alternate_delete_command(self):
  for code,stdout in ((1,b'Failed\n'),(0,b'Failed\n'),(0,b'Success\nSuccess\n')):
   self.calls=[];self.uid_values=['10024'];self.clear_code=code;self.clear_output=stdout
   with self.assertRaises(h.TransportUnavailable):self.b.clear_cache(PACKAGE)
   self.assertEqual(len(self.mutators()),1);self.assertIn('--cache-only',self.mutators()[0])
   self.assertFalse(any(args[:1] in (['rm'],['pm']) for args,_ in self.calls))
 def test_uid_handoff_after_operation_is_not_reported_success(self):
  self.uid_values=['10024','10025']
  with self.assertRaises(h.RuntimeIdentityError):self.b.clear_cache(PACKAGE)
  self.assertEqual(len(self.mutators()),1)
 def test_container_handoff_is_not_adopted(self):
  self.m._owned_container_record.return_value=({'Id':'b'*64},None)
  with self.assertRaises(h.RuntimeIdentityError):self.b.clear_cache(PACKAGE)
 def test_current_user_query_failure_prevents_any_clear(self):
  self.b.native_display_exec.return_value=None
  self.b.native_display_exec.side_effect=lambda *a,**kw:subprocess.CompletedProcess([],1,b'unknown',b'')
  with self.assertRaises(h.TransportUnavailable):self.b.clear_cache(PACKAGE)
  self.assertEqual(self.mutators(),[])
 def test_cache_only_repetition_is_safe_and_does_not_regenerate(self):
  self.b.regenerate=MagicMock(side_effect=AssertionError('cache is not regeneration'));self.b.app_test_action=MagicMock(side_effect=AssertionError('no app reset/close'))
  self.b.clear_cache(PACKAGE);self.uid_values=['10024','10024'];self.b.clear_cache(PACKAGE)
  self.assertEqual(len(self.mutators()),2);self.assertTrue(all('--cache-only' in args for args in self.mutators()));self.b.regenerate.assert_not_called();self.b.app_test_action.assert_not_called()

if __name__=='__main__':unittest.main()
