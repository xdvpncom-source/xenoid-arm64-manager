#!/usr/bin/env python3
"""Real dispatch/native-flock regression; no runtime or network actions."""
import ast
from contextlib import contextmanager
from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

HERE=Path(__file__).resolve().parent;SOURCE=HERE.parent;sys.path.insert(0,str(SOURCE/'src'))
from xenoid import mcp_server as mcp, remote_service as remote
from xenoid.config import InstanceError
from xenoid.device_identity import IdentityError
from xenoid.daemon_client import wait_for_proxy_check
from xenoid.operation_lock import instance_operation_lock,operation_lock_is_held

INSTANCE='11111111-1111-4111-8111-111111111111';OTHER='22222222-2222-4222-8222-222222222222';EPOCH='a'*64
POLICY={'schema':'dev.xenoid.proxy-failure-policy/v1','mode':'retry_proxy','retry_delays_seconds':[5,15,60]}

# The native lock regression needs no manager checkout. With the public manager
# path explicitly supplied, exercise its actual official Broker body as well.
# Example: XENOID_MANAGER_ROOT=/path/to/manager python3 -B scripts/test-proxy-diagnostic-dispatch.py
broker_owned=None
if os.environ.get('XENOID_MANAGER_ROOT'):
    qa=Path(os.environ['XENOID_MANAGER_ROOT'])/'qa/script_host.py'
    tree=ast.parse(qa.read_bytes());klass=next(n for n in tree.body if isinstance(n,ast.ClassDef)and n.name=='Broker');method=next(n for n in klass.body if isinstance(n,ast.FunctionDef)and n.name=='owned_manager')
    namespace={'contextmanager':contextmanager,'re':re,'TransportUnavailable':RuntimeError,'RuntimeIdentityError':RuntimeError}
    exec(compile(ast.Module(body=[method],type_ignores=[]),str(qa),'exec'),namespace);broker_owned=namespace['owned_manager']

class Dispatch(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.path=Path(self.tmp.name)
        self.context=SimpleNamespace(instance_name='phone-a',instance_id=INSTANCE,resource_tag='fixture',state_root=self.path/INSTANCE,project_root=self.path)
        self.migrations=[];self.allocations=[];self.waits=[];self.mode=POLICY
        self.manager=SimpleNamespace(context=self.context,cfg=object(),lease=SimpleNamespace(instance_id=INSTANCE,resource_tag='fixture'),ensure_instance_lease=lambda:None)
        self.manager.instance_operation_lock=lambda timeout_seconds=None:instance_operation_lock(self.context.state_root,timeout_seconds=timeout_seconds)
        self.manager._owned_container_record=lambda timeout=5:({'Id':'d'*64,'State':{'Running':True}},None)
        self.manager.migrate_legacy_token_state=lambda:self.migrations.append(operation_lock_is_held(INSTANCE))
        self.daemon=SimpleNamespace(proxy_status=self.status,proxy_check=self.allocate)
        self.runtime=mcp.MCPRuntime(self.context,self.manager.cfg,self.manager.lease,self.manager,self.daemon)
        self.pending=None
        self.addCleanup(patch.stopall)
        patch('xenoid.mcp_server.RegenerationJournal',side_effect=lambda context:SimpleNamespace(load=lambda:self.pending)).start()
        patch('xenoid.remote_service.RegenerationJournal',side_effect=lambda context:SimpleNamespace(load=lambda:self.pending)).start()
        patch('xenoid.mcp_server._refresh_stdio_runtime',side_effect=lambda r:r).start()
        self.app=remote.ServiceApplication(self.path);self.app._resolve_runtime=lambda name:self.runtime
        self.grant=remote.AccessGrant('control',frozenset({'control'}),{'phone-a':INSTANCE},'test')

    def status(self):
        value={'ok':True,'enabled':True,'configured':True,'generation':7,'checkId':10,'runtimeEpoch':EPOCH}
        if self.mode is not None:value['failurePolicy']=self.mode
        return value

    def allocate(self):
        self.assertTrue(operation_lock_is_held(INSTANCE));self.allocations.append(11)
        return {'ok':True,'generation':7,'checkId':11}

    def worker_available(self,*,remote_thread=False):
        result=[]
        def worker():
            try:
                with (broker_owned(SimpleNamespace(verify=None,manager=self.manager,container_id=None)) if broker_owned else instance_operation_lock(self.context.state_root,timeout_seconds=0)):
                    result.append('native_worker_owned')
                if remote_thread:
                    with self.app._thread_operation_lock(INSTANCE):result.append('remote_thread_owned')
            except Exception as exc:result.append(type(exc).__name__+':'+str(exc))
        thread=threading.Thread(target=worker);thread.start();thread.join(7)
        self.assertFalse(thread.is_alive());self.assertEqual(result,['native_worker_owned']+(['remote_thread_owned']if remote_thread else[]))

    def diagnostic(self,daemon,**expected):
        self.assertFalse(operation_lock_is_held(INSTANCE));self.assertIs(daemon,self.daemon)
        self.assertEqual(expected,{'expected_check_id':11,'expected_generation':7,'expected_runtime_epoch':EPOCH})
        self.waits.append(expected);self.worker_available(remote_thread=self.remote_thread)
        return {'ok':False,'code':'probe_timeout','error':'probe_timeout'}

    def call(self,kind):
        if kind=='stdio':return mcp._call_stdio_tool(self.runtime,'xenoid_proxy_check',{})
        return self.app._execute_tool(self.grant,'xenoid_proxy_check',{'instance':'phone-a'})

    def test_continuous_stdio_and_remote_wait_release_native_and_thread_locks(self):
        for kind in ('stdio','remote'):
            with self.subTest(kind=kind):
                self.allocations.clear();self.waits.clear();self.migrations.clear();self.remote_thread=kind=='remote'
                with patch('xenoid.mcp_server.wait_for_proxy_check',side_effect=self.diagnostic):result=self.call(kind)
                self.assertEqual(len(self.allocations),1);self.assertEqual(len(self.waits),1);self.assertEqual(self.migrations,[True])
                self.assertIn('probe_timeout',json.dumps(result))

    def test_control_authorization_is_not_downgraded_to_read(self):
        self.grant=replace(self.grant,scopes=frozenset({'read'}))
        with self.assertRaises(remote.ServiceError)as error:self.call('remote')
        self.assertEqual(error.exception.code,'insufficient_scope');self.assertEqual(self.allocations,[])
        self.assertEqual(remote.REMOTE_TOOL_POLICIES['xenoid_proxy_check'].scope,'control');self.assertTrue(remote.REMOTE_TOOL_POLICIES['xenoid_proxy_check'].mutating)

    def test_foreign_instance_grant_cannot_allocate(self):
        self.grant=replace(self.grant,instance_bindings={'phone-a':OTHER});result=self.call('remote')
        self.assertIn('instance_not_available',json.dumps(result));self.assertEqual(self.allocations,[])

    def test_pending_gen_blocks_both_entrypoints_before_migration_or_allocation(self):
        self.pending={'phase':'verified'}
        for kind in ('stdio','remote'):
            with self.subTest(kind=kind):
                result=self.call(kind);self.assertIn('device_regeneration_pending',json.dumps(result));self.assertEqual(self.allocations,[]);self.assertEqual(self.migrations,[])

    def test_instance_change_during_refresh_blocks_both_entrypoints(self):
        changed=replace(self.runtime,context=SimpleNamespace(**{**vars(self.context),'instance_id':OTHER}))
        with patch('xenoid.mcp_server._refresh_stdio_runtime',side_effect=[self.runtime,changed]):
            with self.assertRaises(InstanceError):self.call('stdio')
        seen=iter([self.runtime,changed]);self.app._resolve_runtime=lambda name:next(seen)
        self.assertIn('instance_not_available',json.dumps(self.call('remote')));self.assertEqual(self.allocations,[])

    def test_legacy_retains_original_serialized_tool_path(self):
        self.mode=None
        def legacy(runtime,name,args):
            self.assertTrue(operation_lock_is_held(INSTANCE));self.assertEqual(name,'xenoid_proxy_check');return mcp.text_result({'ok':True,'legacy':True})
        with patch('xenoid.mcp_server.call_tool',side_effect=legacy):self.assertIn('legacy',json.dumps(self.call('stdio')))
        self.app._tool_caller=legacy;self.assertIn('legacy',json.dumps(self.call('remote')));self.assertEqual(self.allocations,[])

    def test_failed_allocation_never_waits_or_allocates_twice(self):
        self.daemon.proxy_check=lambda:{'ok':False,'code':'proxy_disabled'}
        for kind in ('stdio','remote'):
            with patch('xenoid.mcp_server.wait_for_proxy_check')as wait:
                self.assertIn('proxy_disabled',json.dumps(self.call(kind)));wait.assert_not_called()

    def test_real_exact_ticket_wait_rejects_changed_check_generation_or_epoch(self):
        for key,value,code in [('checkId',12,'agent_stale'),('generation',8,'agent_stale'),('runtimeEpoch','b'*64,'agent_stale')]:
            with self.subTest(key=key):
                daemon=SimpleNamespace(proxy_status=lambda:{**self.status(),'checkId':11,key:value},proxy_check=lambda:self.fail('must_not_allocate_again'))
                result=wait_for_proxy_check(daemon,expected_check_id=11,expected_generation=7,expected_runtime_epoch=EPOCH)
                self.assertFalse(result['ok']);self.assertEqual(result['code'],code)

    def test_real_wait_rejects_identity_change_after_the_baseline(self):
        for key,value,code in [('checkId',12,'agent_stale'),('generation',8,'agent_stale'),('runtimeEpoch','b'*64,'runtime_epoch_mismatch')]:
            with self.subTest(key=key):
                initial={**self.status(),'checkId':11};values=iter([initial,{**initial,key:value}])
                daemon=SimpleNamespace(proxy_status=lambda:next(values),proxy_check=lambda:self.fail('must_not_allocate_again'))
                result=wait_for_proxy_check(daemon,expected_check_id=11,expected_generation=7,expected_runtime_epoch=EPOCH)
                self.assertFalse(result['ok']);self.assertEqual(result['code'],code)

if __name__=='__main__':unittest.main()
