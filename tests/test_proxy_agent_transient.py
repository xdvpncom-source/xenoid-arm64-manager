"""Exercise the actual native agent loop and channel across transport failures."""
import contextlib
import copy
import errno
import json
from pathlib import Path
import runpy
import socket
import stat
import struct
import sys
import types
import unittest
from unittest.mock import patch

import test_proxy_domain_routing as routing

PROTOCOL=types.ModuleType('xenoid.proxy_protocol')
exec(compile((routing.CANDIDATE/'src/xenoid/proxy_protocol.py').read_text(),'actual-native-protocol','exec'),PROTOCOL.__dict__)
PARENT=types.ModuleType('xenoid');PARENT.__path__=[]
with patch.dict(sys.modules,{'xenoid':PARENT,'xenoid.proxy_protocol':PROTOCOL,'xenoid.proxy_source':routing.NATIVE}):
    AGENT=runpy.run_path(str(routing.CANDIDATE/'scripts/xenoid-proxy-agent.py'))
GLOBALS=AGENT['_run'].__globals__
MANIFEST={'instanceId':'12345678-1234-4234-8234-123456789abc','resourceTag':'123456789abc','runtimeEpoch':'a'*64,'generation':7,'users':{'agent':'fixture'},'paths':{},'engine':{'binarySha256':'f'*64}}


class Clock:
    def __init__(self):self.now=0.;self.sleeps=[];self.on_sleep=None
    def monotonic(self):return self.now
    def time(self):return 1795254000+self.now
    def sleep(self,seconds):
        self.sleeps.append(seconds);self.now+=seconds
        if self.on_sleep:self.on_sleep(seconds)


class TransportClassificationTests(unittest.TestCase):
    def test_port_fd_and_network_exhaustion_are_transient(self):
        for code in (errno.EMFILE,errno.ENFILE,errno.EADDRNOTAVAIL,errno.ECONNREFUSED,errno.ECONNRESET,errno.ETIMEDOUT,errno.EPIPE,errno.ENETUNREACH):
            with self.subTest(code=code):self.assertIsInstance(AGENT['_transport_failure'](OSError(code,'fixture')),AGENT['TransientTransportFailure'])
        self.assertIsInstance(AGENT['_transport_failure'](TimeoutError('timeout')),AGENT['TransientTransportFailure'])

    def test_permissions_invalid_fd_and_unknown_os_errors_are_fatal(self):
        for code in (errno.EACCES,errno.EPERM,errno.EBADF,errno.EINVAL,errno.ENOTSOCK,errno.ENOENT,None):
            with self.subTest(code=code):self.assertNotIsInstance(AGENT['_transport_failure'](OSError(code,'fixture')),AGENT['TransientTransportFailure'])

    def test_socket_creation_emfile_is_classified_without_losing_channel(self):
        ipc=AGENT['EngineIPC'](MANIFEST,123)
        info=types.SimpleNamespace(st_mode=stat.S_IFSOCK|0o660,st_uid=0,st_gid=123)
        with patch.object(Path,'stat',return_value=info),patch.object(socket,'SOCK_CLOEXEC',0,create=True),patch.object(socket,'socket',side_effect=OSError(errno.EMFILE,'fixture')),self.assertRaises(AGENT['TransientTransportFailure']):ipc._connect(1)

    def test_socket_owner_mismatch_is_not_retried(self):
        ipc=AGENT['EngineIPC'](MANIFEST,123);info=types.SimpleNamespace(st_mode=stat.S_IFSOCK|0o660,st_uid=42,st_gid=123)
        with patch.object(Path,'stat',return_value=info),self.assertRaises(AGENT['AgentFailure'])as error:ipc._connect(1)
        self.assertNotIsInstance(error.exception,AGENT['TransientTransportFailure'])


class ChannelSequenceTests(unittest.TestCase):
    def setUp(self):
        self.clock=Clock();self.enterContext(patch.dict(GLOBALS,{'_STOP':False,'time':self.clock}))

    def test_lost_reply_and_unsent_request_preserve_session_and_use_fresh_sequences(self):
        recorded=[];replay=PROTOCOL.ReplayWindow();parent=self
        class Engine:
            def daemon_request(self,payload,token,timeout):
                env=json.loads(payload);recorded.append(env)
                if len(recorded)==1:raise AGENT['TransientTransportFailure']('engine_unavailable')
                replay.check(env['session'],env['seq']);replay.commit(env['session'],env['seq'])
                if len(recorded)==2:raise AGENT['TransientTransportFailure']('engine_unavailable')
                return json.dumps({'request':env,'body':{'accepted':True}}).encode()
        def seal(_key,instance,epoch,session,sequence,request,operation,timestamp,body):return {'session':session,'seq':sequence,'requestId':request,'instanceId':instance,'runtimeEpoch':epoch}
        def opened(_key,outer,_instance,_epoch,session,sequence,request):
            self.assertEqual((outer['request']['session'],outer['request']['seq'],outer['request']['requestId']),(session,sequence,request));return {'ok':True,'status':200,'body':outer['body']}
        with patch.dict(GLOBALS,{'_engine':lambda:Engine(),'seal_request':seal,'open_response':opened}):
            channel=AGENT['DaemonChannel'](MANIFEST,bytearray(b'a'*32),bytearray(b'b'*44));session=channel._session
            self.assertEqual(channel.call('desired',{}),{'accepted':True});self.assertEqual(channel._session,session)
            self.assertEqual([r['seq']for r in recorded],[1,2,3]);self.assertEqual(len({r['requestId']for r in recorded}),3);self.assertEqual({r['session']for r in recorded},{session})
            with self.assertRaises(PROTOCOL.ProxyProtocolError):replay.check(session,2)
            channel.close();self.assertTrue(all(v==0 for v in channel._c2s_key+channel._s2c_key+channel._agent_token))

    def test_authenticated_explicit_unreachable_is_transient_but_auth_failure_is_fatal(self):
        def seal(*_):return {'fixture':True}
        for code,expected in [('agent_channel_unreachable',AGENT['TransientTransportFailure']),('agent_channel_auth_failed',AGENT['ChannelAuthenticationFailure'])]:
            engine=types.SimpleNamespace(daemon_request=lambda *_args,code=code:(_ for _ in ()).throw(AGENT['AgentFailure'](code)))
            with self.subTest(code=code),patch.dict(GLOBALS,{'_engine':lambda:engine,'seal_request':seal}):
                channel=AGENT['DaemonChannel'](MANIFEST,bytearray(b'a'*32),bytearray(b'b'*44))
                with self.assertRaises(expected):channel._once('desired',{})
                channel.close()

    def test_binding_permission_and_malformed_errors_do_not_retry(self):
        for code in ('runtime_identity_mismatch','manifest_live_mismatch','ipc_peer_denied','ipc_request_invalid'):
            attempts=[]
            class Engine:
                def daemon_request(self,*_):attempts.append(1);raise AGENT['AgentFailure'](code)
            with self.subTest(code=code),patch.dict(GLOBALS,{'_engine':lambda:Engine(),'seal_request':lambda *_:{'fixture':True}}):
                channel=AGENT['DaemonChannel'](MANIFEST,bytearray(b'a'*32),bytearray(b'b'*44))
                with self.assertRaises(AGENT['AgentFailure']):channel.call('desired',{})
                self.assertEqual(attempts,[1]);channel.close()


class ActualAgentLoopTests(unittest.TestCase):
    def setUp(self):
        self.clock=Clock();self.events=[];self.channels=[];self.desired_count=0;self.scenarios=[];self.probe_count=0
        parent=self
        class Channel:
            def __init__(self,*args):self.session='unchanged-session';self.closed=False;parent.channels.append(self)
            def close(self):self.closed=True
            def call(self,operation,body):parent.events.append(('channel',operation));return {}
        self.channel_class=Channel
        self.state={'generation':7,'phase':'active','structuralApplied':True,'dataPlaneVerified':True,'capabilities':{key:True for key in AGENT['CAPABILITY_KEYS']},'counters':{},'selectedNode':'chosen','nodeCount':1,'nodes':['chosen']}
        self.source={'kind':'endpoint','value':'socks5://fixture@example.com:1080','selectedNode':'chosen','udpAllowed':True,'ipv6Allowed':True,'allowInsecureHttp':False}
        def desired(*_):
            self.desired_count+=1
            if not self.scenarios:
                GLOBALS['_STOP']=True;raise AGENT['AgentFailure']('agent_stopping')
            answer=self.scenarios.pop(0)
            if isinstance(answer,BaseException):raise answer
            return {'generation':7,'enabled':True,'checkId':answer,'source':copy.deepcopy(self.source)}
        def probe(*_):self.probe_count+=1;self.events.append(('probe',self.probe_count));return copy.deepcopy(self.state)
        self.enterContext(patch.dict(GLOBALS,{'_STOP':False,'time':self.clock,'DaemonChannel':Channel,'_desired':desired,'_write_health':lambda _m,phase,generation,code='':self.events.append(('health',phase,generation,code)),'_live_status':lambda *_:copy.deepcopy(self.state),'_run_root':lambda action,*_:self.events.append(('root',action))or {'ok':True},'_compile_source':lambda *_:{'config':routing.config(),'nodeNames':['chosen']},'_validate_binary':lambda *_:None,'_report_staging':lambda *_:None,'_report_status':lambda *_args,**_kwargs:self.events.append(('report','success')),'_report_failure':lambda *_args,**_kwargs:self.events.append(('report','failure')),'_probe_data_plane':probe,'_commit_candidate':lambda:self.events.append(('root','commit')),'_rollback_previous':lambda:self.events.append(('root','rollback')),'_quarantine':lambda *_:self.events.append(('root','quarantine')),'_engine':lambda:types.SimpleNamespace(generation=7),'load_proxy_routing_policy':lambda:routing.DEFAULT}))

    def run_loop(self):
        return AGENT['_run'](MANIFEST,Path('/fixture/manifest.json'),bytearray(b'a'*32),bytearray(b'b'*44),{'generation':7,'structuralApplied':False})

    def test_transient_desired_transport_retains_the_same_channel_and_recovers(self):
        self.scenarios=[AGENT['TransientTransportFailure']('agent_channel_unreachable'),1]
        self.assertEqual(self.run_loop(),0);self.assertEqual(len(self.channels),1);self.assertTrue(self.channels[0].closed);self.assertIn(('report','success'),self.events);self.assertIn(('health','quarantined',7,'agent_channel_unreachable'),self.events);self.assertEqual(sum(self.clock.sleeps),8.)

    def test_repeated_outage_uses_bounded_backoff_and_does_not_busy_loop(self):
        self.scenarios=[AGENT['TransientTransportFailure']('engine_unavailable')for _ in range(4)]+[1]
        self.assertEqual(self.run_loop(),0);self.assertEqual(len(self.channels),1);self.assertEqual(sum(self.clock.sleeps),143.);self.assertLessEqual(max(self.clock.sleeps),3.)

    def test_transport_failure_while_reporting_failure_no_longer_escapes_the_loop(self):
        self.scenarios=[1,1];attempt=[0];reports=[0]
        def compile_source(*_):
            attempt[0]+=1
            if attempt[0]==1:raise AGENT['AgentFailure']('source_fetch_denied')
            return {'config':routing.config(),'nodeNames':['chosen']}
        def report_failure(*_args,**_kwargs):
            reports[0]+=1
            if reports[0]==1:raise AGENT['TransientTransportFailure']('agent_channel_unreachable')
        with patch.dict(GLOBALS,{'_compile_source':compile_source,'_report_failure':report_failure}):self.assertEqual(self.run_loop(),0)
        self.assertEqual(len(self.channels),1);self.assertIn(('report','success'),self.events)

    def test_provider_probe_outage_keeps_agent_and_lkg_without_direct_fallback(self):
        self.scenarios=[1,2,2,2,2];attempt=[0]
        def probe(*_):
            attempt[0]+=1;self.events.append(('probe',attempt[0]))
            if attempt[0]==2:raise AGENT['AgentFailure']('proxy_check_timeout')
            return copy.deepcopy(self.state)
        with patch.dict(GLOBALS,{'_probe_data_plane':probe}):self.assertEqual(self.run_loop(),0)
        self.assertEqual(len(self.channels),1);self.assertEqual(attempt[0],3)
        self.assertNotIn(('root','off'),self.events)
        failed=self.events.index(('report','failure'));recovered=self.events.index(('probe',3))
        self.assertFalse(any(item[:2]==('health','ready')for item in self.events[failed:recovered]))

    def test_lost_success_report_reply_after_commit_recovers_with_a_new_probe(self):
        self.scenarios=[1]*8;reports=[0]
        def report_status(*_args,**_kwargs):
            reports[0]+=1
            if reports[0]==1:
                self.events.append(('report','accepted-but-reply-lost'))
                raise AGENT['TransientTransportFailure']('agent_channel_unreachable')
            self.events.append(('report','success'))
        with patch.dict(GLOBALS,{'_report_status':report_status}):self.assertEqual(self.run_loop(),0)
        self.assertEqual(len(self.channels),1);self.assertEqual(self.probe_count,2)
        lost=self.events.index(('report','accepted-but-reply-lost'));recovered=self.events.index(('report','success'))
        self.assertIn(('root','commit'),self.events[:lost]);self.assertTrue(any(item[0]=='probe'for item in self.events[lost:recovered]));self.assertFalse(any(item[:2]==('health','ready')for item in self.events[lost:recovered]));self.assertNotIn(('root','off'),self.events)

    def test_candidate_and_probe_resource_exhaustion_recover_without_new_identity(self):
        for stage in ('compile','probe'):
            with self.subTest(stage=stage):
                self.scenarios=[1]*8;self.probe_count=0;self.events.clear();self.channels.clear();GLOBALS['_STOP']=False;attempt=[0]
                def fail_once(*_):
                    attempt[0]+=1
                    if attempt[0]==1:raise AGENT['_transport_failure'](OSError(errno.EADDRNOTAVAIL if stage=='probe'else errno.EMFILE,'fixture'))
                    return copy.deepcopy(self.state)if stage=='probe'else{'config':routing.config(),'nodeNames':['chosen']}
                with patch.dict(GLOBALS,{'_compile_source'if stage=='compile'else'_probe_data_plane':fail_once}):self.assertEqual(self.run_loop(),0)
                self.assertEqual(len(self.channels),1);self.assertIn(('report','success'),self.events);self.assertGreaterEqual(attempt[0],2);self.assertNotIn(('root','off'),self.events)

    def test_successful_poll_does_not_clear_transport_error_without_a_fresh_probe(self):
        self.scenarios=[1,AGENT['TransientTransportFailure']('agent_channel_unreachable'),1,1,2]
        self.assertEqual(self.run_loop(),0)
        failed=self.events.index(('health','error',7,'agent_channel_unreachable'));recovered=self.events.index(('probe',2));self.assertFalse(any(item[:2]==('health','ready')for item in self.events[failed:recovered]))

    def test_same_check_recovers_automatically_with_fresh_probe_after_transport(self):
        self.scenarios=[1,AGENT['TransientTransportFailure']('agent_channel_unreachable'),1]
        self.assertEqual(self.run_loop(),0);self.assertEqual(self.probe_count,2);self.assertEqual(len(self.channels),1)
        failed=self.events.index(('health','error',7,'agent_channel_unreachable'));recovered=self.events.index(('probe',2));self.assertFalse(any(item[:2]==('health','ready')for item in self.events[failed:recovered]));self.assertIn(('health','ready',7,''),self.events[recovered:])

    def test_unchanged_digest_refresh_cannot_clear_transient_from_stale_proof(self):
        self.scenarios=[1,AGENT['TransientTransportFailure']('agent_channel_unreachable'),1];original=GLOBALS['_desired'];calls=[0];compiled=[0]
        def desired(*args):
            calls[0]+=1
            if calls[0]==2:self.clock.now=350.
            return original(*args)
        def compiler(*_):compiled[0]+=1;return {'config':routing.config(),'nodeNames':['chosen']}
        with patch.dict(GLOBALS,{'_desired':desired,'_compile_source':compiler}):self.assertEqual(self.run_loop(),0)
        self.assertEqual(compiled[0],2);self.assertEqual(self.probe_count,2)
        failed=self.events.index(('health','error',7,'agent_channel_unreachable'));recovered=self.events.index(('probe',2));self.assertFalse(any(item[:2]==('health','ready')for item in self.events[failed:recovered]));self.assertEqual(self.events.count(('root','write-config')),1)

    def test_authenticated_stale_probe_reloads_new_check_without_old_failure_report(self):
        self.scenarios=[1,AGENT['TransientTransportFailure']('agent_channel_unreachable'),1,2];attempt=[0]
        def probe(*_):
            attempt[0]+=1;self.events.append(('probe',attempt[0]))
            if attempt[0]==2:raise AGENT['DesiredStateAdvanced']('stale_probe')
            return copy.deepcopy(self.state)
        with patch.dict(GLOBALS,{'_probe_data_plane':probe}):self.assertEqual(self.run_loop(),0)
        self.assertEqual(attempt[0],3);self.assertEqual(len(self.channels),1);self.assertNotIn(('report','failure'),self.events);self.assertIn(('health','error',7,'stale_probe'),self.events);self.assertNotIn(('root','off'),self.events)

    def test_stale_probe_during_candidate_waits_for_monotonic_new_desired(self):
        self.scenarios=[1,2];attempt=[0]
        def probe(*_):
            attempt[0]+=1
            if attempt[0]==1:raise AGENT['DesiredStateAdvanced']('stale_probe')
            return copy.deepcopy(self.state)
        with patch.dict(GLOBALS,{'_probe_data_plane':probe}):self.assertEqual(self.run_loop(),0)
        self.assertEqual(attempt[0],2);self.assertNotIn(('report','failure'),self.events);self.assertIn(('report','success'),self.events);self.assertEqual(len(self.channels),1);self.assertEqual(sum(self.clock.sleeps),6.)

    def test_repeated_authenticated_check_advancement_is_bounded_not_busy_loop(self):
        self.scenarios=list(range(1,7));attempt=[0]
        def probe(*_):
            attempt[0]+=1
            if attempt[0]<=5:raise AGENT['DesiredStateAdvanced']('stale_probe')
            return copy.deepcopy(self.state)
        with patch.dict(GLOBALS,{'_probe_data_plane':probe}):self.assertEqual(self.run_loop(),0)
        self.assertEqual(len(self.channels),1);self.assertEqual(attempt[0],6);self.assertEqual(sum(self.clock.sleeps),18.);self.assertNotIn(('report','failure'),self.events)

    def test_actual_probe_classifies_only_authenticated_stale_result_as_advancement(self):
        desired={'checkId':1};response={'checkId':1,'capabilities':{key:False for key in AGENT['CAPABILITY_KEYS']},'elapsedMs':1,'errorCode':'stale_probe'}
        channel=types.SimpleNamespace(call=lambda *_:response)
        with self.assertRaises(AGENT['DesiredStateAdvanced']):AGENT['_probe_data_plane'](channel,desired,MANIFEST,Path('/fixture/manifest.json'),self.state)
        response['errorCode']='proxy_check_timeout'
        with self.assertRaises(AGENT['AgentFailure'])as error:AGENT['_probe_data_plane'](channel,desired,MANIFEST,Path('/fixture/manifest.json'),self.state)
        self.assertNotIsInstance(error.exception,AGENT['DesiredStateAdvanced'])
        response['checkId']=2
        with self.assertRaises(AGENT['ChannelAuthenticationFailure']):AGENT['_probe_data_plane'](channel,desired,MANIFEST,Path('/fixture/manifest.json'),self.state)

    def test_authentication_failure_is_fatal_without_retry_or_new_session(self):
        self.scenarios=[AGENT['ChannelAuthenticationFailure']('agent_channel_auth_failed')]
        with self.assertRaises(AGENT['ChannelAuthenticationFailure']):self.run_loop()
        self.assertEqual(len(self.channels),1);self.assertEqual(self.clock.sleeps,[]);self.assertNotIn(('report','success'),self.events)

    def test_identity_mismatch_is_fatal_without_transport_retry(self):
        self.scenarios=[AGENT['AgentFailure']('manifest_live_mismatch')]
        with self.assertRaises(AGENT['AgentFailure']):self.run_loop()
        self.assertEqual(len(self.channels),1);self.assertEqual(self.clock.sleeps,[])

    def test_sigterm_during_backoff_stops_within_one_second(self):
        self.scenarios=[AGENT['TransientTransportFailure']('engine_unavailable')]
        self.clock.on_sleep=lambda _:GLOBALS.__setitem__('_STOP',True)
        self.assertEqual(self.run_loop(),0);self.assertLessEqual(sum(self.clock.sleeps),1.);self.assertEqual(self.desired_count,1)


if __name__=='__main__':unittest.main()
