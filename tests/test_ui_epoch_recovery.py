"""Transient epoch rejection, fenced cleanup and fresh official navigation.

Uses actual production methods and existing local reader/native collaborators;
no Android, SSH, ADB endpoint or native input device is contacted.
"""
import contextlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'qa'))
import qa_ui_reader as u
import script_host as h
import test_persistent_ui_reader as pf
import test_observed_native_navigation as nf

class EpochClassification(unittest.TestCase):
    def reader(self, **changed):
        r = object.__new__(u.PersistentUiReader)
        r.stopping = threading.Event()
        r.binding = dict(pid=44, pid_start=8, image='image', instance='instance', tag='tag', epoch='old')
        r._binding = Mock(return_value={**r.binding, **changed})
        return r

    def test_absent_epoch_is_unreadable_with_same_physical_ownership(self):
        for epoch in (None, ''):
            with self.subTest(epoch=epoch):
                with self.assertRaisesRegex(u.UiReaderError, '^ui_reader_epoch_unreadable$'):
                    self.reader(epoch=epoch)._verify()

    def test_each_real_physical_change_precedes_unreadable_epoch(self):
        for key, value in dict(pid=45, pid_start=9, image='new', instance='foreign', tag='foreign').items():
            with self.subTest(key=key):
                with self.assertRaisesRegex(u.UiReaderError, '^ui_reader_runtime_changed$'):
                    self.reader(**{key:value, 'epoch':None})._verify()

    def test_valid_epoch_transition_remains_fatal(self):
        with self.assertRaisesRegex(u.UiReaderError, '^ui_reader_runtime_changed$'):
            self.reader(epoch='new')._verify()

    def test_cleanup_does_not_require_epoch_but_still_requires_physical_ownership(self):
        r=self.reader(epoch=None);r._verify(cleanup=True)
        r._binding.assert_called_once_with(include_epoch=False)
        with self.assertRaisesRegex(u.UiReaderError, '^ui_reader_runtime_changed$'):
            self.reader(pid_start=9,epoch=None)._verify(cleanup=True)

class EpochNavigation(nf.NativeNavigation):
    def actual_reader(self, epoch=None):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        directory=Path(tmp.name)/'reader';directory.mkdir(mode=0o700)
        (directory/'server.jar').write_bytes(b'local owned helper fixture')
        info=directory.stat();r=object.__new__(u.PersistentUiReader)
        r.manager=self.manager;r.container_id='a'*64;r.stopping=threading.Event()
        r.priority=None;r.deadline=None;r.closed=False;r.started=True
        r.directory=directory;r.private_identity=(info.st_dev,info.st_ino,info.st_uid)
        r.binding=dict(pid=44,pid_start=8,image='image',instance='instance',tag='tag',epoch='old')
        r._binding=Mock(side_effect=lambda *,include_epoch=True:{**r.binding,'epoch':epoch if include_epoch else None})
        r._helper=Mock(return_value=None);r._terminate=Mock();r.cleanup_targets={}
        r.pipe=u.PipeProcess([sys.executable,'-u','-c',pf.CHILD,'ok'],None,r.stopping,r._terminate)
        self.manager.instance_operation_lock=lambda **_:contextlib.nullcontext()
        def cleanup():
            r._terminate.side_effect=None
            r.close(force=True)
        self.addCleanup(cleanup)
        return r

    def test_transient_epoch_rejects_consumed_offer_then_normal_reobserve_recreates_reader(self):
        old=self.actual_reader();self.broker._ui_reader=old
        self.read_seconds=57
        c=nf.cycle.Cycle(self.device(),'v2.vpn.fast')
        first=c.observe();old_token=first['native_navigation_observation']
        self.assertFalse(c.service_missing_ad_close(first))
        self.assertEqual(c.last_navigation_result['reason'],'navigation_reader_epoch_unreadable')
        self.assertEqual(self.injected,[]);self.assertEqual(sum(c.missing_ad_close_backs.values()),0)
        self.assertTrue(old.closed);self.assertIsNone(old.private_identity)
        self.assertTrue(old.pipe.closed);self.assertFalse(old.pipe.cleanup_pending)
        self.assertIsNotNone(old.pipe.process.poll());self.assertFalse(old.directory.exists())
        self.assertFalse(self.broker.handle_window(old_token,'back')['handled'])
        fresh=self.actual_reader(epoch='old');fresh.started=False
        fresh.start=Mock(side_effect=lambda:setattr(fresh,'started',True))
        fresh.pipe.request=Mock(return_value=self.hierarchy.encode())
        fresh.pipe.navigation=Mock(side_effect=lambda display_id,deadline:json.dumps(
            self.fresh_navigation(self.manager,display_id,deadline)).encode('ascii'))
        del self.broker.ui_hierarchy
        with patch.object(u,'PersistentUiReader',return_value=fresh) as factory,patch.object(u,'time',self.clock):
            later=c.observe()
            self.assertNotEqual(later['native_navigation_observation'],old_token)
            factory.assert_called_once();fresh.start.assert_called_once();fresh.pipe.request.assert_called_once()
            self.assertTrue(c.service_missing_ad_close(later))
        self.assertEqual(self.injected,[(10,95)])
        self.assertEqual(sum(c.missing_ad_close_backs.values()),1)
        self.assertFalse(self.action(first)['handled'])
        self.assertEqual(self.daemon.tap.call_count,1)

    def test_epoch_unreadable_after_nav_response_discards_proof_and_cleans_before_reobserve(self):
        r=self.actual_reader(epoch='old');self.broker._ui_reader=r
        r._binding.side_effect=[r.binding,{**r.binding,'epoch':None},{**r.binding,'epoch':None}]
        r.pipe.navigation=Mock(return_value=b'{"schema":"xenoid.nav-node-proof/v1"}')
        window=self.snapshot();result=self.action(window)
        self.assertEqual(result['reason'],'navigation_reader_epoch_unreadable')
        self.assertFalse(result['input_sent']);r.pipe.navigation.assert_called_once()
        self.assertTrue(r.closed);self.assertIsNone(r.private_identity)
        self.assertIsNotNone(r.pipe.process.poll());self.assertEqual(self.injected,[])
        self.assertFalse(self.action(window)['handled'])

    def test_valid_changed_epoch_still_fails_cycle_and_sends_no_input(self):
        r=self.actual_reader(epoch='new');self.broker._ui_reader=r
        window=self.snapshot()
        with self.assertRaisesRegex(h.TransportUnavailable,'ui_reader_runtime_changed'):
            self.action(window)
        self.assertEqual(self.injected,[]);self.assertFalse(self.action(window)['handled'])
        self.assertTrue(r.closed);self.assertIsNone(r.private_identity)

    def test_epoch_read_failure_with_cleanup_error_is_fatal_not_retryable(self):
        r=self.actual_reader();r._terminate.side_effect=u.UiReaderError('fixture cleanup cannot be proven')
        self.broker._ui_reader=r;window=self.snapshot()
        with self.assertRaises(h.TransportUnavailable) as caught:self.action(window)
        self.assertEqual(str(caught.exception.__cause__),'ui_reader_epoch_unreadable')
        self.assertIsNotNone(caught.exception.__cause__.cleanup_error)
        self.assertTrue(r.pipe.cleanup_pending);self.assertIsNotNone(r.private_identity)
        self.assertEqual(self.injected,[]);self.assertFalse(self.action(window)['handled'])

    def test_unconfirmed_cleanup_state_is_never_converted_to_safe_rejection(self):
        for changed in ({'closed':False},{'private_identity':(1,2,3)},
                        {'pipe.closed':False},{'pipe.cleanup_pending':True},{'process_live':True}):
            with self.subTest(changed=changed):
                r=Mock();r.closed=False;r.started=True;r.private_identity=None
                r.pipe.closed=False;r.pipe.cleanup_pending=False;r.pipe.process.poll.return_value=None
                error=u.UiReaderError('ui_reader_epoch_unreadable')
                def fail(*_,r=r,changed=changed,error=error):
                    r.closed=changed.get('closed',True);r.private_identity=changed.get('private_identity')
                    r.pipe.closed=changed.get('pipe.closed',True)
                    r.pipe.cleanup_pending=changed.get('pipe.cleanup_pending',False)
                    r.pipe.process.poll.return_value=None if changed.get('process_live') else 0
                    raise error
                r.navigation.side_effect=fail;self.broker._ui_reader=r
                with self.assertRaises(h.TransportUnavailable):self.action(self.snapshot())
        self.assertEqual(self.injected,[])

    def test_lost_input_ack_even_with_same_error_enum_is_never_retried(self):
        error=u.UiReaderError('ui_reader_epoch_unreadable')
        def lost(x,y):
            self.injected.append((x,y));raise error
        self.daemon.tap.side_effect=lost;window=self.snapshot()
        with self.assertRaises(u.UiReaderError) as caught:self.action(window)
        self.assertIs(caught.exception,error)
        self.assertFalse(self.action(window)['handled']);self.assertEqual(self.injected,[(10,95)])
        self.assertEqual(self.daemon.tap.call_count,1)

def load_tests(loader,tests,pattern):
    # Reuse fixture setup while keeping the unchanged inherited test suite in
    # its existing module, so discovery runs each old contract only once.
    return unittest.TestSuite(cls(name) for cls in (EpochClassification,EpochNavigation)
        for name in sorted(cls.__dict__) if name.startswith('test_'))

if __name__=='__main__':unittest.main(verbosity=2)
