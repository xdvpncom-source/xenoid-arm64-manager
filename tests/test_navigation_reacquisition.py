"""Bounded same-reader navigation reacquisition and typed failure evidence.

Actual Broker/reader methods with the public UI epoch/native fixtures and real
local framed child processes; no Android, SSH, ADB or native input endpoint.
"""
import contextlib,json,sys,threading,unittest
from unittest.mock import Mock,patch
import test_ui_epoch_recovery as e
h=e.h;u=e.u

class NavigationReacquisition(e.EpochNavigation):
 def setUp(self):
  super().setUp()
  immediate_owner=self.broker.owned_manager
  @contextlib.contextmanager
  def owned(*,deadline=...,allow_stopping=False):
   # The shared native fixture models an immediate owner; real lock deadlines
   # and cancellation are separately exercised by the native-flock tests.
   self.assertFalse(allow_stopping)
   with immediate_owner()as manager:yield manager
  self.broker.owned_manager=owned

 def fail_then_fresh(self,code):
  calls=[]
  def navigation(*args):
   calls.append(args)
   if len(calls)==1:raise u.DumpFailed(code)
   return self.fresh_navigation(*args)
  self.reader.navigation.side_effect=navigation;return calls

 def test_only_four_unavailable_codes_can_reacquire_same_reader_and_send_once(self):
   for code in('nav_display_unavailable','nav_bar_unavailable','nav_root_unavailable','nav_back_unavailable'):
    with self.subTest(code=code):
     self.injected.clear();calls=self.fail_then_fresh(code);reader=self.reader;window=self.snapshot();answer=self.action(window)
     self.assertTrue(answer['handled']);self.assertEqual(self.injected,[(10,95)]);self.assertEqual(len(calls),2);self.assertIs(self.broker._ui_reader,reader);self.assertEqual(answer['verification_stages']['fresh_node_attempts'],2);self.assertEqual(answer['verification_stages']['fresh_node_error_code'],code);self.assertFalse(self.action(window)['handled']);self.assertEqual(len(self.injected),1)
 def test_permanently_hidden_or_absent_bar_stays_rejected_after_two_reads(self):
   for code in('nav_bar_unavailable','nav_back_unavailable'):
    with self.subTest(code=code):
     self.reader.navigation.reset_mock();self.reader.navigation.side_effect=u.DumpFailed(code);window=self.snapshot();answer=self.action(window);self.assertFalse(answer['handled']);self.assertFalse(answer['input_sent']);self.assertEqual(answer['reason'],'navigation_fresh_back_unavailable');self.assertEqual(self.reader.navigation.call_count,2);self.assertEqual(self.injected,[])
 def test_changed_ambiguous_cleanup_or_untyped_failure_is_never_retried(self):
   for code in('nav_back_changed','nav_window_ambiguous','nav_back_ambiguous','nav_cleanup_failed','nav_node_obsolete','nav_back_outside_window','nav_metadata_exception','private https://token'):
    with self.subTest(code=code):
     self.reader.navigation.reset_mock();self.reader.navigation.side_effect=u.DumpFailed(code);window=self.snapshot();answer=self.action(window);self.assertFalse(answer['input_sent']);self.assertEqual(self.reader.navigation.call_count,1);self.assertEqual(self.injected,[])
 def test_first_failure_that_uses_entire_offer_lease_cannot_start_second_read(self):
   def expired(*args):self.clock.now+=h.NATIVE_NAV_MAX_AGE_SECONDS+.01;raise u.DumpFailed('nav_root_unavailable')
   self.reader.navigation.side_effect=expired;window=self.snapshot();answer=self.action(window);self.assertEqual(answer['reason'],'navigation_observation_stale');self.assertEqual(self.reader.navigation.call_count,1);self.assertEqual(self.injected,[])
 def test_absolute_command_deadline_can_expire_before_offer_lease(self):
   self.broker.config['command_timeout']=1
   def expired(*args):self.clock.now+=1.01;raise u.DumpFailed('nav_root_unavailable')
   self.reader.navigation.side_effect=expired;window=self.snapshot();answer=self.action(window);self.assertEqual(answer['reason'],'navigation_operation_expired');self.assertEqual(self.reader.navigation.call_count,1);self.assertEqual(self.injected,[])
 def test_reader_replaced_between_attempts_is_not_started_or_used(self):
   def changed(*args):self.broker._ui_reader=Mock();raise u.DumpFailed('nav_root_unavailable')
   self.reader.navigation.side_effect=changed;window=self.snapshot();answer=self.action(window);self.assertEqual(answer['reason'],'navigation_reader_unavailable');self.assertEqual(self.reader.navigation.call_count,1);self.assertEqual(self.injected,[])
 def test_reacquired_proof_still_must_match_node_namespace_geometry_and_route(self):
   for mutate in('node_link','bounds','namespace','routing'):
    with self.subTest(mutate=mutate):
     calls=[]
     def changed(*args):
      calls.append(args)
      if len(calls)==1:raise u.DumpFailed('nav_root_unavailable')
      proof=self.fresh_navigation(*args)
      if mutate=='node_link':proof['node_window_id']+=1
      if mutate=='bounds':proof['bounds']=[1,90,21,100]
      if mutate=='namespace':proof['window_id_scope']='old_connection'
      if mutate=='routing':self.nav_visible=False
      return proof
     self.nav_visible=True;self.reader.navigation.side_effect=changed;answer=self.action(self.snapshot());self.assertFalse(answer['input_sent']);self.assertEqual(len(calls),2);self.assertEqual(self.injected,[])
 def test_second_request_rechecks_real_reader_physical_fence_before_pipe(self):
   reader=self.actual_reader(epoch='old');self.broker._ui_reader=reader;self.reader=reader
   first=dict(reader.binding);reads=[]
   def binding(**kwargs):
    reads.append(kwargs);return first if len(reads)==1 else{**first,'pid_start':9}
   reader._binding=Mock(side_effect=binding);reader.pipe.navigation=Mock(side_effect=u.DumpFailed('nav_root_unavailable'))
   with self.assertRaises(h.TransportUnavailable):self.action(self.snapshot())
   self.assertEqual(reader.pipe.navigation.call_count,1);self.assertEqual(reader._binding.call_count,3);self.assertEqual(self.injected,[]);self.assertTrue(reader.closed);self.assertIsNotNone(reader.private_identity)
   # Restore only the local test double so its temporary directory/child can be
   # reaped by fixture teardown; production correctly refused foreign cleanup.
   reader._binding=Mock(return_value=first)
 def test_one_real_ordered_pipe_handles_first_error_then_second_proof(self):
   with self.broker.owned_manager():proof=self.fresh_navigation(self.manager,0,None)
   child=e.pf.CHILD.replace("if mode=='naverror':reply(seq,1,b'nav_back_unavailable');continue", "if mode=='naverror' and seq==1:reply(seq,1,b'nav_root_unavailable');continue")
   old="reply(seq,0,b'{\"schema\":\"xenoid.nav-node-proof/v1\",\"display_id\":'+str(display).encode()+b'}');continue";self.assertIn(old,child);child=child.replace(old,'reply(seq,0,'+repr(json.dumps(proof).encode())+');continue')
   reader=self.actual_reader(epoch='old');reader.pipe.close(force=True);reader.pipe=u.PipeProcess([sys.executable,'-u','-c',child,'naverror'],None,threading.Event(),reader._terminate);pid=reader.pipe.process.pid;self.broker._ui_reader=reader
   with patch.object(u,'time',self.clock):answer=self.action(self.snapshot())
   self.assertTrue(answer['handled']);self.assertEqual(self.injected,[(10,95)]);self.assertEqual(reader.pipe.sequence,2);self.assertEqual(reader.pipe.process.pid,pid);self.assertIsNone(reader.pipe.process.poll());reader.close(force=True)
 def test_each_typed_java_failure_is_retained_without_input_or_offer_replay(self):
   for code in('nav_display_unavailable','nav_bar_unavailable','nav_root_unavailable','nav_node_obsolete','nav_back_unavailable','nav_back_changed'):
    with self.subTest(code=code):
     window=self.snapshot();self.reader.navigation.side_effect=u.DumpFailed(code);answer=self.action(window)
     self.assertFalse(answer['handled']);self.assertFalse(answer['input_sent']);self.assertEqual(answer['reason'],'navigation_fresh_back_unavailable');self.assertEqual(answer['verification_stages']['fresh_node_error_code'],code);self.assertEqual(self.injected,[]);self.assertFalse(self.action(window)['handled'])
 def test_untyped_or_sensitive_exception_text_is_not_emitted(self):
   for code in('https://endpoint.invalid/?token=PRIVATE','nav_back_unavailable\nPRIVATE','nav_'+('x'*100),'stock_dump_no_xml'):
    with self.subTest(code=code):
     window=self.snapshot();self.reader.navigation.side_effect=u.DumpFailed(code);answer=self.action(window);self.assertEqual(answer['verification_stages']['fresh_node_error_code'],'unrecognized_navigation_dump_failure');self.assertNotIn('PRIVATE',json.dumps(answer));self.assertEqual(self.injected,[])
 def test_real_transport_error_is_still_fatal(self):
   window=self.snapshot();self.reader.navigation.side_effect=u.UiReaderError('ui_reader_runtime_changed')
   with self.assertRaises(h.TransportUnavailable):self.action(window)
   self.assertEqual(self.injected,[])

if __name__=='__main__':unittest.main()
