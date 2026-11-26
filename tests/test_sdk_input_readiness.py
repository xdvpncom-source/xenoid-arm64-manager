"""SDK integration: strict mapping, same-inode repair, no injected repair events."""
import contextlib
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import types
import unittest
from unittest.mock import MagicMock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'qa'))
import script_host as h
import script_runtime as sdk
import qa_input_ready as q
PROFILE={'input':{'name':'sec_touchscreen','x':{'minimum':0,'maximum':1439},'y':{'minimum':0,'maximum':3119}}}
DUMP=Path(__file__).with_name('test_native_input_fixture.txt').read_text()
RAW=json.dumps(PROFILE).encode()
SHA=hashlib.sha256(RAW).hexdigest()
CONFIG={'expected_container_id':'a'*64,'command_timeout':180,'instance_id':'11111111-1111-1111-1111-111111111111','resource_tag':'123456789abc'}


def binding(profile_sha=SHA):
 return q.InputBinding(20,8142202,'/dev/input/event3','/dev/input/.xenoid-reannounce-20-8142202-event3',3,4,4831484,'60:604:21b0:d:43:0:1004:660',profile_sha,'b'*64,'c'*64,'d'*64)

def state(ready=True,focus=True):
 return {'ready':ready,'profile':PROFILE,'profile_sha256':SHA,'dump':DUMP,'mapping':{'logical_size':[200,356]} if ready else None,'foreground':{'ready':focus,'package':'org.example' if focus else '', 'component':'org.example/.Main' if focus else ''}}


class ReadinessTests(unittest.TestCase):
 def setUp(self):
  global q
  q=sys.modules['qa_input_ready']
  self.manager=MagicMock();self.manager._owned_container_record.return_value=({'Id':'a'*64},None)
  self.b=h.Broker(self.manager,lambda *_args:None,config=CONFIG.copy())
  self.b.adb=MagicMock(side_effect=AssertionError('no framework input fallback'))
  self.b.stopping.wait=MagicMock(return_value=False)
  self.manager.daemon_client.return_value.tap.side_effect=AssertionError('repair must not inject a tap')
 def root_fixture(self,proof=None):
  proof=proof or binding()
  reply={'ok':True,'schema':'xenoid.native-input-reannouncement/v1','reannounced':True,'same_inode':True,'driver_recreated':False,'events_injected':False,'binding_digest':proof.digest}
  self.b.native_input_rootd=MagicMock(side_effect=['probe-evidence',json.dumps(reply)])
  return patch.object(q,'parse_probe',return_value=proof)
 def test_ready_fastpath_calls_no_rootd_no_adb_no_pointer(self):
  self.b.native_interaction_snapshot=MagicMock(return_value=state())
  self.b.native_input_rootd=MagicMock(side_effect=AssertionError('ready path may not repair'))
  result=self.b.ensure_native_interaction(self.manager,None,repair=True)
  self.assertTrue(result['ready']);self.b.native_input_rootd.assert_not_called();self.b.adb.assert_not_called();self.manager.daemon_client.assert_not_called()
 def test_public_query_reports_mapping_and_focus_without_repair(self):
  self.manager.instance_operation_lock.return_value=contextlib.nullcontext()
  self.b.owned_manager=lambda *,deadline=...,allow_stopping=False:contextlib.nullcontext(self.manager)
  self.b.native_interaction_snapshot=MagicMock(return_value=state(False,True))
  self.b.native_input_rootd=MagicMock(side_effect=AssertionError('readonly'))
  result=self.b.dispatch('input-status',[0]);self.assertFalse(result['ready']);self.assertTrue(result['foreground']['ready']);self.assertFalse(result['events_injected']);self.b.native_input_rootd.assert_not_called()
 def test_sdk_query_serializes_optional_admin_timeout(self):
  with patch.object(sdk,'request',side_effect=lambda action,args:(action,args)):
   self.assertEqual(sdk.Device().input_status(),('input-status',[]));self.assertEqual(sdk.Device().input_status(0),('input-status',[0]))
 def test_bash_query_converts_timeout_and_remains_readonly(self):
  with patch.object(sdk,'request',side_effect=lambda action,args:(action,args)):
   self.assertEqual(sdk.cli('device',['input-status','0']),('input-status',[0]))
   self.assertEqual(sdk.cli('device',['input-status']),('input-status',[]))
 def test_repair_waits_for_real_mapping_never_counts_helper_success_as_ready(self):
  self.b.native_interaction_snapshot=MagicMock(side_effect=[state(False),state(False),state(True)])
  with self.root_fixture():result=self.b.ensure_native_interaction(self.manager,time.monotonic()+10,repair=True)
  self.assertTrue(result['ready']);self.assertEqual(self.b.native_interaction_snapshot.call_count,3);self.assertEqual(self.b.native_input_rootd.call_count,2)
  commands=[call.args[1] for call in self.b.native_input_rootd.call_args_list]
  self.assertTrue(all(len(command.encode())<=4096 for command in commands));self.assertTrue(all('input tap' not in command and ' reload' not in command for command in commands));self.manager.daemon_client.assert_not_called();self.b.adb.assert_not_called()
 def test_profile_changed_between_snapshot_and_probe_never_repairs(self):
  self.b.native_interaction_snapshot=MagicMock(return_value=state(False))
  with self.root_fixture(binding('f'*64)),self.assertRaises(q.NativeInputBindingError):self.b.ensure_native_interaction(self.manager,None,repair=True)
  self.assertEqual(self.b.native_input_rootd.call_count,1)
 def test_invalid_sysfs_service_or_socket_evidence_never_repairs(self):
  self.b.native_interaction_snapshot=MagicMock(return_value=state(False));self.b.native_input_rootd=MagicMock(return_value='bad-evidence')
  with self.assertRaises(q.NativeInputBindingError):self.b.ensure_native_interaction(self.manager,None,repair=True)
  self.assertEqual(self.b.native_input_rootd.call_count,1);self.manager.daemon_client.assert_not_called()
 def test_expired_admin_deadline_performs_no_commands(self):
  self.b.native_interaction_snapshot=MagicMock()
  with self.assertRaises(h.TransportUnavailable):self.b.ensure_native_interaction(self.manager,time.monotonic()-1,repair=True)
  self.b.native_interaction_snapshot.assert_not_called()
 def test_foreground_wait_never_opens_app_or_repairs_ready_input(self):
  self.b.native_interaction_snapshot=MagicMock(side_effect=[state(True,False),state(True,True)]);self.b.native_input_rootd=MagicMock(side_effect=AssertionError('input ready'))
  result=self.b.ensure_native_interaction(self.manager,None,repair=True,foreground=True)
  self.assertTrue(result['foreground']['ready']);self.b.native_input_rootd.assert_not_called();self.b.adb.assert_not_called()
 def test_snapshot_uses_actual_profile_reader_and_any_foreground_window(self):
  raw=RAW+b'\nXENOID_INPUT_MAPPING\n'+DUMP.encode()+b'\nXENOID_FOREGROUND_WINDOW\n  mCurrentFocus=Window{abcd u0 com.android.vpndialogs/.ConfirmDialog}\n'
  self.b.native_display_exec=MagicMock(return_value=subprocess.CompletedProcess([],0,raw,b''))
  result=self.b.native_interaction_snapshot(self.manager,None,foreground=True)
  self.assertTrue(result['ready']);self.assertTrue(result['foreground']['ready']);self.assertEqual(result['foreground']['package'],'com.android.vpndialogs');self.assertEqual(result['profile_sha256'],SHA)
 def test_snapshot_missing_or_duplicate_direct_device_is_not_ready(self):
  duplicate=DUMP.replace('  Device -1:',DUMP[DUMP.index('  Device 2:'):DUMP.index('  Device -1:')]+'  Device -1:',1)
  for dump in ('Input Reader State:\nInput Dispatcher State:\n',duplicate):
   self.b.native_display_exec=MagicMock(return_value=subprocess.CompletedProcess([],0,RAW+b'\nXENOID_INPUT_MAPPING\n'+dump.encode(),b''))
   result=self.b.native_interaction_snapshot(self.manager,None)
   self.assertFalse(result['ready'])
   self.assertTrue(result['mapping_error'].startswith('Native touch mapping unavailable: '))
 def test_native_rootd_rejects_cid_handoff_before_command(self):
  self.manager._owned_container_record.return_value=({'Id':'b'*64},None)
  with self.assertRaises(h.RuntimeIdentityError):self.b.native_input_rootd(self.manager,'read-only-command',None)
  self.manager.daemon_client.assert_not_called();self.manager._rootd_exec.assert_not_called()
 def test_native_rootd_rechecks_cid_after_reply(self):
  self.manager._owned_container_record.side_effect=[({'Id':'a'*64},None),({'Id':'b'*64},None)]
  self.manager.daemon_client.return_value.read_private_token.return_value='private-token-never-log'
  self.manager._rootd_exec.return_value={'ok':True,'exitCode':0,'stdout':'evidence'}
  with self.assertRaises(h.RuntimeIdentityError):self.b.native_input_rootd(self.manager,'read-only-command',None)
 def test_native_rootd_failure_is_not_successful_readiness(self):
  self.manager.daemon_client.return_value.read_private_token.return_value='private-token-never-log';self.manager._rootd_exec.return_value={'ok':True,'exitCode':2,'stdout':'not accepted'}
  with self.assertRaises(h.TransportUnavailable):self.b.native_input_rootd(self.manager,'read-only-command',None)
 def test_stop_prevents_repair_or_wait(self):
  self.b.stopping.set();self.b.native_interaction_snapshot=MagicMock()
  with self.assertRaises(h.TransportUnavailable):self.b.ensure_native_interaction(self.manager,None,repair=True)
  self.b.native_interaction_snapshot.assert_not_called()


if __name__=='__main__':unittest.main()
