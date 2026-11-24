"""Actual guarded filesystem writes: no Docker, network, guest or sysctl changes."""
import contextlib,copy,signal,importlib.util,json,os,tempfile,types,unittest
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('headroom_runtime',ROOT/'qa/qa_resource_runtime.py');q=importlib.util.module_from_spec(spec);spec.loader.exec_module(q)
CID='a'*64;IID='11111111-2222-4333-8444-555555555555';TAG='abc123def456'

class Headroom(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.proc=self.root/'proc';self.proc.mkdir();p=self.proc/'123';p.mkdir();(p/'stat').write_text('123 (init) '+' '.join(['S']+['0']*18+['67890'])+'\n');(p/'cgroup').write_text('0::/system.slice/docker-'+CID+'.scope\n')
        self.cg=self.root/'cgroup';self.folder=self.cg/'system.slice'/('docker-'+CID+'.scope');self.folder.mkdir(parents=True)
        for k,v in {'memory.current':'1700000000','memory.max':str(q.MAX_BYTES),'memory.high':'max','memory.swap.max':str(q.SWAP_BYTES),'memory.events':'oom 0\noom_kill 0','memory.pressure':'some avg10=0.00','cpu.max':'100000 100000'}.items():(self.folder/k).write_text(v+'\n')
        self.owned={'Id':CID,'Image':'sha256:'+'b'*64,'State':{'Running':True,'Pid':123,'StartedAt':'2026-01-01T00:00:00Z'},'Config':{'Labels':{'dev.xenoid.owner':'xenoid','dev.xenoid.instance_id':IID,'dev.xenoid.resource_tag':TAG}},'HostConfig':{'NanoCpus':1000000000,'Memory':q.MAX_BYTES,'MemorySwap':q.MAX_BYTES+q.SWAP_BYTES}}
        self.manager=types.SimpleNamespace(context=types.SimpleNamespace(instance_id=IID,resource_tag=TAG),lease=types.SimpleNamespace(resource_tag=TAG),_owned_container_record=lambda **kw:(self.owned,None))
        self.state=self.root/'state'
        self.kernel_fs=patch.object(q,'_memory_filesystem_type',return_value=0x63677270);self.kernel_fs.start();self.addCleanup(self.kernel_fs.stop)
        # Kernel cgroup files replace one scalar on each write. An ordinary
        # temporary file needs a test-only truncation to emulate that ABI.
        real_write=os.write
        def cgroup_write(fd,body):
            result=real_write(fd,body);os.ftruncate(fd,len(body));return result
        self.writer=patch.object(q,'_memory_write',side_effect=lambda fd,body,record,timeout:cgroup_write(fd,body));self.writer.start();self.addCleanup(self.writer.stop)
    def apply(self,**kw):return q.ensure_memory_headroom(self.manager,CID,reserve_mib=kw.pop('reserve_mib',256),proc_root=self.proc,cgroup_root=self.cg,state_root=self.state,**kw)
    def test_actual_write_readback_receipt_and_rollback(self):
        caps=[(self.folder/n).read_bytes()for n in('memory.max','memory.swap.max','cpu.max')]
        self.assertTrue(self.apply()['changed']);self.assertEqual((self.folder/'memory.high').read_text(),str(q.HIGH_BYTES)+'\n');r=json.loads((self.state/(CID+'.json')).read_text());self.assertEqual(r['original_high'],'max');self.assertEqual(r['phase'],'applied');self.assertEqual(os.stat(self.state/(CID+'.json')).st_mode&0o777,0o600)
        self.assertFalse(self.apply()['changed']);self.apply(operation='rollback');self.assertEqual((self.folder/'memory.high').read_text(),'max\n');self.assertEqual(caps,[(self.folder/n).read_bytes()for n in('memory.max','memory.swap.max','cpu.max')])
    def test_unowned_admin_high_rejected_without_receipt(self):
        (self.folder/'memory.high').write_text('1800000000\n')
        with self.assertRaisesRegex(RuntimeError,'existing_admin'):self.apply()
        self.assertFalse(self.state.exists())
    def test_changed_pid_and_caps_reject_before_mutation(self):
        for key,value in(('Memory',q.MAX_BYTES+1),('MemorySwap',q.MAX_BYTES),('NanoCpus',2_000_000_000)):
            old=self.owned['HostConfig'][key];self.owned['HostConfig'][key]=value
            with self.assertRaisesRegex(RuntimeError,'qualified_caps'):self.apply()
            self.owned['HostConfig'][key]=old
        calls=0
        def changed(**kw):
            nonlocal calls
            calls+=1;v=copy.deepcopy(self.owned)
            if calls>1:v['State']['StartedAt']='different'
            return v,None
        self.manager._owned_container_record=changed
        with self.assertRaisesRegex(RuntimeError,'owner_changed_before_write'):self.apply()
        self.assertEqual((self.folder/'memory.high').read_text(),'max\n')
    def test_pending_before_write_resumes_same_intent(self):
        self.apply();r=json.loads((self.state/(CID+'.json')).read_text());r.update(desired_high=str(q.MAX_BYTES-128*1024**2),previous_high=str(q.HIGH_BYTES),reserve_mib=128,phase='pending');q.atomic_json(self.state/(CID+'.json'),r)
        self.assertTrue(self.apply(reserve_mib=128)['changed'])
    def test_symlink_and_wrong_cgroup_reject(self):
        original=self.folder/'memory.high';original.unlink();original.symlink_to(self.root/'outside');(self.root/'outside').write_text('max\n')
        with self.assertRaises(OSError):self.apply()
        self.assertEqual((self.root/'outside').read_text(),'max\n')
    def test_policy_reserve_validation(self):
        fresh={'fps':60,'scale':None,'dpi':420,'render_fps':15}
        self.assertEqual(q.validate_config(fresh)['memory_reserve_mib'],256)
        self.assertEqual(q.validate_config({})['memory_reserve_mib'],0)
        for value in(True,-1,1025,None,'256'):
            with self.assertRaises(ValueError):q.validate_config({**fresh,'memory_reserve_mib':value})
    @unittest.skipUnless(Path('/proc/self/stat').exists() and hasattr(os,'fork'),'Linux fork/proc required')
    def test_actual_writer_deadline_has_no_late_write(self):
        self.writer.stop()
        import time
        fd=os.open(self.root/'blocked-writer',os.O_RDWR|os.O_CREAT,0o600)
        original=os.write;pid_record=[]
        def delayed(descriptor,body):
            if descriptor==fd:time.sleep(1)
            return original(descriptor,body)
        try:
            with patch.object(q.os,'write',side_effect=delayed):
                with self.assertRaisesRegex(RuntimeError,'timed_out_child_reaped'):
                    q._memory_write(fd,b'candidate',pid_record.append,0.05)
            time.sleep(0.1)
            self.assertEqual((self.root/'blocked-writer').read_bytes(),b'')
            with self.assertRaises(ChildProcessError):os.waitpid(pid_record[0]['pid'],os.WNOHANG)
        finally:os.close(fd)

    def test_explicit_legacy_policy_defaults_to_zero(self):
        legacy={"enabled":True,"fps":20,"scale":"minimum","dpi":72,"min_side":200,"render_fps":None}
        self.assertEqual(q.validate_config(legacy)['memory_reserve_mib'],0)

    def test_real_fleet_warm_up_keeps_native_command_and_reconciles(self):
        import runpy
        fleet=runpy.run_path(str(ROOT/'xenoid-fleet'),run_name='headroom_test_fleet')
        run=fleet['run_devices'];g=run.__globals__;upstream=types.SimpleNamespace(call=lambda command,**kw:types.SimpleNamespace(returncode=0))
        calls=[];native=[]
        upstream.call=lambda command,**kw:(native.append((command,kw))or types.SimpleNamespace(returncode=0))
        with patch.dict(g,device_lock=lambda _id:contextlib.nullcontext(),
                        qa_reconcile=lambda ids,**kw:calls.append((ids,kw))):
            with patch.object(g['ChromeBaseline'],'load',return_value=None),patch.object(g['ProxyDefault'],'load',return_value=None):
                self.assertEqual(run(upstream,['1'],['up']),0)
        self.assertEqual(calls,[(['1'],{'force':True})]);self.assertEqual(native,[(['up'],{'device_id':'1','interactive':True})])

    def test_disabled_admin_caps_preserve_existing_display_path(self):
        self.owned['HostConfig'].update(NanoCpus=2_000_000_000,Memory=4*1024**3,MemorySwap=4*1024**3+q.SWAP_BYTES)
        with patch.object(q,'_memory_record')as store:
            result=self.apply(reserve_mib=0)
            self.assertEqual(result['skipped'],'disabled_no_owned_receipt');store.assert_not_called()
        with self.assertRaisesRegex(RuntimeError,'qualified_caps'):self.apply(reserve_mib=256)
        config=q.validate_config({'fps':60,'render_fps':15,'scale':None,'dpi':420,'memory_reserve_mib':0})
        state=self.root/'resource-marker';state.mkdir();q.atomic_json(state/'phone-1.json',{'key':q.resource_key(self.owned,config,q.resource_identity(self.manager))})
        native_binding=q._memory_binding
        with patch.object(q,'_memory_binding',side_effect=lambda manager,cid,proc,**kw:native_binding(manager,cid,self.proc,**kw)),patch.object(q,'STATE',state),patch.object(q,'pending_profile',return_value=False),patch.object(q,'effective_config',side_effect=lambda c,*_:c),patch.object(q,'ensure_host_vm_guard',return_value={'ok':True}),patch.dict(__import__('sys').modules,{'qa_fresh6a':types.SimpleNamespace(is_bluejay=lambda _:False)}):
            self.assertEqual(q.reconcile_owned('phone-1',config,self.manager)['skipped'],'already_applied')

    def test_entire_cleanup_reaps_after_interruption(self):
        self.writer.stop()
        writer_stat='987654 (writer) '+' '.join(['0']*19+['10'])
        pidfd=os.open(os.devnull,os.O_RDONLY)
        with patch.object(q.os,'pidfd_open',return_value=pidfd,create=True),patch.object(signal,'pidfd_send_signal',create=True),patch.object(signal,'getsignal',return_value=signal.SIG_DFL),patch.object(q.os,'fork',return_value=987654),patch.object(q.Path,'read_text',return_value=writer_stat),patch.object(q.os,'waitpid',side_effect=[KeyboardInterrupt('cancel in cleanup'),(987654,0)])as wait,patch.object(q.os,'kill')as kill:
            with self.assertRaisesRegex(RuntimeError,'record sink failure'):
                q._memory_write(-1,b'x',lambda _r:(_ for _ in()).throw(RuntimeError('record sink failure')),0.1)
            self.assertEqual(wait.call_count,2)

    def test_non_cgroup_filesystem_cannot_receive_high_write(self):
        with patch.object(q,'_memory_filesystem_type',return_value=0x1234):
            with self.assertRaisesRegex(RuntimeError,'cgroup_directory_invalid'):self.apply()
        self.assertFalse(self.state.exists());self.assertEqual((self.folder/'memory.high').read_text(),'max\n')

    def test_post_timeout_blocking_reap_handles_already_reaped_child(self):
        self.writer.stop()
        import time
        pidfd=os.open(os.devnull,os.O_RDONLY);writer_stat='987654 (writer) '+' '.join(['0']*19+['10'])
        with patch.object(q.os,'pidfd_open',return_value=pidfd,create=True),patch.object(signal,'pidfd_send_signal',create=True)as kill,patch.object(signal,'getsignal',return_value=signal.SIG_DFL),patch.object(q.os,'fork',return_value=987654),patch.object(q.Path,'read_text',return_value=writer_stat),patch.object(q.os,'write',return_value=1),patch.object(time,'monotonic',side_effect=[0,1]),patch.object(q.os,'waitpid',side_effect=[KeyboardInterrupt('after reap'),ChildProcessError('already reaped')])as wait:
            with self.assertRaisesRegex(RuntimeError,'timed_out_child_reaped'):
                q._memory_write(-1,b'x',lambda _:None,0.1)
            self.assertEqual(wait.call_count,2);kill.assert_called_once_with(pidfd,signal.SIGKILL,None,0)
    def test_sigchld_custom_reaper_rejected_before_fork(self):
        self.writer.stop()
        with patch.object(q.os,'pidfd_open',create=True),patch.object(signal,'pidfd_send_signal',create=True),patch.object(signal,'getsignal',return_value=signal.SIG_IGN),patch.object(q.os,'fork')as fork:
            with self.assertRaisesRegex(RuntimeError,'exact_child_control_unsupported'):q._memory_write(-1,b'x',lambda _:None,1)
            fork.assert_not_called()

    def test_fork_failure_closes_both_pipe_fds(self):
        self.writer.stop();created=[];real_pipe=os.pipe
        def pipe():
            pair=real_pipe();created.extend(pair);return pair
        with patch.object(q.os,'pidfd_open',create=True),patch.object(signal,'pidfd_send_signal',create=True),patch.object(signal,'getsignal',return_value=signal.SIG_DFL),patch.object(q.os,'pipe',side_effect=pipe),patch.object(q.os,'fork',side_effect=OSError(11,'fork unavailable')):
            with self.assertRaisesRegex(OSError,'fork unavailable'):q._memory_write(-1,b'x',lambda _:None,1)
        for fd in created:
            with self.assertRaises(OSError):os.fstat(fd)
    def test_parent_setup_close_interruption_reaps_child_and_closes_pipes(self):
        self.writer.stop();created=[];real_pipe=os.pipe;real_close=os.close;interrupted=False
        def pipe():
            pair=real_pipe();created.extend(pair);return pair
        def close(fd):
            nonlocal interrupted
            if created and fd==created[0] and not interrupted:
                interrupted=True;raise KeyboardInterrupt('cancel parent setup')
            return real_close(fd)
        with patch.object(q.os,'pidfd_open',create=True),patch.object(signal,'pidfd_send_signal',create=True),patch.object(signal,'getsignal',return_value=signal.SIG_DFL),patch.object(q.os,'pipe',side_effect=pipe),patch.object(q.os,'fork',return_value=987654),patch.object(q.os,'close',side_effect=close),patch.object(q.os,'waitpid',return_value=(987654,0))as wait:
            with self.assertRaisesRegex(KeyboardInterrupt,'cancel parent setup'):q._memory_write(-1,b'x',lambda _:None,1)
            wait.assert_called_once_with(987654,os.WNOHANG)
        for fd in created:
            with self.assertRaises(OSError):os.fstat(fd)

    def test_owning_policy_hook_and_disabled_skip(self):
        with patch.object(q,'CONFIG',self.root/'settings.json'),patch.object(q,'load_config',return_value=q.validate_config({'enabled':False})),patch.object(q,'ensure_memory_headroom')as apply:
            q.CONFIG.write_text('{}');self.assertIn('skipped',q.ensure_configured_memory_headroom(self.manager,CID));apply.assert_not_called()

if __name__=='__main__':unittest.main()
