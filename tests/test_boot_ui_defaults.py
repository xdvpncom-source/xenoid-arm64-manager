"""Run the actual candidate methods with owned-container/command fixtures."""
import ast,contextlib,hashlib,json,math,re,shlex,threading,types,unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any,Optional
HERE=Path(__file__).resolve().parents[1];SOURCE=HERE/'nativebundle/source/src/xenoid/backend.py';TREE=ast.parse(SOURCE.read_bytes());CLASS=next(n for n in TREE.body if isinstance(n,ast.ClassDef)and n.name=='RuntimeManager')
CID='a'*64
class Clock:
    def __init__(self):self.now=0
    def monotonic(self):return self.now
    def sleep(self,value):self.now+=value
def methods(clock,run):
    ns={'Any':Any,'Optional':Optional,'Mapping':Mapping,'math':math,'re':re,'time':clock,'json':json,'shlex':shlex,'run':run,'bounded_timeout':lambda n:n,'_DAEMON_START_TRANSPORT_TIMEOUT_SECONDS':120,'_SHA256_PATTERN':re.compile('[0-9a-f]{64}'),'_SOFT_REBOOT_TIMEOUT_SECONDS':300}
    for name in('_ensure_initial_boot_ui_defaults','start_owned_container','soft_reboot'):
        node=next(n for n in CLASS.body if isinstance(n,ast.FunctionDef)and n.name==name);exec(compile(ast.Module(body=[node],type_ignores=[]),str(SOURCE),'exec'),ns)
    return ns
class Owner:
    def __init__(self,responses=(),profile=True):
        self.cfg=types.SimpleNamespace(cold_profile={}if profile else None);self.responses=list(responses);self.commands=[];self.lookups=0;self.wrong_on=None;self.cancellation_event=None
    def _owned_container_record(self,**kw):
        self.lookups+=1;return {'Id':'b'*64 if self.lookups==self.wrong_on else CID,'Image':'sha256:fixed','State':{'Pid':123,'Running':True,'StartedAt':'fixed'}},None
    def docker_base_cmd(self):return['docker','--context','owned']
    def docker_env(self):return{}
    def run(self,argv,**kw):
        self.commands.append(argv);assert argv[:5]==['docker','--context','owned','exec',CID];assert argv[5:10]==['/system/bin/cmd','settings','--user','0',argv[9]]
        value=self.responses.pop(0)
        if isinstance(value,Exception):raise value
        return types.SimpleNamespace(returncode=value[0],stdout=value[1],stderr='')
class BootUiTests(unittest.TestCase):
    def call(self,owner,timeout=10,parent=None):
        clock=Clock();ns=methods(clock,owner.run)
        if parent is not None:ns['bounded_timeout']=lambda n:min(n,parent)
        return ns['_ensure_initial_boot_ui_defaults'](owner,CID,timeout=timeout),clock
    def test_provider_unavailable_then_ready_write_readback(self):
        m=Owner([(1,''),(0,'null\n'),(0,''),(0,'confirmed\n')]);r,c=self.call(m);self.assertTrue(r['ok']);self.assertTrue(r['changed']);self.assertEqual(r['attempts'],2);self.assertEqual([a[9]for a in m.commands],['get','get','put','get']);self.assertEqual(m.commands[2][-3:],['secure','immersive_mode_confirmations','confirmed']);self.assertEqual(c.now,1)
    def test_already_confirmed_is_one_get_no_write(self):
        m=Owner([(0,'confirmed\n')]);r,_=self.call(m);self.assertTrue(r['ok']);self.assertFalse(r['changed']);self.assertEqual(len(m.commands),1)
    def test_late_provider_obeys_parent_without_30_or_120_second_clamp(self):
        for delay in(35,125):
            m=Owner([(1,'')]*delay+[(0,'null'),(0,''),(0,'confirmed')]);r,c=self.call(m,timeout=180,parent=150);self.assertTrue(r['ok']);self.assertEqual(c.now,delay);self.assertEqual(r['attempts'],delay+1)
    def test_expected_CID_fence_before_write(self):
        m=Owner([(0,'null')]);m.wrong_on=3;r,_=self.call(m);self.assertEqual(r['error'],'convergence_state_conflict');self.assertEqual([a[9]for a in m.commands],['get'])
    def test_cancellation_before_any_command(self):
        m=Owner();m.cancellation_event=threading.Event();m.cancellation_event.set();r,_=self.call(m);self.assertEqual(r['error'],'boot_ui_defaults_cancelled');self.assertEqual(m.commands,[])
    def test_parent_deadline_and_invalid_budget(self):
        m=Owner([(1,'')]);r,c=self.call(m,parent=.05);self.assertFalse(r['ok']);self.assertLessEqual(c.now,.05);self.assertEqual(len(m.commands),1)
        for value in(True,0,-1,float('inf'),float('nan')):
            m=Owner();r,_=self.call(m,timeout=value);self.assertEqual(r['error'],'boot_ui_defaults_budget_invalid');self.assertEqual(m.commands,[])
    def test_unexpected_value_never_overwritten(self):
        m=Owner([(0,'unrecognized')]);r,_=self.call(m);self.assertEqual(r['error'],'boot_ui_defaults_unexpected_value');self.assertEqual(len(m.commands),1)
    def test_write_and_readback_fail_closed(self):
        for responses,error in([[(0,'null'),(1,'')],'boot_ui_defaults_write_failed'],[[(0,'null'),(0,''),(0,'null')],'boot_ui_defaults_readback_failed'],[[(0,'null'),TimeoutError()],'boot_ui_defaults_write_unverified']):
            m=Owner(responses);r,_=self.call(m);self.assertEqual(r['error'],error)
    def test_normal_profile_none_has_no_commands(self):
        m=Owner(profile=False);r,_=self.call(m);self.assertEqual(r,{'ok':True,'skipped':True});self.assertEqual(m.commands,[])
    def test_real_start_orders_hook_before_boot_wait_and_skips_nonwait_legacy(self):
        for profile,wait,cold,expected in((True,True,True,['dockerStart','statfs','ui','boot']),(True,True,False,['statfs','boot']),(False,True,False,['statfs','boot']),(True,False,False,['statfs'])):
            m=Owner(profile=profile);m.lease=types.SimpleNamespace(android_adb_port=5555);events=[];m.ensure_instance_lease=lambda:None;m._container_matches_lease=lambda *a,**k:True;m._qa_host_vm_isolation=lambda *a:{'ok':True};m._republish_statfs_fsid_after_start=lambda *a:events.append('statfs');m._ensure_initial_boot_ui_defaults=lambda *a,**k:(events.append('ui')or{'ok':True});m.docker_wait_boot=lambda **k:(events.append('boot')or{'ok':True})
            if cold:
                m.running=False
                def owned(**kw):return {'Id':CID,'Image':'sha256:fixed','State':{'Running':m.running,'Pid':123}},None
                m._owned_container_record=owned;m.proxy_bootstrap_quarantine=lambda:{'ok':True};m._shared_protection_engine_lock=contextlib.nullcontext;m.shared_protection_manager=lambda:types.SimpleNamespace(status=lambda:{'ok':True})
                def start(*a,**kw):m.running=True;events.append('dockerStart');return types.SimpleNamespace(returncode=0,stderr='')
                m._shared_protection_engine_shell=start
            for name in('ensure_adb_authorized_key','adb_connect','adb_wait','adb_disconnect','forward_daemon_port'):setattr(m,name,lambda **kw:{'ok':True})
            ns=methods(Clock(),m.run);ns['StorageStateStore']=lambda *a:types.SimpleNamespace(load=lambda:None);ns['StorageError']=RuntimeError;m.context=object();r=ns['start_owned_container'](m,expected_container_id=CID,wait=wait);self.assertTrue(r['ok']);self.assertEqual(events,expected)
    def test_real_soft_reboot_prepares_before_stop_and_resume_skips(self):
        for profile,resume,ready in((True,False,True),(False,False,True),(True,True,True),(True,False,False)):
            m=Owner(profile=profile);clock=Clock();ns=methods(clock,m.run);events=[];m.daemon_client=lambda **kw:types.SimpleNamespace(read_private_token=lambda **kw:'token');m.ensure_rootd_root=lambda *a:{'ok':True};m._ensure_initial_boot_ui_defaults=lambda *a,**kw:(events.append('ui')or{'ok':ready,'error':'fixture_ui_guard'});m._rootd_exec=lambda command,*a,**kw:(events.append('stop')or None)
            def output(args,**kw):
                if args==['getprop','sys.boot_completed']:return'1'
                if args[0]=='pidof':return'123'
                return'stopped'
            m._rootd_output=output;r=ns['soft_reboot'](m,timeout=1,userspace_restart_observed=resume)
            self.assertFalse(r['ok']);self.assertEqual(events,[]if resume else(['ui','stop']if profile and ready else['ui']if profile else['stop']))
            if profile and not ready:self.assertEqual(r['error'],'fixture_ui_guard')
if __name__=='__main__':unittest.main()
