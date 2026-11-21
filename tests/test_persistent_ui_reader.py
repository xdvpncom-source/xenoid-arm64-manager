"""Framed reader lifecycle and Broker integration, without a device dependency."""
import contextlib
import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch

BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE/'qa'))
import qa_ui_reader as u
import script_host as h
import script_runtime as runtime

CHILD=r'''
import os,struct,sys,time
MAGIC=0x58554931
inp=sys.stdin.buffer;out=sys.stdout.buffer
mode=sys.argv[1]
def reply(seq,status,data):
 payload=struct.pack('>IIBI',MAGIC,seq,status,len(data))+data
 if mode=='split':
  out.write(payload[:7]);out.flush();time.sleep(.01);out.write(payload[7:]);out.flush()
 else:out.write(payload);out.flush()
if mode=='startupslow':time.sleep(20)
reply(0,0,b'READY\t123\t2000\t456')
while True:
 data=inp.read(9)
 if not data:break
 magic,seq,flags=struct.unpack('>IIB',data)
 if flags==127:reply(seq,0,b'BYE');break
 if mode=='slow':time.sleep(20)
 if mode=='error':reply(seq,1,b'stock_dump_no_xml');continue
 if mode=='badmagic':out.write(struct.pack('>IIBI',0,seq,0,0));out.flush();continue
 if mode=='oversize':out.write(struct.pack('>IIBI',MAGIC,seq,0,9*1024*1024));out.flush();continue
 reply(seq,0,b'<displays></displays>' if flags&2 else b'<hierarchy></hierarchy>')
'''

class PipeTests(unittest.TestCase):
 def process(self,mode='ok',deadline=None):
  cleaned=[];p=u.PipeProcess([sys.executable,'-u','-c',CHILD,mode],deadline,threading.Event(),cleaned.append)
  self.addCleanup(p.close,force=True)
  return p,cleaned
 def test_repeated_d_and_w_requests_reuse_one_process_with_unlimited_deadline(self):
  p,_=self.process();pid=p.process.pid
  for i in range(5):self.assertEqual(p.request(bool(i%2),None),b'<displays></displays>'if i%2 else b'<hierarchy></hierarchy>')
  self.assertEqual(p.process.pid,pid);self.assertIsNone(p.process.poll())
 def test_split_frames_and_diagnostics_do_not_require_one_os_read(self):
  p,_=self.process('split');self.assertEqual(p.request(False,time.monotonic()+1),b'<hierarchy></hierarchy>')
 def test_stock_error_never_returns_previous_xml_and_keeps_framing(self):
  p,cleaned=self.process('error')
  for _ in range(2):
   with self.assertRaisesRegex(u.DumpFailed,'stock_dump_no_xml'):p.request(False,None)
  self.assertFalse(p.closed);self.assertFalse(cleaned)
 def test_bad_frame_and_oversize_force_cleanup(self):
  for mode in ('badmagic','oversize'):
   with self.subTest(mode=mode):
    p,cleaned=self.process(mode)
    with self.assertRaisesRegex(u.UiReaderError,'invalid_frame'):p.request(False,None)
    self.assertTrue(p.closed);self.assertEqual(len(cleaned),1);self.assertIsNotNone(p.process.poll())
 def test_timeout_and_stop_force_cleanup_without_waiting_for_dump(self):
  p,cleaned=self.process('slow')
  with self.assertRaises(TimeoutError):p.request(False,time.monotonic()+.02)
  self.assertTrue(p.closed);self.assertEqual(len(cleaned),1)
  p,cleaned=self.process();p.stopping.set()
  with self.assertRaisesRegex(u.UiReaderError,'stopping'):p.request(False,None)
  self.assertTrue(p.closed);self.assertEqual(len(cleaned),1)
 def test_concurrent_requests_are_serialized(self):
  p,_=self.process('split');answers=[]
  threads=[threading.Thread(target=lambda:answers.append(p.request(False,None)))for _ in range(4)]
  for t in threads:t.start()
  for t in threads:t.join(2)
  self.assertEqual(answers,[b'<hierarchy></hierarchy>']*4)
 def test_cleanup_error_still_terminates_launcher(self):
  p,_=self.process('slow');p.cleanup=Mock(side_effect=RuntimeError('identity cannot be verified'))
  with self.assertRaisesRegex(u.UiReaderError,'cleanup_unconfirmed'):p.close(force=True)
  self.assertIsNotNone(p.process.poll());p.cleanup=Mock()
 def test_cleanup_failure_is_retryable_after_launcher_already_exited(self):
  p,_=self.process();p.cleanup=Mock(side_effect=[RuntimeError('temporary'),None])
  with self.assertRaisesRegex(u.UiReaderError,'cleanup_unconfirmed'):p.close(force=True)
  self.assertTrue(p.cleanup_pending);self.assertIsNotNone(p.process.poll())
  p.close(force=True);self.assertFalse(p.cleanup_pending);self.assertEqual(p.cleanup.call_count,2)
 def test_timeout_before_ready_reaps_launcher_and_requests_guest_cleanup(self):
  created=[];cleaned=[];real_popen=subprocess.Popen
  def launch(*args,**kwargs):
   proc=real_popen(*args,**kwargs);created.append(proc);return proc
  with patch.object(u.subprocess,'Popen',side_effect=launch):
   with self.assertRaises(TimeoutError):
    u.PipeProcess([sys.executable,'-u','-c',CHILD,'startupslow'],time.monotonic()+.03,threading.Event(),cleaned.append)
  self.assertEqual(cleaned,[None]);self.assertIsNotNone(created[0].poll())

class NativeFenceTests(unittest.TestCase):
 def reader(self):
  r=object.__new__(u.PersistentUiReader);r.pipe=Mock();r.closed=False;r.manager=object();r._verify=Mock();r._helper=Mock();r.close=Mock();return r
 def test_runtime_changed_after_read_discards_xml_and_closes(self):
  r=self.reader();r._verify.side_effect=[None,u.UiReaderError('ui_reader_runtime_changed')];r.pipe.request.return_value=b'<hierarchy/>'
  with self.assertRaisesRegex(u.UiReaderError,'runtime_changed'):r.read(object(),False,None)
  r.close.assert_called_once_with(force=True)
 def test_stock_failure_stays_visible_without_restarting_warm_process(self):
  r=self.reader();r.pipe.request.side_effect=u.DumpFailed('stock_dump_no_xml')
  with self.assertRaises(u.DumpFailed):r.read(object(),False,None)
  r.close.assert_not_called()
 def test_pid_reuse_does_not_send_signal(self):
  r=object.__new__(u.PersistentUiReader)
  expected={'pid':123,'starttime':10,'uid':1000,'pidns':'ns','kind':'compiler','cmdline':[b'dex2oat']}
  with patch.object(u,'proc_record',return_value={**expected,'starttime':11}),patch.object(u.os,'kill')as kill:
   self.assertIsNone(r._live_cleanup_target(expected))
  kill.assert_not_called()
 def test_zombie_with_empty_cmdline_is_already_terminated(self):
  r=object.__new__(u.PersistentUiReader)
  expected={'pid':123,'starttime':10,'uid':1000,'pidns':'ns','kind':'compiler','cmdline':[b'dex2oat']}
  with patch.object(u,'proc_record',return_value={**expected,'state':'Z','cmdline':[]}):
   self.assertIsNone(r._live_cleanup_target(expected))
 def test_private_symlink_cleanup_preserves_unowned_target(self):
  with tempfile.TemporaryDirectory()as temp:
   root=Path(temp);directory=root/'private';directory.mkdir(mode=0o700);outside=root/'outside';outside.write_text('preserve');(directory/'server.jar').symlink_to(outside)
   info=directory.stat();r=object.__new__(u.PersistentUiReader);r.pipe=None;r.directory=directory;r.private_identity=(info.st_dev,info.st_ino,info.st_uid);r._verify=Mock();r._terminate=Mock();r._helper=Mock(return_value=None)
   with self.assertRaisesRegex(u.UiReaderError,'cleanup_unconfirmed'):r.close()
   self.assertEqual(outside.read_text(),'preserve');self.assertTrue(directory.exists())
 def private_reader(self,directory):
  info=directory.stat();r=object.__new__(u.PersistentUiReader);r.pipe=None;r.directory=directory;r.private_identity=(info.st_dev,info.st_ino,info.st_uid);r._verify=Mock();r._terminate=Mock();r._helper=Mock(return_value=None);return r
 def test_cleanup_can_retry_after_a_transient_guest_sweep_failure(self):
  with tempfile.TemporaryDirectory()as temp:
   d=Path(temp)/'private';d.mkdir(mode=0o700);(d/'server.jar').write_bytes(b'jar')
   r=self.private_reader(d);r._terminate.side_effect=[RuntimeError('busy'),RuntimeError('busy'),None,None]
   with self.assertRaisesRegex(u.UiReaderError,'cleanup_unconfirmed'):r.close()
   self.assertTrue(d.exists());r.close();self.assertFalse(d.exists());self.assertIsNone(r.private_identity)
 def test_outer_cleanup_recovery_clears_pipe_pending_and_repeated_close_is_noop(self):
  with tempfile.TemporaryDirectory()as temp:
   d=Path(temp)/'private';d.mkdir(mode=0o700);(d/'server.jar').write_bytes(b'jar')
   r=self.private_reader(d);calls=[]
   def sweep(identity):
    calls.append(identity)
    if len(calls)==1:raise u.UiReaderError('temporary guest sweep failure')
   r._terminate=Mock(side_effect=sweep)
   p=u.PipeProcess([sys.executable,'-u','-c',CHILD,'ok'],None,threading.Event(),r._terminate)
   r.pipe=p
   try:
    r.close(force=True)
    self.assertEqual(len(calls),2);self.assertIsNotNone(p.process.poll())
    self.assertFalse(p.cleanup_pending);self.assertIsNone(r.private_identity);self.assertFalse(d.exists())
    r._verify.assert_called_once_with(cleanup=True)
    r.close(force=True)
    self.assertEqual(len(calls),2);r._verify.assert_called_once_with(cleanup=True)
   finally:p.cleanup=Mock();p.close(force=True)
 def test_failed_outer_cleanup_keeps_pipe_pending_until_fenced_retirement(self):
  with tempfile.TemporaryDirectory()as temp:
   d=Path(temp)/'private';d.mkdir(mode=0o700);(d/'server.jar').write_bytes(b'jar')
   r=self.private_reader(d);recover=[False]
   def sweep(identity):
    if not recover[0]:raise u.UiReaderError('guest cleanup still unverified')
   r._terminate=Mock(side_effect=sweep)
   p=u.PipeProcess([sys.executable,'-u','-c',CHILD,'ok'],None,threading.Event(),r._terminate)
   r.pipe=p
   try:
    with self.assertRaisesRegex(u.UiReaderError,'cleanup_unconfirmed'):r.close(force=True)
    self.assertTrue(p.cleanup_pending);self.assertIsNotNone(r.private_identity);self.assertTrue(d.exists())
    recover[0]=True;r.close(force=True)
    self.assertFalse(p.cleanup_pending);self.assertIsNone(r.private_identity);self.assertFalse(d.exists())
   finally:p.cleanup=Mock();p.close(force=True)
 def test_failed_directory_retirement_does_not_clear_recovered_pipe_pending(self):
  with tempfile.TemporaryDirectory()as temp:
   d=Path(temp)/'private';d.mkdir(mode=0o700);(d/'unexpected').write_text('preserve')
   r=self.private_reader(d);calls=[]
   def sweep(identity):
    calls.append(identity)
    if len(calls)==1:raise u.UiReaderError('temporary guest sweep failure')
   r._terminate=Mock(side_effect=sweep)
   p=u.PipeProcess([sys.executable,'-u','-c',CHILD,'ok'],None,threading.Event(),r._terminate)
   r.pipe=p
   try:
    with self.assertRaisesRegex(u.UiReaderError,'cleanup_unconfirmed'):r.close(force=True)
    self.assertEqual(len(calls),2);self.assertTrue(p.cleanup_pending)
    self.assertIsNotNone(r.private_identity);self.assertEqual((d/'unexpected').read_text(),'preserve')
   finally:p.cleanup=Mock();p.close(force=True)
 def test_retiring_directory_prevents_delayed_pre_ready_exec(self):
  with tempfile.TemporaryDirectory()as temp:
   root=Path(temp);d=root/'private';d.mkdir(mode=0o700);(d/'server.jar').write_bytes(b'jar');launched=root/'unexpected-launch'
   # A queued shell that has not even written its PID record must fail before
   # launching Java once the parent cancels/retires the original directory.
   program='import subprocess,time;time.sleep(.15);raise SystemExit(subprocess.call(["sh","-c",'+repr('set -e; printf "pid" > '+str(d/'process.tmp')+'; touch '+str(launched))+']))'
   p=subprocess.Popen([sys.executable,'-c',program],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
   try:
    self.private_reader(d).close();self.assertNotEqual(p.wait(timeout=3),0);self.assertFalse(launched.exists())
   finally:
    if p.poll()is None:p.kill();p.wait()
 def test_epoch_uses_same_request_deadline_and_cleanup_skips_epoch_probe(self):
  r=object.__new__(u.PersistentUiReader);r.container_id='a'*64;r.deadline=110;r.stopping=threading.Event()
  r.manager=Mock();r.manager.context.instance_id='id';r.manager.context.resource_tag='tag';r.manager._owned_container_record.return_value=({'Id':'a'*64,'Image':'image','State':{'Running':True,'Pid':44}},None);r.manager._userspace_runtime_epoch.return_value='epoch'
  with patch.object(u,'proc_record',return_value={'starttime':8}),patch.object(u.time,'monotonic',return_value=100):
   self.assertEqual(r._binding()['epoch'],'epoch');r.manager._userspace_runtime_epoch.assert_called_once_with(deadline=102)
   r.manager._userspace_runtime_epoch.reset_mock();self.assertIsNone(r._binding(include_epoch=False)['epoch']);r.manager._userspace_runtime_epoch.assert_not_called()
 def test_new_epoch_blocks_reads_but_allows_cleanup_of_same_owned_runtime(self):
  r=object.__new__(u.PersistentUiReader);r.stopping=threading.Event()
  r.binding={'pid':1,'pid_start':2,'image':'a','instance':'id','tag':'tag','epoch':'old'}
  r._binding=Mock(return_value={**r.binding,'epoch':'new'})
  with self.assertRaisesRegex(u.UiReaderError,'runtime_changed'):r._verify()
  r._verify(cleanup=True)
 def test_foreign_runtime_never_receives_cleanup_signal(self):
  r=object.__new__(u.PersistentUiReader);r.stopping=threading.Event()
  r.binding={'pid':1,'pid_start':2,'image':'a','instance':'id','tag':'tag','epoch':'old'}
  r._binding=Mock(return_value={**r.binding,'pid':44});r._private_ok=Mock(return_value=True);r.cleanup_targets={}
  with patch.object(u.os,'kill')as kill,self.assertRaisesRegex(u.UiReaderError,'runtime_changed'):r._terminate(None)
  kill.assert_not_called()
 def test_compiler_capture_excludes_foreign_tree_and_survives_reparenting(self):
  r=object.__new__(u.PersistentUiReader);r._verify=Mock();r._private_ok=Mock(return_value=True);r._mark_cancelled=Mock();r.pidns='ns';r.private_identity=(1,2,1000);r.cleanup_targets={}
  base={'uid':1000,'pidns':'ns','nspid':1}
  helper={**base,'pid':10,'ppid':1,'starttime':100,'state':'T','cmdline':[b'app_process',b'org.xenoid.qa.StockUiServer']}
  compiler={**base,'pid':20,'ppid':10,'starttime':200,'cmdline':[b'/apex/bin/dex2oat64']}
  foreign={**base,'pid':21,'ppid':999,'starttime':210,'cmdline':[b'/apex/bin/dex2oat64']}
  r._helper=Mock(return_value=helper);r._matches=Mock(return_value=True)
  original=Path.iterdir
  def entries(path):
   if str(path)=='/proc':return iter([Path('/proc/10'),Path('/proc/20'),Path('/proc/21')])
   return original(path)
  with patch.object(Path,'iterdir',entries),patch.object(u,'proc_record',side_effect=lambda pid:{10:helper,20:compiler,21:foreign}[pid]),patch.object(u.os,'kill')as freeze:r._capture_cleanup()
  freeze.assert_called_once_with(10,signal.SIGSTOP)
  self.assertEqual(set(r.cleanup_targets),{10,20})
  live={20:{**compiler,'ppid':1}}
  r._helper=Mock(return_value=None)
  def read(pid):
   if pid not in live:raise FileNotFoundError
   return live[pid]
  def kill(pid,sig):live.pop(pid,None)
  with patch.object(u,'proc_record',side_effect=read),patch.object(u.os,'kill',side_effect=kill)as signal_process:r._terminate(None)
  signal_process.assert_called_once_with(20,signal.SIGTERM)
 def test_failed_cleanup_resumes_only_its_verified_frozen_helper(self):
  helper={'pid':10,'state':'T'}
  r=object.__new__(u.PersistentUiReader);r.cleanup_targets={10:helper}
  r._capture_cleanup=Mock(side_effect=u.UiReaderError('temporary failure'))
  r._live_cleanup_target=Mock(return_value=helper)
  with patch.object(u.os,'kill')as kill,self.assertRaisesRegex(u.UiReaderError,'temporary failure'):
   r._terminate(None)
  kill.assert_called_once_with(10,signal.SIGCONT)
 def test_stopped_helper_is_killed_without_resume_fork_window(self):
  helper={'pid':10,'kind':'helper','state':'T'}
  r=object.__new__(u.PersistentUiReader);r.cleanup_targets={10:helper};r._capture_cleanup=Mock()
  live=[helper]
  r._live_cleanup_target=lambda expected:live[0] if live else None
  def killed(pid,sig):live.clear()
  with patch.object(u.os,'kill',side_effect=killed)as kill:r._terminate(None)
  kill.assert_called_once_with(10,signal.SIGKILL)

class BrokerTests(unittest.TestCase):
 def broker(self):
  b=h.Broker(object(),Mock(),config={'command_timeout':0});b.container_id='a'*64
  @contextlib.contextmanager
  def owned():yield object()
  b.owned_manager=owned
  return b
 def test_default_is_persistent_reuses_process_and_unlimited_timeout(self):
  b=self.broker();session=Mock();session.manager.instance_operation_lock.side_effect=lambda **kw:contextlib.nullcontext();session.pipe.closed=False;session.pipe.process.poll.return_value=None;session.priority=None;session.read.return_value='<?xml version="1.0"?><hierarchy></hierarchy>'
  with patch.object(u,'PersistentUiReader',return_value=session)as factory:
   self.assertEqual(b.ui_hierarchy(None),'<hierarchy></hierarchy>');b.ui_hierarchy(None)
  factory.assert_called_once();self.assertIsNone(factory.call_args.args[3]);self.assertEqual(session.read.call_count,2)
 def test_persistent_windows_returns_displays_and_legacy_is_explicit(self):
  b=self.broker();session=Mock();session.manager.instance_operation_lock.side_effect=lambda **kw:contextlib.nullcontext();session.read.return_value='<displays></displays>';b.legacy_ui_hierarchy=Mock(return_value='<hierarchy></hierarchy>')
  with patch.object(u,'PersistentUiReader',return_value=session):self.assertEqual(b.ui_hierarchy(None,windows=True),'<displays></displays>')
  self.assertTrue(session.read.call_args.args[1]);self.assertEqual(b.ui_hierarchy(None,reader='legacy'),'<hierarchy></hierarchy>');session.close.assert_called_once()
 def test_reader_failure_does_not_silently_fallback(self):
  b=self.broker();b.legacy_ui_hierarchy=Mock()
  with patch.object(u,'PersistentUiReader',side_effect=u.UiReaderError('start failed')):
   with self.assertRaises(h.TransportUnavailable):b.ui_hierarchy(None)
  b.legacy_ui_hierarchy.assert_not_called()
 def test_pending_reader_is_published_before_blocking_startup(self):
  b=self.broker();session=Mock()
  def start():
   self.assertIs(b._ui_reader,session)
   raise u.UiReaderError('startup cancelled')
  session.start.side_effect=start
  with patch.object(u,'PersistentUiReader',return_value=session)as factory:
   with self.assertRaises(h.TransportUnavailable):b.ui_hierarchy(None)
  self.assertTrue(factory.call_args.kwargs['defer_start']);self.assertIs(b._ui_reader,session)
 def test_stop_interrupts_blocking_setup_command_before_ready(self):
  r=object.__new__(u.PersistentUiReader);r.stopping=threading.Event();r._argv=lambda command:[sys.executable,'-c','import time;time.sleep(20)']
  errors=[]
  def run():
   try:r._adb('id -u',None)
   except Exception as error:errors.append(error)
  thread=threading.Thread(target=run);thread.start();time.sleep(.05);r.stopping.set();thread.join(2)
  self.assertFalse(thread.is_alive());self.assertEqual(len(errors),1);self.assertRegex(str(errors[0]),'stopping')
 def test_deferred_constructor_performs_no_runtime_or_process_work(self):
  manager=Mock();stopping=threading.Event()
  with patch.object(u.subprocess,'Popen')as popen:
   r=u.PersistentUiReader(manager,'a'*64,0,None,stopping,defer_start=True)
  self.assertFalse(r.started);self.assertIsNone(r.binding);manager._owned_container_record.assert_not_called();popen.assert_not_called()
 def test_round_shutdown_waits_for_stop_during_setup_then_cleans(self):
  r=object.__new__(u.PersistentUiReader);r.stopping=threading.Event();r._argv=lambda command:[sys.executable,'-c','import time;time.sleep(20)']
  errors=[]
  def setup():
   try:r._adb('id -u',None)
   except Exception as error:errors.append(error)
  active=threading.Thread(target=setup);active.start();time.sleep(.05)
  server=Mock();server.active=active;broker=Mock();broker.stopping=r.stopping
  def cleaned():self.assertFalse(active.is_alive())
  broker.cleanup.side_effect=cleaned
  h.finish_device_requests(server,Mock(),broker)
  self.assertEqual(len(errors),1);self.assertRegex(str(errors[0]),'stopping');broker.cleanup.assert_called_once()
 def test_round_shutdown_does_not_race_an_unfinished_request(self):
  server=Mock();server.active.is_alive.return_value=True;broker=Mock()
  with self.assertRaisesRegex(RuntimeError,'did not finish'):h.finish_device_requests(server,Mock(),broker)
  server.active.join.assert_called_once_with(timeout=20);broker.cleanup.assert_not_called()
 def test_gen_and_round_cleanup_close_reader(self):
  b=self.broker();session=Mock();session.manager.instance_operation_lock.side_effect=lambda **kw:contextlib.nullcontext();b._ui_reader=session;b.invalidate_regeneration_caches();session.close.assert_called_once_with(force=True);self.assertIsNone(b._ui_reader)
  session=Mock();session.manager.instance_operation_lock.side_effect=lambda **kw:contextlib.nullcontext();b._ui_reader=session;b.cleanup();session.close.assert_called_once_with(force=False)
 def test_sdk_keeps_old_d_and_w_call_shapes_and_adds_explicit_reader(self):
  d=runtime.Device()
  with patch.object(runtime,'request',side_effect=lambda action,args:(action,args)):
   self.assertEqual(d.inspect_window(),('inspect-window',[]));self.assertEqual(d.inspect_window(windows=True),('inspect-window',[None,None,True]));self.assertEqual(d.inspect_window(reader='legacy'),('inspect-window',[None,None,False,'legacy']))
 def test_invalid_reader_rejects_without_opening_device(self):
  b=self.broker()
  with self.assertRaises(ValueError):b.inspect_window(reader='other')


class ReaderRegenerationOrderTests(unittest.TestCase):
 def fixture(self):
  from test_regeneration_sdk import RegenerationRuntimeTests
  fixture=RegenerationRuntimeTests('test_committed_result_and_cache_invalidation');fixture.setUp();self.addCleanup(fixture.doCleanups);return fixture
 def test_reader_is_closed_before_first_native_regeneration_and_not_after_commit(self):
  f=self.fixture();order=[];session=Mock();session.manager.instance_operation_lock.side_effect=lambda **kw:contextlib.nullcontext();session.close.side_effect=lambda **kw:order.append('reader-closed');f.broker._ui_reader=session
  cli=f.broker.regeneration_cli();old=cli.cmd_device_regenerate
  def command(args):
   self.assertEqual(order,['reader-closed']);self.assertIsNone(f.broker._ui_reader);order.append('native-regenerate');return old(args)
  cli.cmd_device_regenerate=command;f.broker.regeneration_cli=lambda:cli
  self.assertEqual(f.broker.regenerate(0,'reader-order')['phase'],'committed');self.assertEqual(order,['reader-closed','native-regenerate'])
 def test_failed_reader_cleanup_preserves_reference_and_prevents_native_transition(self):
  f=self.fixture();session=Mock();session.manager.instance_operation_lock.side_effect=lambda **kw:contextlib.nullcontext();session.close.side_effect=u.UiReaderError('cleanup pending');f.broker._ui_reader=session
  with self.assertRaises(h.RegenerationPending):f.broker.regenerate(0,'reader-cleanup-pending')
  self.assertIs(f.broker._ui_reader,session);self.assertEqual(f.manager.rotations,0);self.assertEqual(f.manager.commands,0)
  session.close.side_effect=None;self.assertEqual(f.broker.regenerate(0,'reader-cleanup-pending')['phase'],'committed');self.assertEqual(f.manager.rotations,1)

if __name__ == "__main__":
 unittest.main()
