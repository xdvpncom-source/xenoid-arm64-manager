#!/usr/bin/env python3
"""Real host/client bootstrap retry contracts, with an in-memory daemon peer."""
import contextlib,copy,importlib.util,json,os,sys,types,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
BASE=Path(os.environ.get('XENOID_TEST_BASE_PROJECT',str(ROOT))).resolve()
sys.path.insert(0,str(BASE/'src'))
import xenoid
xenoid.__path__.insert(0,str(ROOT/'src/xenoid'))
from xenoid import backend,daemon_client,util
spec=importlib.util.spec_from_file_location('bootstrap_contract',ROOT/'scripts/test-bootstrap-contract.py')
contract=importlib.util.module_from_spec(spec);spec.loader.exec_module(contract)

class Peer(contract.FakeDaemon):
    """GET retains a failure; only the existing coordinator POST does work."""
    def __init__(self,clock):
        super().__init__(clock);self.state='degraded';self.generation=1
        self.cached_ready=False;self.native_ok=True;self.attempted=False
        self.wrong_epoch=False;self.observed_timeouts=[]
        self.proxy={'ok':True,'state':'quarantined','configured':True,'enabled':True,
                    'generation':7,'quarantined':True}
    def status(self):
        value=super().status()
        if self.attempted and self.state=='ready':self.cached_ready=True
        value['components']['root']={'ok':True,'state':'ready'}
        value['components']['proxy']=copy.deepcopy(self.proxy)
        value['components']['location']={'ok':True,'state':'ready','configured':True}
        value['components']['camera']={'ok':True,'state':'unconfigured','configured':False,'active':False}
        if self.state=='ready'and not self.attempted:
            value['components']['keybox']={'ok':True,'state':'unconfigured','configured':False,'active':False}
        if self.wrong_epoch:value['runtimeEpoch']='ef'*32
        return value
    def handle(self,request,**options):
        method=request.get_method();path=request.full_url.split('/',3)[-1]
        self.observed_timeouts.append(options.get('timeout'))
        if path=='keybox/status':
            self.requests.append({'method':method,'path':'/'+path})
            contract.require(method=='GET','keybox status must be observational')
            token=request.headers.get('X-xenoid-token')or request.headers.get('X-Xenoid-Token')
            contract.require(token==contract.TOKEN,'missing status authentication')
            body={'ok':False,'error':'clear_failed'}
            if self.cached_ready:
                body={'ok':True,'configured':False,'ready':True,'active':False,
                      'algorithms':{'rsa':False,'ecdsa':False,'rsaChainCount':0,'ecdsaChainCount':0}}
            return contract.FakeResponse(200,body)
        if path=='bootstrap/reconcile':
            self.requests.append({'method':method,'path':'/'+path,'body':json.loads(request.data)})
            body=json.loads(request.data)
            contract.require(method=='POST'and body['instanceId']==contract.INSTANCE_ID
                and body['runtimeEpoch']==contract.RUNTIME_EPOCH
                and 1000<=body['timeoutMs']<=230000,'invalid owner retry')
            if self.state not in{'accepted','reconciling','ready'}:
                self.generation+=1;self.state='accepted';self.started=self.clock.now
                self.attempted=True;self.keybox_fails=not self.native_ok
            return contract.FakeResponse(200 if self.state=='ready'else 202,self.status())
        return super().handle(request,**options)

@contextlib.contextmanager
def fixture():
    clock=contract.FakeClock();peer=Peer(clock)
    with contract.client_fixture(peer)as(client,state,argv):
        manager=backend.RuntimeManager.__new__(backend.RuntimeManager)
        manager.context=client._context;manager.lease=client._lease
        manager.daemon_client=lambda**kw:client
        manager.wait_daemon_transport=lambda timeout:{'ok':True,'transportReady':True}
        manager.ensure_daemon_runtime_permissions=lambda:{'ok':True}
        manager.ensure_rootd_root=lambda timeout,client:{'ok':True}
        manager._android_tun_state=lambda**kw:{'ok':True}
        manager._daemon_runtime_epoch=lambda**kw:contract.RUNTIME_EPOCH
        with patch.object(backend.time,'monotonic',side_effect=lambda:clock.now),\
             patch.object(backend.time,'sleep',side_effect=clock.advance):
            yield manager,peer,clock

class Reconcile(unittest.TestCase):
    def test_failed_status_retries_real_authenticated_bootstrap_then_reads_fresh_status(self):
        with fixture()as(manager,peer,clock):
            proxy=copy.deepcopy(peer.proxy)
            result=manager.reconcile_keybox({'action':'reconcile'})
            self.assertTrue(result['ok'],result);self.assertFalse(result['configured'])
            self.assertEqual(peer.generation,2);self.assertEqual(peer.proxy,proxy)
            paths=[r['path']for r in peer.requests]
            self.assertEqual(paths.count('/keybox/status'),2)
            self.assertEqual(paths.count('/bootstrap/reconcile'),1)
            self.assertLess(paths.index('/bootstrap/reconcile'),len(paths)-1)
            self.assertFalse(any(p in('/keybox/clear','/keybox/source','/proxy/source','/proxy/enabled')for p in paths))
            self.assertNotIn(contract.TOKEN,json.dumps(result))
    def test_healthy_unconfigured_is_warm_noop(self):
        with fixture()as(manager,peer,clock):
            peer.cached_ready=True;peer.state='ready'
            self.assertTrue(manager.reconcile_keybox('reconcile')['ok'])
            self.assertEqual([r['path']for r in peer.requests],['/keybox/status'])
    def test_inspect_and_reuse_never_repair_or_hide_error(self):
        for action in('inspect','reuse','none','matching'):
            with self.subTest(action=action),fixture()as(manager,peer,clock):
                result=manager.reconcile_keybox(action)
                self.assertFalse(result['ok']);self.assertTrue(result['skipped'])
                self.assertEqual([r['path']for r in peer.requests],['/keybox/status'])
    def test_persistent_native_failure_does_not_fake_ready(self):
        with fixture()as(manager,peer,clock):
            peer.native_ok=False;result=manager.reconcile_keybox('reconcile')
            self.assertFalse(result['ok']);self.assertEqual(result['error'],'clear_failed')
            self.assertTrue(peer.attempted);self.assertFalse(peer.cached_ready)
    def test_ready_coordinator_with_stale_component_status_stays_failed(self):
        with fixture()as(manager,peer,clock):
            peer.state='ready';result=manager.reconcile_keybox('reconcile')
            self.assertFalse(result['ok']);self.assertEqual(result['error'],'clear_failed')
            self.assertFalse(peer.attempted);self.assertEqual(peer.generation,1)
    def test_wrong_epoch_cannot_accept_a_successful_foreign_reply(self):
        with fixture()as(manager,peer,clock):
            peer.wrong_epoch=True;result=manager.reconcile_keybox('reconcile')
            self.assertFalse(result['ok']);self.assertFalse(peer.cached_ready)
    def test_root_failure_is_propagated_without_clear_or_status_override(self):
        with fixture()as(manager,peer,clock):
            manager.ensure_rootd_root=lambda timeout,client:{'ok':False,'code':'rootd_unavailable'}
            result=manager.reconcile_keybox('reconcile')
            self.assertFalse(result['ok']);self.assertEqual(result['error'],'rootd_unavailable')
            self.assertEqual([r['path']for r in peer.requests],['/keybox/status'])
    def test_existing_parent_deadline_limits_owner_retry(self):
        with fixture()as(manager,peer,clock):
            with util.command_timeout(.5):result=manager.reconcile_keybox('reconcile')
            self.assertFalse(result['ok']);self.assertLessEqual(clock.now,.5)
            self.assertFalse(peer.attempted)
            self.assertTrue(all(t is None or t<=.5 for t in peer.observed_timeouts))
    def test_private_exception_details_never_enter_result(self):
        with fixture()as(manager,peer,clock):
            manager.reconcile_bootstrap=lambda:(_ for _ in()).throw(RuntimeError('private fixture secret'))
            self.assertEqual(manager.reconcile_keybox('reconcile'),{'ok':False,'error':'keybox_daemon_unavailable'})

if __name__=='__main__':unittest.main()
