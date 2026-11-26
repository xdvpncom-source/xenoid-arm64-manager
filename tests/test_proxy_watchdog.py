"""Timer and route ordering branches with the actual Broker proxy method.

No Docker, Android, systemd, sockets, source changes, or external service calls.
"""
import contextlib
import copy
import importlib.util
import json
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'qa'))
import script_host as host
import qa_resource_runtime as q
CID='a'*64;EPOCH='b'*64

class Manager:
    def __init__(self,state):
        self.context=types.SimpleNamespace(instance_id='11111111-2222-4333-8444-555555555555',resource_tag='abc123def456',state_root=state)
        self.cfg=types.SimpleNamespace(cold_profile='fresh-bluejay')
        self.owned={'Id':CID,'State':{'Running':True,'StartedAt':'fixture-start'}}
        self.status={'ok':True,'stateReadable':True,'enabled':True,'configured':True,'quarantined':False,'mustBlock':True,
                     'generation':1,'runtimeEpoch':EPOCH,'report':{'phase':'active','structuralApplied':True,'dataPlaneVerified':True}}
        self.engine={'ok':True,'phase':'active','structuralApplied':True,'quarantined':False,'generation':1,'runtimeEpoch':EPOCH}
        self.bootstrap_epoch=EPOCH;self.events=[];self.offline=False;self.bootstrap_fails=False;self.status_unreadable=False
    def _owned_container_record(self,**kwargs):return self.owned,None
    def instance_operation_lock(self,**kwargs):return contextlib.nullcontext()
    def daemon_client(self,**kwargs):return types.SimpleNamespace(proxy_status=self.proxy_status)
    def proxy_status(self,**kwargs):
        if self.status_unreadable:raise RuntimeError('fixture_transport_unavailable')
        return copy.deepcopy(self.status)
    def proxy_engine_status(self):self.events.append('host_engine_read');return copy.deepcopy(self.engine)
    def reconcile_bootstrap(self,**kwargs):
        self.events.append(('official_bootstrap',kwargs.get('timeout')))
        if self.bootstrap_fails:return {'transportReady':True,'controlReady':False,'code':'bootstrap_timeout'}
        self.bootstrap_epoch=EPOCH;self.status_unreadable=False;return {'transportReady':True,'controlReady':True,'ok':True}
    def reconcile_proxy_desired(self):
        self.events.append('official_proxy_reconcile')
        if not self.bootstrap_epoch:return {'ok':False,'code':'instance_identity_mismatch'}
        if self.offline:return {'ok':False,'code':'proxy_outbound_unavailable'}
        self.engine.update(ok=True,phase='active',structuralApplied=True,quarantined=False,generation=1,runtimeEpoch=EPOCH)
        self.status.update(quarantined=False,runtimeEpoch=EPOCH,report={'phase':'active','structuralApplied':True,'dataPlaneVerified':True})
        return {'ok':True}

class Broker:
    # Run the actual current production method, rather than a fake watchdog.
    display_proxy_reconcile=host.Broker.display_proxy_reconcile
    def __init__(self,manager):self.manager=manager;self.container_id=CID;self.log=None;self.stopping=threading.Event()
    def owned_manager(self):return contextlib.nullcontext(self.manager)
    def display_raw_docker(self,*args,**kwargs):return contextlib.nullcontext()

class Watchdog(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.manager=Manager(self.root);self.broker=Broker(self.manager)
        self.config=q.validate_config({'fps':60,'scale':None,'dpi':420,'render_fps':15,'timeout':0})
        self.identity={'instance_id':self.manager.context.instance_id,'resource_tag':self.manager.context.resource_tag}
        self.previous={'instance':'phone-1','ok':True,'identity':self.identity,
                       'key':q.resource_key(self.manager.owned,self.config,self.identity),
                       'display':{'verified_sentinel':True},'metrics':{'unchanged_sentinel':True}}
        fresh=types.ModuleType('qa_fresh6a');fresh.is_bluejay=lambda _:True;fresh.ensure_runtime=Mock(return_value={'ok':True,'changed':False})
        self.fresh=fresh
        route=types.ModuleType('qa_control_routes');route.ensure_control_routes=Mock(return_value={'ok':True,'changed':False,'ipv6_changed':False});self.route=route
        util=types.ModuleType('xenoid.util');util.command_timeout=lambda seconds:contextlib.nullcontext()
        self.patches=[patch.dict(sys.modules,{'qa_fresh6a':fresh,'qa_control_routes':route,'xenoid.util':util}),patch.object(q,'STATE',self.root),
                      patch.object(q,'pending_profile',return_value=False),patch.object(q,'effective_config',side_effect=lambda c,*a:c),
                      patch.object(q,'ensure_host_vm_guard',return_value={'ok':True}),patch.object(q,'ensure_memory_headroom',return_value={'ok':True}),
                      patch.object(q,'resource_identity',return_value=self.identity)]
        for p in self.patches:p.start();self.addCleanup(p.stop)
        q.atomic_json(self.root/'phone-1.json',self.previous)
    def timer(self,**kwargs):return q.reconcile_owned('phone-1',self.config,self.manager,broker=self.broker,**kwargs)
    def test_healthy_same_boot_reads_actual_host_without_reconcile_or_graphics(self):
        value=self.timer();self.assertTrue(value['ok']);self.assertFalse(value['proxy_pending']);self.assertTrue(value['proxy_maintenance_only'])
        self.assertIn('host_engine_read',self.manager.events);self.assertNotIn('official_proxy_reconcile',self.manager.events)
        self.assertEqual(value['display'],self.previous['display']);self.assertEqual(value['metrics'],self.previous['metrics']);self.fresh.ensure_runtime.assert_not_called()
    def test_dead_agent_stale_daemon_report_invokes_existing_official_reconcile(self):
        self.manager.engine={'ok':False,'error':'engine_process_failed'}
        value=self.timer();self.assertTrue(value['ok']);self.assertFalse(value['proxy_pending']);self.assertIn('official_proxy_reconcile',self.manager.events)
        self.assertEqual(value['display'],self.previous['display']);self.fresh.ensure_runtime.assert_not_called()
    def test_missing_runtime_epoch_full_official_bootstrap_precedes_proxy_reconcile(self):
        self.manager.status.update(runtimeEpoch='',quarantined=True,report=None);self.manager.bootstrap_epoch='';self.manager.engine={'ok':False}
        value=self.timer();self.assertTrue(value['ok']);self.assertFalse(value['proxy_pending'])
        names=[e[0]if isinstance(e,tuple)else e for e in self.manager.events]
        self.assertLess(names.index('official_bootstrap'),names.index('official_proxy_reconcile'))
        bootstrap=next(e for e in self.manager.events if isinstance(e,tuple));self.assertGreater(bootstrap[1],170)
    def test_disabled_or_unconfigured_never_enables_or_bootstraps(self):
        for field in('enabled','configured'):
            self.manager.events=[];self.manager.status[field]=False;value=self.timer()
            self.assertTrue(value['ok']);self.assertFalse(value['proxy_pending']);self.assertEqual(value['proxy_watchdog']['skipped'],'disabled_or_unconfigured');self.assertEqual(self.manager.events,[])
            self.manager.status[field]=True
    def test_upstream_offline_is_pending_but_graphics_and_normal_resource_ok(self):
        self.manager.status.update(quarantined=True);self.manager.engine={'ok':False};self.manager.offline=True
        value=self.timer();self.assertTrue(value['ok']);self.assertTrue(value['resource_ok']);self.assertTrue(value['proxy_pending'])
        self.assertEqual(value['proxy_watchdog']['code'],'proxy_outbound_unavailable');self.assertEqual(value['display'],self.previous['display']);self.fresh.ensure_runtime.assert_not_called()
        value=self.timer();self.assertTrue(value['proxy_pending']);self.assertEqual(self.manager.events.count('official_proxy_reconcile'),2)
    def test_unreadable_daemon_status_uses_official_bootstrap_then_rechecks(self):
        self.manager.status_unreadable=True
        value=self.timer();self.assertTrue(value['ok']);self.assertFalse(value['proxy_pending'])
        self.assertEqual(self.manager.events[0][0],'official_bootstrap')
    def test_timer_manager_adopts_factory_environment_before_native_resolution(self):
        events=[];cfg=types.ModuleType('xenoid.config');backend=types.ModuleType('xenoid.backend')
        cfg.resolve_instance=lambda *a,**k:(events.append('resolve') or ('context','settings','lease'))
        backend.RuntimeManager=lambda *a:(events.append('manager') or 'manager')
        with patch.dict(sys.modules,{'xenoid.config':cfg,'xenoid.backend':backend}), patch.object(host,'adopt_native_environment',side_effect=lambda:events.append('factory_env')), patch.object(q.os,'chdir'), patch.object(q.sys,'path',list(sys.path)):
            self.assertEqual(q.native_manager('phone-1'),'manager')
        self.assertEqual(events,['factory_env','resolve','manager'])
    def test_failed_bootstrap_is_nonfatal_network_pending_without_lower_reconcile(self):
        self.manager.status['runtimeEpoch']='';self.manager.bootstrap_fails=True
        value=self.timer();self.assertTrue(value['ok']);self.assertTrue(value['proxy_pending']);self.assertNotIn('official_proxy_reconcile',self.manager.events)
    def test_profile_or_native_GEN_pending_defers_before_any_healing(self):
        with patch.object(q,'pending_profile',return_value=True):self.assertTrue(self.timer()['deferred'])
        for name in('device-regenerate-v3.json','convergence-v1.json'):
            p=self.root/name;p.write_text('{}');value=self.timer();p.unlink();self.assertTrue(value['deferred']);self.assertEqual(value['skipped'],'native_transaction_pending')
        self.assertEqual(self.manager.events,[]);self.fresh.ensure_runtime.assert_not_called()
    def test_stopped_runtime_never_starts_or_heals(self):
        self.manager.owned['State']['Running']=False
        with patch.object(q,'native_manager',return_value=self.manager):self.assertEqual(q.reconcile('phone-1',self.config)['skipped'],'stopped')
        self.assertEqual(self.manager.events,[])
    def test_SDK_force_true_preserves_existing_readback_without_network_healing(self):
        value=self.timer(force=True);self.assertTrue(value['ok']);self.fresh.ensure_runtime.assert_called_once();self.assertEqual(self.manager.events,[])
    def test_changed_owned_CID_is_not_masked_as_network_pending(self):
        self.manager.owned={**self.manager.owned,'Id':'c'*64}
        with self.assertRaises(host.RuntimeIdentityError):q.native_proxy_maintenance(self.manager,{'Id':CID},self.config,self.previous,broker=self.broker)
    def test_budget_default_validation_and_unchanged_graphics_key(self):
        self.assertEqual(self.config['proxy_maintenance_timeout'],180)
        for bad in(-1,True,None,'180',1.5):
            with self.assertRaises(ValueError):q.validate_config({'proxy_maintenance_timeout':bad})
        self.assertEqual(q.validate_config({'proxy_maintenance_timeout':0})['proxy_maintenance_timeout'],0)
        old={k:v for k,v in self.config.items()if k!='proxy_maintenance_timeout'}
        self.assertEqual(q.resource_key(self.manager.owned,old,self.identity),q.resource_key(self.manager.owned,{**self.config,'proxy_maintenance_timeout':240},self.identity))
    def test_zero_maintenance_budget_preserves_native_default_bounds_without_resource_cap(self):
        self.config.update(proxy_maintenance_timeout=0,timeout=1)
        self.manager.status.update(runtimeEpoch='',quarantined=True,report=None);self.manager.bootstrap_epoch=''
        value=q.native_proxy_maintenance(self.manager,self.manager.owned,self.config,self.previous,broker=self.broker)
        self.assertTrue(value['ok']);self.assertFalse(value['proxy_pending'])
        self.assertIn(('official_bootstrap',None),self.manager.events)
    def test_control_route_precedes_initial_FPS_and_timer_UI_reads(self):
        order=[];self.route.ensure_control_routes.side_effect=lambda *a,**k:(order.append('route')or{'ok':True})
        self.fresh.ensure_runtime.side_effect=lambda *a,**k:(order.append('fps')or{'ok':True})
        self.timer(force=True);self.assertEqual(order,['route','fps'])
        self.assertEqual(self.route.ensure_control_routes.call_args.args[1],CID)
        order.clear();self.timer();self.assertEqual(order,['route'])
    def test_GEN_defer_also_precedes_initial_force_true_route_and_FPS(self):
        (self.root/'convergence-v1.json').write_text('{}')
        self.assertTrue(self.timer(force=True)['deferred']);self.route.ensure_control_routes.assert_not_called();self.fresh.ensure_runtime.assert_not_called()
    def test_legacy_same_boot_retains_existing_early_return(self):
        self.fresh.is_bluejay=lambda _:False;self.config=q.validate_config({});self.previous['key']=q.resource_key(self.manager.owned,self.config,self.identity);q.atomic_json(self.root/'phone-1.json',self.previous)
        value=self.timer();self.assertEqual(value['skipped'],'already_applied');self.assertEqual(self.manager.events,[]);self.assertNotIn('proxy_watchdog',value)

if __name__=='__main__':unittest.main()
