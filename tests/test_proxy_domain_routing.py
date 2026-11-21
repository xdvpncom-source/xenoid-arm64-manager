"""Exercise shipped patch with exact Native sources, compiler and root validator."""
import ast
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from native_proxy_routing_fixture import sources

ROOT=Path(__file__).resolve().parents[1]
TEMP=tempfile.TemporaryDirectory(prefix='xenoid-routing-tests-')
CANDIDATE=Path(TEMP.name)
for name,body in sources().items():
    path=CANDIDATE/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(body)
subprocess.run(['git','apply',str(ROOT/'patches/proxy-domain-routing.patch')],cwd=CANDIDATE,check=True,capture_output=True)
SPEC=importlib.util.spec_from_file_location('actual_routing_source',CANDIDATE/'src/xenoid/proxy_source.py')
NATIVE=importlib.util.module_from_spec(SPEC);sys.modules[SPEC.name]=NATIVE;SPEC.loader.exec_module(NATIVE)
ENGINE=runpy.run_path(str(CANDIDATE/'scripts/xenoid-proxy-engine.py'))
ENGINE['validate_config'].__globals__['routing_module']=lambda:NATIVE
HELPER=runpy.run_path(str(ROOT/'qa/qa_proxy_routing.py'))
HELPER_GLOBALS=HELPER['reconcile'].__globals__
SCHEMA='dev.xenoid.qa.proxy-routing/v1'
DEFAULT={'schema':SCHEMA,'exceptions':[{'domain':v,'include_subdomains':True}for v in ('us.xdvpn.com','xdvpn.com','ir.xdgram.com','xdgram.com','github.com')]}


def policy(*entries):
    return {'schema':SCHEMA,'exceptions':[{'domain':domain,'include_subdomains':suffix}for domain,suffix in entries]}


def config(ipv6=True,udp=True):
    return NATIVE.compile_source('endpoint','socks5://fixture:fixture@example.com:1080',udp_allowed=udp,ipv6_allowed=ipv6).config


def direct_for(domain,rules):
    for rule in rules:
        kind,target,route=rule.split(',')if rule!='MATCH,GLOBAL'else('MATCH','','GLOBAL')
        if kind=='MATCH':return route=='DIRECT'
        if kind=='DOMAIN'and domain==target:return route=='DIRECT'
        if kind=='DOMAIN-SUFFIX'and(domain==target or domain.endswith('.'+target)):return route=='DIRECT'
    return False


class NativeDomainPolicyTests(unittest.TestCase):
    def test_shipped_patch_preserves_original_compiler(self):
        old=next(ast.get_source_segment(sources()['src/xenoid/proxy_source.py'].decode(),n)for n in ast.parse(sources()['src/xenoid/proxy_source.py'].decode()).body if isinstance(n,ast.FunctionDef)and n.name=='compile_source')
        new=next(ast.get_source_segment((CANDIDATE/'src/xenoid/proxy_source.py').read_text(),n)for n in ast.parse((CANDIDATE/'src/xenoid/proxy_source.py').read_text()).body if isinstance(n,ast.FunctionDef)and n.name=='compile_source')
        self.assertEqual(old,new)

    def test_all_five_defaults_retained_but_redundant_suffixes_compiled_once(self):
        self.assertEqual(NATIVE.normalize_proxy_routing_policy(DEFAULT),DEFAULT)
        self.assertEqual(NATIVE.proxy_routing_rules(DEFAULT),['DOMAIN-SUFFIX,github.com,DIRECT','DOMAIN-SUFFIX,xdgram.com,DIRECT','DOMAIN-SUFFIX,xdvpn.com,DIRECT','MATCH,GLOBAL'])

    def test_suffix_is_label_bounded_and_raw_github_assets_stay_proxied(self):
        rules=NATIVE.proxy_routing_rules(DEFAULT)
        for name in ('xdvpn.com','us.xdvpn.com','nested.us.xdvpn.com','ir.xdgram.com','api.github.com'):
            with self.subTest(domain=name):self.assertTrue(direct_for(name,rules))
        for name in ('notxdvpn.com','xdvpn.com.evil.test','githubusercontent.com','raw.githubusercontent.com','google.com'):
            with self.subTest(domain=name):self.assertFalse(direct_for(name,rules))

    def test_exact_domain_excludes_children(self):
        rules=NATIVE.proxy_routing_rules(policy(('api.example.com',False)))
        self.assertTrue(direct_for('api.example.com',rules));self.assertFalse(direct_for('sub.api.example.com',rules));self.assertFalse(direct_for('example.com',rules))

    def test_idna_normalization_and_single_label_are_consistent(self):
        p=policy(('BÜCHER.Example.',False),('intranet',True),('BÜCHER.Example.',False))
        self.assertEqual(NATIVE.normalize_proxy_routing_policy(p),policy(('xn--bcher-kva.example',False),('intranet',True)))

    def test_syntax_and_ip_literals_rejected(self):
        for value in ('*.example.com','.example.com','https://example.com','example.com:443','example.com/path','example.com..','  example.com','example.com\n','127.0.0.1','::1','ex\u200bample.com','-bad.example','bad_.example'):
            with self.subTest(value=value),self.assertRaises(NATIVE.ProxyRoutingError):NATIVE.normalize_proxy_routing_policy(policy((value,True)))

    def test_boolean_and_document_shape_validated(self):
        for value in ({'exceptions':None},{'schema':'other','exceptions':[]},policy(('example.com',1)),{'exceptions':[],'surprise':True}):
            with self.subTest(value=value),self.assertRaises(NATIVE.ProxyRoutingError):NATIVE.normalize_proxy_routing_policy(value)

    def test_missing_policy_is_legacy_global(self):
        with tempfile.TemporaryDirectory()as d:
            self.assertEqual(NATIVE.load_proxy_routing_policy('/xenoid-unit-test-nonexistent/missing.json'),policy())

    def test_private_parent_is_rejected_before_agent_would_fail_traversal(self):
        import os,stat
        info=os.stat_result((stat.S_IFDIR|0o700,1,1,1,0,0,0,0,0,0))
        with patch.object(Path,'lstat',return_value=info),patch.object(os,'open')as opened,self.assertRaises(NATIVE.ProxyRoutingError):
            NATIVE.load_proxy_routing_policy('/private-policy-fixture/policy.json')
        opened.assert_not_called()

    def test_oversized_policy_is_rejected_without_clobbering_existing_file(self):
        domains=policy(*[(('a'*60+'.'+'b'*60+'.'+'c'*60+'.'+str(i)+'.example'),False)for i in range(5000)])
        with tempfile.TemporaryDirectory()as directory,patch.dict(sys.modules,{'xenoid.proxy_source':NATIVE}):
            path=Path(directory)/'policy.json';path.write_bytes(b'previous-policy')
            with self.assertRaises(HELPER['RoutingError'])as error:HELPER['save_policy'](domains,path=path)
            self.assertEqual(str(error.exception),'routing_policy_too_large')
            self.assertEqual(path.read_bytes(),b'previous-policy')

    def test_empty_list_restores_global_and_removes_dns_exceptions(self):
        initial=NATIVE.apply_proxy_routing_config(config(),DEFAULT)
        cleared=NATIVE.apply_proxy_routing_config(initial,policy())
        self.assertEqual(cleared['mode'],'global');self.assertEqual(cleared['rules'],['MATCH,GLOBAL']);self.assertNotIn('nameserver-policy',cleared['dns'])

    def test_dns_direct_only_for_requested_domains_and_fallback_unchanged(self):
        old=config();new=NATIVE.apply_proxy_routing_config(old,DEFAULT)
        self.assertEqual(new['dns']['nameserver'],old['dns']['nameserver'])
        self.assertEqual(set(new['dns']['nameserver-policy']),{'+.github.com','+.xdvpn.com','+.xdgram.com'})
        self.assertTrue(all(v==['https://8.8.8.8/dns-query#DIRECT','https://8.8.4.4/dns-query#DIRECT']for v in new['dns']['nameserver-policy'].values()))
        self.assertEqual(old['mode'],'global');self.assertNotIn('nameserver-policy',old['dns'])

    def test_real_native_root_validator_accepts_ipv4_ipv6_udp_modes(self):
        for ipv6 in (False,True):
            for udp in (False,True):
                c=NATIVE.apply_proxy_routing_config(config(ipv6,udp),DEFAULT)
                with self.subTest(ipv6=ipv6,udp=udp):self.assertEqual(ENGINE['validate_config']({},c)[0],'a')

    def test_native_root_keeps_old_config_readable_during_transition(self):
        old=config();self.assertEqual(ENGINE['validate_config']({},old)[0],'a')
        with self.assertRaises(NATIVE.ProxyRoutingError):NATIVE.validate_proxy_routing_config(old,DEFAULT)

    def test_unrequested_direct_rule_rejected_against_current_policy(self):
        extra=NATIVE.apply_proxy_routing_config(config(),policy(('evil.test',True)))
        with self.assertRaises(NATIVE.ProxyRoutingError):NATIVE.validate_proxy_routing_config(extra,DEFAULT)

    def test_global_mode_cannot_ignore_domain_rules(self):
        c=NATIVE.apply_proxy_routing_config(config(),DEFAULT);c['mode']='global'
        with self.assertRaises(ENGINE['Error']):ENGINE['validate_config']({},c)

    def test_direct_fallback_and_extra_dns_policy_rejected_by_native_root(self):
        for mutate in (lambda c:c['rules'].__setitem__(-1,'MATCH,DIRECT'),lambda c:c['dns']['nameserver-policy'].update({'evil.test':['system']})):
            c=NATIVE.apply_proxy_routing_config(config(),DEFAULT);mutate(c)
            with self.assertRaises(ENGINE['Error']):ENGINE['validate_config']({},c)

    def test_write_config_rejects_unconfigured_direct_before_any_staging(self):
        globals_=ENGINE['write_config'].__globals__
        payload={'instanceId':'fixture','runtimeEpoch':'epoch','generation':2,'config':NATIVE.apply_proxy_routing_config(config(),policy(('not-requested.test',True)))}
        with patch.object(NATIVE,'load_proxy_routing_policy',return_value=DEFAULT),self.assertRaises(ENGINE['Error'])as error:
            ENGINE['write_config']({'instanceId':'fixture','runtimeEpoch':'epoch','generation':1},{},json.dumps(payload).encode(),expected_generation=2)
        self.assertEqual(error.exception.code,'routing_policy_invalid')

    def test_agent_attaches_root_policy_before_digest_and_write_config(self):
        tree=ast.parse((CANDIDATE/'scripts/xenoid-proxy-agent.py').read_text())
        run=next(n for n in tree.body if isinstance(n,ast.FunctionDef)and n.name=='_run')
        calls=[n.func.id for n in ast.walk(run)if isinstance(n,ast.Call)and isinstance(n.func,ast.Name)]
        self.assertIn('load_proxy_routing_policy',calls);self.assertIn('apply_proxy_routing_config',calls)
        source=ast.get_source_segment((CANDIDATE/'scripts/xenoid-proxy-agent.py').read_text(),run)
        self.assertLess(source.index('apply_proxy_routing_config'),source.index('config_digest ='))
        self.assertLess(source.index('apply_proxy_routing_config'),source.index('_run_root("write-config"'))

    def test_panel_and_native_normalization_agree(self):
        panel=ROOT.parent/'xenoid/proxy_routing.py'
        if not panel.exists():self.skipTest('Panel module is separately distributed')
        validator=runpy.run_path(str(panel))['validate_proxy_routing']
        for value in (DEFAULT,policy(),policy(('BÜCHER.Example.',False),('intranet',True)),policy(('example.com',False),('example.com',True))):
            self.assertEqual(validator(copy.deepcopy(value)),NATIVE.normalize_proxy_routing_policy(value))

    def test_active_configuration_reader_uses_the_actual_native_required_keywords(self):
        calls=[]
        class Manager:
            def _proxy_read_manifest(self,*,require_live,allow_absent,allow_stopped=False):
                calls.append((require_live,allow_absent,allow_stopped))
                return {'paths':{'config':'/fixture/config.yaml'}}
            def _proxy_root_process(self,args,*,timeout,output_limit):
                self.assertion=args
                return 0,json.dumps(NATIVE.apply_proxy_routing_config(config(),DEFAULT)).encode()
        with patch.dict(sys.modules,{'xenoid.proxy_source':NATIVE}):
            self.assertTrue(HELPER['active_config_matches'](Manager(),DEFAULT))
        self.assertEqual(calls,[(True,False,False)])


class FleetRoutingDispatchTests(unittest.TestCase):
    def setUp(self):
        self.fleet=runpy.run_path(str(ROOT/'xenoid-fleet'))
        self.globals=self.fleet['main'].__globals__
        self.temporary=self.enterContext(tempfile.TemporaryDirectory())
        target=Path(self.temporary)/'panel-scripts';target.mkdir();(target/'qa_proxy_routing.py').write_text('# fixture')
        self.enterContext(patch.dict(self.globals,{'INSTALL_ROOT':Path(self.temporary),'Upstream':lambda:object(),'selection':lambda *_:['1','81']}))

    def test_configure_forwards_required_flag_and_inherits_json_stdin(self):
        with patch.object(subprocess,'run',return_value=subprocess.CompletedProcess([],0))as run:
            self.assertEqual(self.fleet['main'](['proxy-routing','configure','--json-stdin']),0)
        self.assertEqual(run.call_args.args[0][-2:],['configure','--json-stdin'])
        self.assertNotIn('stdin',run.call_args.kwargs)

    def test_reconcile_selects_devices_and_forwards_unlimited_budget(self):
        with patch.object(subprocess,'run',return_value=subprocess.CompletedProcess([],0))as run:
            self.assertEqual(self.fleet['main'](['proxy-routing','reconcile','1,81','--timeout','0']),0)
        self.assertEqual(run.call_args.args[0][-7:],['reconcile','--timeout','0','--device','phone-1','--device','phone-81'])
        self.assertEqual(run.call_args.kwargs['stdin'],subprocess.DEVNULL)

    def test_all_selector_reaches_actual_helper_and_succeeds_for_empty_registry(self):
        modules={
            'xenoid.proxy_source':NATIVE,
            'script_host':types.SimpleNamespace(adopt_native_environment=lambda:None),
            'xenoid.backend':types.SimpleNamespace(RuntimeManager=object),
            'xenoid.config':types.SimpleNamespace(resolve_instance=lambda *_:None),
            'xenoid.device_identity':types.SimpleNamespace(RegenerationJournal=object),
            'xenoid.convergence':types.SimpleNamespace(ConvergenceExecutor=object),
            'qa_resources':types.SimpleNamespace(registry_names=lambda:[]),
        }
        calls=[]
        def dispatch(command,**kwargs):
            calls.append((command,kwargs))
            with patch.dict(sys.modules,modules),patch.object(os,'geteuid',return_value=0),patch.object(os,'chdir'),patch.object(sys,'path',list(sys.path)):
                code=HELPER['main'](command[2:])
            return subprocess.CompletedProcess(command,code)
        with patch.dict(self.globals,{'selection':lambda *_:[]}),patch.object(subprocess,'run',side_effect=dispatch):
            self.assertEqual(self.fleet['main'](['proxy-routing','reconcile','all','--timeout','0']),0)
        self.assertEqual(calls[0][0][-4:],['reconcile','--timeout','0','--all'])
        self.assertEqual(calls[0][1]['stdin'],subprocess.DEVNULL)

    def test_all_selector_preserved_when_registry_has_devices(self):
        with patch.object(subprocess,'run',return_value=subprocess.CompletedProcess([],7))as run:
            self.assertEqual(self.fleet['main'](['proxy-routing','reconcile','all']),7)
        self.assertEqual(run.call_args.args[0][-4:],['reconcile','--timeout','180','--all'])
        self.assertNotIn('--device',run.call_args.args[0])

    def test_show_is_read_only_dispatch_without_configure_flag(self):
        with patch.object(subprocess,'run',return_value=subprocess.CompletedProcess([],0))as run:
            self.assertEqual(self.fleet['main'](['proxy-routing','show']),0)
        self.assertEqual(run.call_args.args[0][-1],'show')
        self.assertNotIn('--json-stdin',run.call_args.args[0])


class NativeRoutingReconcileTests(unittest.TestCase):
    def setUp(self):
        self.policy=copy.deepcopy(DEFAULT);self.original={'kind':'endpoint','value':'socks5://fixture@example.com:1080','selectedNode':'chosen','udpAllowed':False,'allowInsecureHttp':False,'ipv6Allowed':True}
        self.client=types.SimpleNamespace(proxy_status=lambda:{'ok':True,'stateReadable':True,'configured':True,'enabled':True},proxy_export=lambda:{'ok':True,'source':copy.deepcopy(self.original)})
        self.events=[];self.applied=False;self.owned={'Id':'a'*64,'Image':'sha256:'+'b'*64,'State':{'Running':True},'Mounts':[{'Destination':'/data','Name':'preserved'}]}
        self.manager=types.SimpleNamespace(context=types.SimpleNamespace(instance_id='iid',resource_tag='tag'),cfg='cfg',lease='lease',_owned_container_record=lambda **_:(copy.deepcopy(self.owned),None),daemon_client=lambda:self.client,_proxy_root_json=lambda _: {'ok':True,'controlDigest':'old'},_proxy_local_control_digest=lambda:'new',proxy_prerequisite=lambda:self.event('prerequisite'),proxy_quarantine=lambda:self.event('quarantine'),_proxy_stop_agent=lambda:self.event('stop-agent')['ok'],instance_operation_lock=lambda **_:contextlib.nullcontext())
        self.broker=types.SimpleNamespace(display_raw_docker=lambda *_:contextlib.nullcontext())
        parent=self
        class Controller:
            def __init__(self,*_):pass
            def set_source(self,*args,**kwargs):parent.events.append(('native-source',args,kwargs));parent.applied=True;return {'ok':True}
        self.enterContext(patch.dict(sys.modules,{'xenoid.proxy_source':NATIVE,'xenoid.proxy_controller':types.SimpleNamespace(ProxyController=Controller)}))
        self.enterContext(patch.object(NATIVE,'load_proxy_routing_policy',side_effect=lambda:copy.deepcopy(self.policy)))
        self.enterContext(patch.dict(HELPER_GLOBALS,{'active_config_matches':lambda *_:self.applied}))

    def event(self,name):self.events.append(name);return {'ok':True}

    def reconcile(self,**options):
        return HELPER['reconcile'](self.manager,self.broker,timeout=10,pending=lambda _:False,proof=lambda *_:self.events.append('strict-proof'),**options)

    def test_current_source_node_and_all_flags_preserved_through_native_mutation(self):
        result=self.reconcile();self.assertTrue(result['ok']);self.assertTrue(result['container_binding_preserved']);self.assertTrue(result['desired_proxy_preserved'])
        mutation=next(e for e in self.events if isinstance(e,tuple));self.assertEqual(mutation[1],('endpoint',self.original['value'],True));self.assertEqual(mutation[2],{'selected_node':'chosen','udp_allowed':False,'allow_insecure_http':False,'ipv6_allowed':True});self.assertLess(self.events.index('stop-agent'),self.events.index(mutation));self.assertIn('strict-proof',self.events)

    def test_pending_gen_does_not_mutate(self):
        with self.assertRaises(HELPER['RoutingError']):HELPER['reconcile'](self.manager,self.broker,timeout=10,pending=lambda _:True)
        self.assertEqual(self.events,[])

    def test_disabled_proxy_is_not_enabled_implicitly(self):
        self.client.proxy_status=lambda:{'ok':True,'stateReadable':True,'configured':True,'enabled':False}
        self.assertEqual(self.reconcile()['skipped'],'disabled_or_unconfigured');self.assertEqual(self.events,[])

    def test_binding_change_is_rejected_after_native_proof(self):
        original=self.manager._owned_container_record;n=[0]
        def inspect(**kwargs):
            n[0]+=1;owned,_=original(**kwargs)
            if n[0]>1:owned['Id']='c'*64
            return owned,None
        self.manager._owned_container_record=inspect
        with self.assertRaises(HELPER['RoutingError'])as error:self.reconcile()
        self.assertEqual(str(error.exception),'routing_binding_changed')

    def test_policy_race_is_rejected_after_native_proof(self):
        def proof(*_):self.policy=policy()
        with self.assertRaises(HELPER['RoutingError'])as error:HELPER['reconcile'](self.manager,self.broker,timeout=10,pending=lambda _:False,proof=proof)
        self.assertEqual(str(error.exception),'routing_policy_changed_during_apply')

    def test_failed_native_proof_does_not_report_success(self):
        def proof(*_):raise HELPER['RoutingError']('engine_quarantined')
        with self.assertRaises(HELPER['RoutingError'])as error:HELPER['reconcile'](self.manager,self.broker,timeout=10,pending=lambda _:False,proof=proof)
        self.assertEqual(str(error.exception),'engine_quarantined')

    def test_current_config_proves_without_reimport(self):
        self.manager._proxy_root_json=lambda _:{'ok':True,'controlDigest':'new'}
        with patch.dict(HELPER_GLOBALS,{'active_config_matches':lambda *_:True}):result=self.reconcile()
        self.assertFalse(result['changed']);self.assertEqual(self.events,['strict-proof'])

    def test_save_does_not_restore_defaults_over_administrator_empty_policy(self):
        with tempfile.TemporaryDirectory()as directory:
            target=Path(directory)/'policy.json';target.parent.chmod(0o755);target.write_text(json.dumps(policy()));target.chmod(0o644)
            original_lstat=Path.lstat
            def root_info(path):
                info=original_lstat(path);v=list(info);v[4]=0;v[5]=0;return os.stat_result(v)
            with patch.object(Path,'lstat',root_info),patch.object(NATIVE,'load_proxy_routing_policy',return_value=policy()):
                result=HELPER['save_policy'](DEFAULT,path=target,only_if_missing=True)
            self.assertFalse(result['changed']);self.assertEqual(json.loads(target.read_text()),policy())


class PrivateLegacyMigrationTests(unittest.TestCase):
    def test_public_profile_has_its_own_directory(self):
        self.assertEqual(NATIVE.PROXY_ROUTING_PATH,'/etc/xenoid-proxy-routing/policy.json')
        self.assertEqual(str(HELPER['POLICY']),NATIVE.PROXY_ROUTING_PATH)
        self.assertEqual(str(HELPER['LEGACY_POLICY']),'/etc/xenoid/qa-proxy-routing.json')

    @contextlib.contextmanager
    def root_metadata(self,private):
        import stat
        actual_lstat=Path.lstat;actual_fstat=os.fstat
        def metadata(path):
            info=actual_lstat(path)
            if stat.S_ISDIR(info.st_mode):
                value=list(info);value[4]=0;value[5]=0;return os.stat_result(value)
            return os.stat_result((stat.S_IFDIR|0o755,1,1,1,0,0,0,0,0,0))
        def fstat(descriptor):
            value=list(actual_fstat(descriptor));value[4]=0;value[5]=0;return os.stat_result(value)
        with patch.object(Path,'lstat',metadata),patch.object(os,'fstat',fstat),patch.object(os,'geteuid',return_value=0),patch.object(os,'fchown'),patch.dict(sys.modules,{'xenoid.proxy_source':NATIVE}):yield

    def test_private_native_directory_is_not_chmodded_and_custom_or_empty_policy_migrates(self):
        for value in (policy(('custom.example',False)),policy()):
            with self.subTest(value=value),tempfile.TemporaryDirectory()as directory:
                root=Path(directory);private=root/'native-private';private.mkdir(mode=0o700);old=private/'qa-proxy-routing.json';old.write_text(json.dumps(value));old.chmod(0o600);new=root/'public-routing'/'policy.json';before=old.read_bytes()
                with self.root_metadata(private):result=HELPER['ensure_default_policy'](path=new,legacy_path=old)
                self.assertTrue(result['migrated_legacy']);self.assertEqual(json.loads(new.read_text()),value);self.assertEqual(new.stat().st_mode&0o777,0o644);self.assertEqual(new.parent.stat().st_mode&0o777,0o755);self.assertEqual(private.stat().st_mode&0o777,0o700);self.assertEqual(old.stat().st_mode&0o777,0o600);self.assertEqual(old.read_bytes(),before)

    def test_new_public_directory_is_traversable_under_service_umask_0077(self):
        with tempfile.TemporaryDirectory()as directory:
            parent=Path(directory)/'public-routing';target=parent/'policy.json';previous=os.umask(0o077)
            try:
                with self.root_metadata(Path(directory)):result=HELPER['save_policy'](DEFAULT,path=target)
            finally:os.umask(previous)
            self.assertTrue(result['changed']);self.assertEqual(parent.stat().st_mode&0o777,0o755);self.assertEqual(target.stat().st_mode&0o777,0o644);self.assertEqual(json.loads(target.read_text()),DEFAULT)

    def test_preexisting_private_directory_is_rejected_without_chmod(self):
        with tempfile.TemporaryDirectory()as directory:
            parent=Path(directory)/'existing-private';parent.mkdir(mode=0o700);target=parent/'policy.json';previous=os.umask(0o077)
            try:
                with self.root_metadata(parent),self.assertRaises(HELPER['RoutingError'])as error:HELPER['save_policy'](DEFAULT,path=target)
            finally:os.umask(previous)
            self.assertEqual(str(error.exception),'routing_policy_parent_invalid');self.assertEqual(parent.stat().st_mode&0o777,0o700);self.assertFalse(target.exists())

    def test_new_administrator_empty_profile_wins_without_reading_private_legacy(self):
        with tempfile.TemporaryDirectory()as directory:
            target=Path(directory)/'new.json';target.write_text(json.dumps(policy()));old=Path(directory)/'legacy.json';old.write_text(json.dumps(DEFAULT))
            with patch.dict(HELPER_GLOBALS,{'read_legacy_policy':lambda *_:self.fail('legacy must not be read'),'save_policy':lambda value,**kw:{'ok':True,'changed':False}}):result=HELPER['ensure_default_policy'](path=target,legacy_path=old)
            self.assertFalse(result['migrated_legacy']);self.assertEqual(json.loads(target.read_text()),policy())

    def test_writable_legacy_file_is_rejected_without_creating_public_profile(self):
        with tempfile.TemporaryDirectory()as directory:
            private=Path(directory)/'private';private.mkdir(mode=0o700);old=private/'policy.json';old.write_text(json.dumps(DEFAULT));old.chmod(0o664);new=Path(directory)/'public'/'policy.json'
            with self.root_metadata(private),self.assertRaises(HELPER['RoutingError'])as error:HELPER['ensure_default_policy'](path=new,legacy_path=old)
            self.assertEqual(str(error.exception),'routing_legacy_file_invalid');self.assertFalse(new.exists());self.assertEqual(private.stat().st_mode&0o777,0o700)

    def test_malformed_private_profile_is_not_replaced_with_defaults(self):
        with tempfile.TemporaryDirectory()as directory:
            private=Path(directory)/'private';private.mkdir(mode=0o700);old=private/'policy.json';old.write_text('{bad-json');old.chmod(0o600);new=Path(directory)/'public'/'policy.json'
            with self.root_metadata(private),self.assertRaises(ValueError):HELPER['ensure_default_policy'](path=new,legacy_path=old)
            self.assertFalse(new.exists());self.assertEqual(old.read_text(),'{bad-json')


if __name__=='__main__':unittest.main()
