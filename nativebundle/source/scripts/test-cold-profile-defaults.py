#!/usr/bin/env python3
"""Default profile compatibility and first-create checkpoint contracts."""
from __future__ import annotations
import ast
from dataclasses import asdict
import importlib.util
import json
import os
import shlex
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from xenoid import artifacts, backend, config
from xenoid.cold_profile import apply_boot_command, expected_properties


def baseline_module(name, relative):
    text=subprocess.check_output(['git','show','HEAD:'+relative],cwd=ROOT,text=True)
    module=SimpleNamespace()
    namespace={'__name__':name,'__package__':'xenoid','__file__':str(ROOT/relative)}
    import types
    imported=types.ModuleType(name);imported.__dict__.update(namespace);sys.modules[name]=imported
    exec(compile(text,relative,'exec'),imported.__dict__)
    return imported

class DefaultContracts(unittest.TestCase):
    def test_serialized_default_and_template_do_not_gain_null_field(self):
        old=baseline_module('xenoid._cold_baseline_config','src/xenoid/config.py')
        values={'instance_name':'example','instance_id':'11111111-2222-4333-8444-555555555555'}
        before=old.XenoidConfig(**values);after=config.XenoidConfig(**values)
        self.assertEqual(config.config_dict(after),asdict(before))
        self.assertNotIn('cold_profile',config._clone_config_overrides(after))
    def test_default_artifact_recipes_and_consumer_closure_unchanged(self):
        old=baseline_module('xenoid._cold_baseline_artifacts','src/xenoid/artifacts.py')
        self.assertEqual(artifacts.CONSUMER_TARGETS['runtimeContext'],old.CONSUMER_TARGETS['runtimeContext'])
        for name,target in old.TARGETS.items():
            self.assertEqual(asdict(artifacts.TARGETS[name]),asdict(target),name)
    def test_legacy_config_instance_produces_identical_create_command(self):
        old_config=baseline_module('xenoid._cold_baseline_config_cmd','src/xenoid/config.py')
        text=subprocess.check_output(['git','show','HEAD:src/xenoid/backend.py'],cwd=ROOT,text=True)
        tree=ast.parse(text);methods=[x for node in tree.body if isinstance(node,ast.ClassDef) and node.name=='RuntimeManager' for x in node.body if isinstance(x,ast.FunctionDef) and x.name in ('_android_boot_command','docker_create_command')]
        ns={'apply_boot_command':apply_boot_command};exec(compile(ast.Module(methods,[]),'baseline','exec'),ns)
        cfg=old_config.XenoidConfig(instance_name='example',instance_id='11111111-2222-4333-8444-555555555555')
        owner=SimpleNamespace(cfg=cfg,lease=SimpleNamespace(android_adb_port=62111,host_adb_port=5555,container_name='android-example',volume_name='data-example',network_name='network-example',ipv4_address='172.31.0.10',ipv6_address='fd00::10',mac_address='02:00:00:00:00:01'),docker_base_cmd=lambda:['docker'],_owner_label_args=lambda:[],_google_label_args=lambda:[],_runtime_image_label_args=lambda:[],binder_volume_args=lambda:[],effective_image=lambda:'image:verified')
        owner._android_boot_command=lambda:ns['_android_boot_command'](owner)
        before=ns['docker_create_command'](owner)
        owner._android_boot_command=lambda:backend.RuntimeManager._android_boot_command(owner)
        after=backend.RuntimeManager.docker_create_command(owner)
        self.assertEqual(after,before)

class SeedOwnerContracts(unittest.TestCase):
    def test_boot_writer_requires_the_existing_pending_target(self):
        owner=SimpleNamespace(cfg=SimpleNamespace(cold_profile={}),context=SimpleNamespace(project_root=ROOT),lease=SimpleNamespace(volume_name='example',owner_labels={}),_committed_storage_uuids=lambda s:('a','b'))
        called=[];owner._engine_host_shell=lambda text,**kw:called.append(text) or SimpleNamespace(returncode=0,stdout='{"ok":true,"already":true}')
        state={'pending':{'bootId':'11111111-2222-4333-8444-555555555555'}}
        with patch.object(backend,'DeviceIdentityStore',return_value=SimpleNamespace(load=lambda:state)),patch.object(backend,'StorageStateStore',return_value=SimpleNamespace(load=lambda:{})):
            result=backend.RuntimeManager._run_boot_identity_seed(owner,'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee')
            self.assertEqual(result['error'],'device_boot_seed_target_mismatch');self.assertEqual(called,[])
            result=backend.RuntimeManager._run_boot_identity_seed(owner,state['pending']['bootId'])
            self.assertTrue(result['metadataPreserved']);self.assertTrue(result['skipped']);self.assertEqual(len(called),1)
    def test_legacy_boot_writer_command_is_unchanged(self):
        owner=SimpleNamespace(cfg=SimpleNamespace(),context=SimpleNamespace(project_root=ROOT),lease=SimpleNamespace(volume_name='example'),docker_env=lambda:{},remote_docker_ssh_cmd=lambda:None)
        with patch.object(backend,'run',return_value=SimpleNamespace(returncode=0,stdout='OK')) as called:
            self.assertTrue(backend.RuntimeManager._run_boot_identity_seed(owner,'boot')['ok'])
        self.assertEqual(called.call_args.args[0],[str(ROOT/'scripts/seed-boot-identity.sh'),'example','boot'])
    def test_expired_parent_deadline_never_starts_seed_helper(self):
        with patch.object(backend,'bounded_timeout',return_value=0),patch.object(backend,'run_bounded',side_effect=AssertionError('must not spawn')):
            self.assertEqual(backend.RuntimeManager._seed_fresh_profile_into_image(SimpleNamespace(),{},'a','b'),
                             {'ok':False,'error':'cold_profile_seed_timeout'})
    def owner(self, active=None, source='fresh', first=None):
        state={'active':active,'pendingStable':None,'stable':{'androidId':'0123456789abcdef','serial':'TEST000000000001','imei':'123456789012345','imeisv':'01'},'pending':None}
        store=SimpleNamespace(load=lambda:state,seed_next_boot=lambda:{'bootId':'11111111-2222-4333-8444-555555555555'})
        storage={'state':'committed','source':source}
        value=json.loads((ROOT/'examples/fingerprints/pixel-bluejay-android13.json').read_text())
        owner=SimpleNamespace(context=object(),lease=object(),cfg=SimpleNamespace(cold_profile=value),_committed_storage_uuids=lambda s:('a','b'),_seed_fresh_profile_into_image=lambda *a:first or {'ok':True,'profileSeeded':True},_run_boot_identity_seed=lambda boot:{'ok':True})
        return owner,store,storage
    def test_storage_rejection_precedes_pending_boot_mutation(self):
        owner,store,storage=self.owner(source='adopted')
        store.seed_next_boot=lambda:(_ for _ in ()).throw(AssertionError('must not mutate'))
        with patch.object(backend,'DeviceIdentityStore',return_value=store),patch.object(backend,'StorageStateStore',return_value=SimpleNamespace(load=lambda:storage)):
            self.assertEqual(backend.RuntimeManager._seed_boot_identity_into_image(owner)['error'],'cold_profile_fresh_storage_required')
    def test_second_create_uses_existing_boot_id_owner(self):
        owner,store,storage=self.owner(first={'ok':True,'alreadyInitialized':True})
        called=[];owner._run_boot_identity_seed=lambda boot:called.append(boot) or {'ok':True}
        with patch.object(backend,'DeviceIdentityStore',return_value=store),patch.object(backend,'StorageStateStore',return_value=SimpleNamespace(load=lambda:storage)):
            self.assertTrue(backend.RuntimeManager._seed_boot_identity_into_image(owner)['ok'])
        self.assertEqual(called,['11111111-2222-4333-8444-555555555555'])
    def test_initialize_replace_reuses_same_materialized_identity(self):
        owner,store,storage=self.owner()
        profiles=[];boots=[]
        def publish(profile,*uuids):
            profiles.append(profile)
            return {'ok':True,'profileSeeded':True} if len(profiles)==1 else {'ok':True,'alreadyInitialized':True}
        owner._seed_fresh_profile_into_image=publish
        owner._run_boot_identity_seed=lambda boot:boots.append(boot) or {'ok':True}
        with patch.object(backend,'DeviceIdentityStore',return_value=store),patch.object(backend,'StorageStateStore',return_value=SimpleNamespace(load=lambda:storage)):
            self.assertTrue(backend.RuntimeManager._seed_boot_identity_into_image(owner)['ok'])
            self.assertTrue(backend.RuntimeManager._seed_boot_identity_into_image(owner)['ok'])
        self.assertEqual(profiles[0],profiles[1])
        self.assertNotIn('REGENERATE',profiles[0]['ids'].values())
        self.assertEqual(boots,['11111111-2222-4333-8444-555555555555'])

class ObservationContracts(unittest.TestCase):
    def test_legacy_config_does_not_read_android(self):
        owner=SimpleNamespace(cfg=SimpleNamespace(),docker_exec=lambda *a,**k:(_ for _ in ()).throw(AssertionError('unexpected read')))
        self.assertEqual(backend.RuntimeManager.cold_profile_status(owner),{'ok':True,'skipped':True})
    def test_bluetooth_directory_shell_accepts_only_safe_native_modes(self):
        value=json.loads((ROOT/'examples/fingerprints/pixel-bluejay-android13.json').read_text())
        commands=[]
        owner=SimpleNamespace(cfg=SimpleNamespace(cold_profile=value),docker_exec=lambda args,**kw:commands.append(args[2]) or {'ok':False})
        backend.RuntimeManager.cold_profile_status(owner)
        command=commands[0].split('d=/data/local/tmp/xenoid-profile; ',1)[1]
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);directory=base/'profile';directory.mkdir()
            leaf=directory/'bluetooth_address';leaf.write_text('02:13:24:35:46:57\n');leaf.chmod(0o600)
            bindir=base/'bin';bindir.mkdir()
            stat_script=bindir/'stat';stat_script.write_text('#!'+sys.executable+'\nimport os,sys\ns=os.stat(sys.argv[-1]);fmt=sys.argv[2];mode=oct(s.st_mode&0o7777)[2:]\nprint(str(s.st_size) if fmt=="%s" else "0:0:"+mode+(":"+str(s.st_nlink) if fmt.endswith(":%h") else ""))\n');stat_script.chmod(0o755)
            getprop=bindir/'getprop';getprop.write_text('#!'+sys.executable+'\nimport sys\nprint("1" if sys.argv[1].endswith("_ready") else "02:13:24:35:46:57")\n');getprop.chmod(0o755)
            command='d='+shlex.quote(str(directory))+'; '+command
            env={**os.environ,'PATH':str(bindir)+':'+os.environ['PATH']}
            for mode,accepted in ((0o700,True),(0o755,True),(0o770,False),(0o777,False),(0o1755,False)):
                with self.subTest(mode=oct(mode)):
                    directory.chmod(mode)
                    result=subprocess.run(['sh','-c',command],env=env,text=True,capture_output=True,check=True)
                    self.assertIn('sidecar-state\t'+('valid' if accepted else 'invalid'),result.stdout)
            directory.chmod(0o700);outside=base/'outside';directory.rename(outside);directory.symlink_to(outside,target_is_directory=True)
            result=subprocess.run(['sh','-c',command],env=env,text=True,capture_output=True,check=True)
            self.assertIn('sidecar-state\tinvalid',result.stdout)

    def test_profile_observation_matches_selected_contract(self):
        value=json.loads((ROOT/'examples/fingerprints/pixel-bluejay-android13.json').read_text())
        props=expected_properties(value)
        text='\n'.join('['+k+']: [string]' for k in props)
        text+='\n__PROP_VALUES__\n'+'\n'.join('['+k+']: ['+v+']' for k,v in props.items())
        text+='\n__WM_SIZE__\nPhysical size: 1080x2400\n__WM_DENSITY__\nPhysical density: 420\n__MEMORY__\nMemTotal: 6291456 kB\n__CPU__\n8\n__INPUT__\n'+json.dumps({'ok':True,'xMin':0,'xMax':1079,'yMin':0,'yMax':2399})
        text+='\n__BLUETOOTH__\nsidecar-state\tvalid\nsidecar\t02:13:24:35:46:57\npersist\t02:13:24:35:46:57\ncontroller\t02:13:24:35:46:57\nready\t1\n'
        commands=[]
        owner=SimpleNamespace(cfg=SimpleNamespace(cold_profile=value),docker_exec=lambda args,**k:commands.append(args) or {'ok':True,'stdout':text})
        self.assertTrue(backend.RuntimeManager.cold_profile_status(owner)['ok'])
        self.assertNotIn('setprop',commands[0][2])
        self.assertNotIn(' tap ',commands[0][2])
        subprocess.run(['sh','-n'],input=commands[0][2],text=True,check=True,capture_output=True)
        changed=text.replace('Physical density: 420','Physical density: 560')
        owner.docker_exec=lambda *a,**k:{'ok':True,'stdout':changed}
        result=backend.RuntimeManager.cold_profile_status(owner)
        self.assertFalse(result['ok'])
        self.assertIn('display.physicalDensity',result['mismatches'])
        invalid=text.replace('sidecar-state\tvalid','sidecar-state\tinvalid')
        owner.docker_exec=lambda *a,**k:{'ok':True,'stdout':invalid}
        self.assertIn('bluetooth.target',backend.RuntimeManager.cold_profile_status(owner)['mismatches'])
        stale=text.replace('controller\t02:13:24:35:46:57','controller\tAA:BB:CC:DD:EE:FF')
        owner.docker_exec=lambda *a,**k:{'ok':True,'stdout':stale}
        self.assertIn('bluetooth.controllerAddress',backend.RuntimeManager.cold_profile_status(owner)['mismatches'])
        not_ready=text.replace('ready\t1','ready\t0')
        owner.docker_exec=lambda *a,**k:{'ok':True,'stdout':not_ready}
        self.assertIn('bluetooth.ready',backend.RuntimeManager.cold_profile_status(owner)['mismatches'])

class ResourceObservationContracts(unittest.TestCase):
    def fixture(self, args=(), provider='microg'):
        cfg=SimpleNamespace(extra_docker_args=list(args),google_services_provider=provider,network_dns_servers=['1.1.1.1'])
        lease=SimpleNamespace(android_adb_port=5555,host_adb_port=62111,volume_name='data-example',network_name='net-example',ipv4_address='172.31.0.10',ipv6_address='fd00::10',mac_address='02:00:00:00:00:01')
        owner=SimpleNamespace(cfg=cfg,lease=lease,_container_has_lease_owner=lambda _:True,_managed_container_labels_match=lambda _:True,_android_boot_command=lambda:['boot'])
        container={'Config':{'Cmd':['boot'],'Labels':{}},'HostConfig':{'AutoRemove':False,'Privileged':True,'RestartPolicy':{'Name':'no'},'NetworkMode':lease.network_name,'Dns':cfg.network_dns_servers,'PortBindings':{'5555/tcp':[{'HostIp':'127.0.0.1','HostPort':'62111'}]},'NanoCpus':1000000000,'Memory':2147483648,'MemorySwap':2684354560},'Mounts':[{'Type':'volume','Name':lease.volume_name,'Destination':'/data'}],'NetworkSettings':{'Networks':{lease.network_name:{'IPAddress':lease.ipv4_address,'GlobalIPv6Address':lease.ipv6_address,'MacAddress':lease.mac_address}}},'State':{'Running':True}}
        return owner,container
    def matches(self, owner, container):
        return backend.RuntimeManager._container_matches_lease(owner,container,image_identity={'ok':True})
    def test_explicit_caps_match_and_each_drift_is_rejected(self):
        args=['--cpus','1','--memory','2147483648','--memory-swap','2684354560']
        owner,container=self.fixture(args)
        self.assertTrue(self.matches(owner,container))
        for field in ('NanoCpus','Memory','MemorySwap'):
            with self.subTest(field=field):
                original=container['HostConfig'][field]
                for incorrect in (original+1,None,str(original),True):
                    container['HostConfig'][field]=incorrect
                    self.assertFalse(self.matches(owner,container))
                container['HostConfig'][field]=original
    def test_unspecified_caps_and_legacy_configs_are_unchanged(self):
        owner,container=self.fixture(['--memory=2147483648'])
        container['HostConfig']['NanoCpus']=0
        container['HostConfig']['MemorySwap']=-1
        self.assertTrue(self.matches(owner,container))
        owner.cfg=SimpleNamespace(network_dns_servers=['1.1.1.1'])
        self.assertTrue(self.matches(owner,container))
        owner.cfg.extra_docker_args=[]
        owner.cfg.google_services_provider='microg'
        self.assertTrue(self.matches(owner,container))
    def test_legacy_provider_none_custom_args_are_unchanged(self):
        owner,container=self.fixture(['--shm-size=128m'],provider='none')
        self.assertTrue(self.matches(owner,container))

if __name__=='__main__':unittest.main()
