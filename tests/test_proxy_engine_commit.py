"""Native apply/probe algorithms, with only privileged IO replaced by fixtures."""
import ast
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parent))
import test_proxy_counter_acceptance as acceptance

OLD_APPLY = r'''def apply(m,o,target="candidate",commit=False,expected_generation=None):
 if target=="previous":
  quarantine(m,o);stop(m,"pending-process");previous=o.get("previous")
  if not isinstance(previous,dict) or not proc_path(m,"process").exists():
   o["candidate"]=o["previous"]=None;save_owner(m,o);return
  config_path=Path(m["paths"]["config"]);_,tp,dp,cp,secret,udp_allowed,ipv6_allowed=candidate_config(m,config_path,previous["name"]);record=checked_process(m,proc_path(m,"process"),previous["generation"],previous["name"])
  configure(m,previous["name"],tp,dp,udp_allowed,ipv6_allowed);health(m,None,(tp,dp,cp),secret,udp_allowed,ipv6_allowed,{"stateName":"process","generation":record["generation"],"candidate":record["candidate"]});activate(m,previous["name"])
  o["appliedGeneration"],o["activeCandidate"]=previous["generation"],previous["name"];o["candidate"]=o["previous"]=None;o["phase"]="applied";save_owner(m,o);return
 if target!="candidate":raise Error("ipc_request_invalid")
 if expected_generation is not None:
  candidate=o.get("candidate")
  if not isinstance(candidate,dict) or candidate.get("generation")!=expected_generation:raise Error("ipc_request_invalid")
 if commit:
  if o["phase"]!="applied-pending" or not isinstance(o.get("candidate"),dict):raise Error("apply_not_pending")
  meta=o["candidate"];path=Path(meta["path"]);_,tp,dp,cp,secret,udp_allowed,ipv6_allowed=candidate_config(m,path,meta["name"]);pending=checked_process(m,proc_path(m,"pending-process"),meta["generation"],meta["name"])
  configure(m,meta["name"],tp,dp,udp_allowed,ipv6_allowed);health(m,None,(tp,dp,cp),secret,udp_allowed,ipv6_allowed,{"stateName":"pending-process","generation":pending["generation"],"candidate":pending["candidate"]});activate(m,meta["name"])
  config_path=Path(m["paths"]["config"]);candidate_bytes=path.read_bytes();proxy_user=pwd.getpwnam(m["users"]["proxy"])
  stop(m,"process");os.replace(proc_path(m,"pending-process"),proc_path(m,"process"));atomic(config_path,candidate_bytes,0o640,0,proxy_user.pw_gid)
  o["appliedGeneration"],o["activeCandidate"]=meta["generation"],meta["name"];o["candidate"]=o["previous"]=None;o["phase"]="applied";save_owner(m,o);return
 meta=o.get("candidate")
 if not isinstance(meta,dict):raise Error("config_missing")
 path=Path(meta["path"])
 if sha(path)!=meta["sha256"]:raise Error("config_digest_mismatch")
 _,tproxy_port,dns_port,controller_port,secret,udp_allowed,ipv6_allowed=candidate_config(m,path,meta["name"])
 apply_stage("baseline",quarantine,m,o);prerequisites();validate_binary(m)
 apply_stage("topology",topology,m,o);apply_stage("policy",policy,m,o);apply_stage("dataplane",dataplane,m,o)
 configure(m,meta["name"],tproxy_port,dns_port,udp_allowed,ipv6_allowed)
 child=launch(m,o,path,meta["name"],meta["generation"],"pending-process")
 try:health(m,child,(tproxy_port,dns_port,controller_port),secret,udp_allowed,ipv6_allowed);activate(m,meta["name"]);outbound_ready(m,controller_port,secret)
 except Error:quarantine(m,o);stop(m,"pending-process",child);raise
 o["previous"]={"generation":o["appliedGeneration"],"name":o["activeCandidate"]} if isinstance(o["appliedGeneration"],int) and o["activeCandidate"] in ("a","b") else None;o["phase"]="applied-pending";save_owner(m,o)
'''
OLD_APPLY_SHA256 = '47766a5ff77b6716cb1505e3a6408cbad341d1511c8a73dfcfefa07e03875ffd'

NEW_APPLY = r'''def apply(m,o,target="candidate",commit=False,expected_generation=None):
 if target=="previous":
  quarantine(m,o);stop(m,"pending-process");previous=o.get("previous")
  if not isinstance(previous,dict) or not proc_path(m,"process").exists():
   o["candidate"]=o["previous"]=None;save_owner(m,o);return
  config_path=Path(m["paths"]["config"]);_,tp,dp,cp,secret,udp_allowed,ipv6_allowed=candidate_config(m,config_path,previous["name"]);record=checked_process(m,proc_path(m,"process"),previous["generation"],previous["name"])
  configure(m,previous["name"],tp,dp,udp_allowed,ipv6_allowed);health(m,None,(tp,dp,cp),secret,udp_allowed,ipv6_allowed,{"stateName":"process","generation":record["generation"],"candidate":record["candidate"]});activate(m,previous["name"])
  o["appliedGeneration"],o["activeCandidate"]=previous["generation"],previous["name"];o["candidate"]=o["previous"]=None;o["phase"]="applied";save_owner(m,o);return
 if target!="candidate":raise Error("ipc_request_invalid")
 if expected_generation is not None:
  candidate=o.get("candidate")
  if not isinstance(candidate,dict) or candidate.get("generation")!=expected_generation:raise Error("ipc_request_invalid")
 if commit:
  if o["phase"]!="applied-pending" or not isinstance(o.get("candidate"),dict):raise Error("apply_not_pending")
  meta=o["candidate"];path=Path(meta["path"]);_,tp,dp,cp,secret,udp_allowed,ipv6_allowed=candidate_config(m,path,meta["name"]);pending=checked_process(m,proc_path(m,"pending-process"),meta["generation"],meta["name"])
  # Staging already configured/activated this candidate before the agent's
  # successful Android probe. Reapplying those rules here would flush the
  # meters and invalidate that proof without changing generation/runtimeEpoch.
  health(m,None,(tp,dp,cp),secret,udp_allowed,ipv6_allowed,{"stateName":"pending-process","generation":pending["generation"],"candidate":pending["candidate"]})
  observed=status(m,o,expected_generation=meta["generation"])
  if (observed.get("ok") is not True or observed.get("structuralApplied") is not True or observed.get("phase")!="active" or observed.get("generation")!=meta["generation"] or any(observed.get(key)!=m[key] for key in ("instanceId","resourceTag","runtimeEpoch","manifestDigest"))):raise Error("engine_structure_mismatch")
  config_path=Path(m["paths"]["config"]);candidate_bytes=path.read_bytes();proxy_user=pwd.getpwnam(m["users"]["proxy"])
  stop(m,"process");os.replace(proc_path(m,"pending-process"),proc_path(m,"process"));atomic(config_path,candidate_bytes,0o640,0,proxy_user.pw_gid)
  o["appliedGeneration"],o["activeCandidate"]=meta["generation"],meta["name"];o["candidate"]=o["previous"]=None;o["phase"]="applied";save_owner(m,o);return
 meta=o.get("candidate")
 if not isinstance(meta,dict):raise Error("config_missing")
 path=Path(meta["path"])
 if sha(path)!=meta["sha256"]:raise Error("config_digest_mismatch")
 _,tproxy_port,dns_port,controller_port,secret,udp_allowed,ipv6_allowed=candidate_config(m,path,meta["name"])
 apply_stage("baseline",quarantine,m,o);prerequisites();validate_binary(m)
 apply_stage("topology",topology,m,o);apply_stage("policy",policy,m,o);apply_stage("dataplane",dataplane,m,o)
 configure(m,meta["name"],tproxy_port,dns_port,udp_allowed,ipv6_allowed)
 child=launch(m,o,path,meta["name"],meta["generation"],"pending-process")
 try:health(m,child,(tproxy_port,dns_port,controller_port),secret,udp_allowed,ipv6_allowed);activate(m,meta["name"]);outbound_ready(m,controller_port,secret)
 except Error:quarantine(m,o);stop(m,"pending-process",child);raise
 o["previous"]={"generation":o["appliedGeneration"],"name":o["activeCandidate"]} if isinstance(o["appliedGeneration"],int) and o["activeCandidate"] in ("a","b") else None;o["phase"]="applied-pending";save_owner(m,o)
'''
NEW_APPLY_SHA256 = 'e0e0b21bf82c647305a40972d3d9434c2a0dda8bcdb7a1b0167a3746192f0fe5'

AGENT_PROBE = r'''def _probe_data_plane(
    channel: DaemonChannel,
    desired: dict[str, Any],
    manifest: dict[str, Any],
    manifest_path: Path,
    before: dict[str, Any],
    *,
    expected_generation: Optional[int] = None,
) -> dict[str, Any]:
    check_id = desired["checkId"]
    if check_id <= 0:
        raise AgentFailure("data_plane_unverified")
    result = channel.call("probe", {"checkId": check_id})
    if set(result) != {"checkId", "capabilities", "elapsedMs", "errorCode"}:
        raise ChannelAuthenticationFailure("agent_channel_auth_failed")
    capabilities = result["capabilities"]
    error_code = result["errorCode"]
    if (
        not isinstance(result["checkId"], int)
        or isinstance(result["checkId"], bool)
        or result["checkId"] != check_id
        or not isinstance(capabilities, dict)
        or set(capabilities) != CAPABILITY_KEYS
        or any(not isinstance(value, bool) for value in capabilities.values())
        or not isinstance(result["elapsedMs"], int)
        or isinstance(result["elapsedMs"], bool)
        or not 0 <= result["elapsedMs"] <= ANDROID_PROBE_TIMEOUT_MS
        or not isinstance(error_code, str)
        or (error_code != "" and not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error_code))
    ):
        raise ChannelAuthenticationFailure("agent_channel_auth_failed")
    if error_code:
        raise AgentFailure(error_code)
    udp_allowed = desired["source"]["udpAllowed"]
    ipv6_allowed = desired["source"].get("ipv6Allowed", True)
    if not isinstance(ipv6_allowed, bool):
        raise AgentFailure("data_plane_unverified")
    policy_matches = (
        capabilities["v4DnsProxy"]
        and capabilities["v4TcpProxy"]
        and capabilities["v6DnsProxy"] == ipv6_allowed
        and capabilities["v6TcpProxy"] == ipv6_allowed
        and capabilities["v4UdpProxy"] == udp_allowed
        and capabilities["v6UdpProxy"] == (udp_allowed and ipv6_allowed)
    )
    if not policy_matches:
        raise AgentFailure("data_plane_unverified")

    after = _live_status(manifest, manifest_path)
    expected = desired["generation"] if expected_generation is None else expected_generation
    if after["generation"] != expected:
        raise AgentFailure("agent_stale")
    if not after["structuralApplied"]:
        code = {
            "active": "engine_process_failed",
            "applying": "apply_not_pending",
            "quarantined": "engine_quarantined",
            "off": "engine_unexpected_off",
        }.get(after["phase"], "engine_structure_mismatch")
        raise AgentFailure(code)
    for capability in CAPABILITY_KEYS:
        counter_prefix = capability[:-len("Proxy")]
        udp_blocked = "Udp" in capability and not udp_allowed
        for stage in ("Ingress", "Uplink", "Egress"):
            counter = counter_prefix + stage
            if counter not in before["counters"] or counter not in after["counters"]:
                raise AgentFailure("data_plane_unverified")
            if stage != "Egress" or capabilities[capability]:
                if after["counters"][counter] <= before["counters"][counter]:
                    raise AgentFailure("data_plane_unverified")
            elif udp_blocked:
                if after["counters"][counter] != before["counters"][counter]:
                    raise AgentFailure("data_plane_unverified")
            elif after["counters"][counter] < before["counters"][counter]:
                raise AgentFailure("data_plane_unverified")
    after["capabilities"] = dict(capabilities)
    after["dataPlaneVerified"] = True
    after["phase"] = "active"
    return after
'''
AGENT_PROBE_SHA256 = '3089c1818706a1171b3340df066e9436db107f01ed005dd3f68c3e96b71140e5'

AGENT_REPORT = r'''def _report_status(
    channel: DaemonChannel,
    desired: dict[str, Any],
    live: dict[str, Any],
    *,
    error_code: str = "",
) -> None:
    report = {
        "generation": desired["generation"],
        "structuralApplied": live["structuralApplied"], "dataPlaneVerified": live["dataPlaneVerified"],
        "phase": live["phase"], "selectedNode": live["selectedNode"], "nodeCount": live["nodeCount"],
        "nodes": live.get("nodes", []), "capabilities": live["capabilities"],
        "counters": live["counters"],
    }
    if live["phase"] != "off":
        report["checkId"] = desired["checkId"]
    if error_code:
        report["errorCode"] = error_code
    if len(_canonical(report)) > 60 * 1024:
        raise AgentFailure("source_invalid")
    channel.call("report", report)
'''
AGENT_REPORT_SHA256 = 'ecec0fe75fc055b81783c05f1cb8024ec61d2e7a5ae7a003606f7eb350388f51'

class EngineError(RuntimeError):
    def __init__(self,code):super().__init__(code);self.code=code

CAPS=copy.deepcopy(acceptance.CAPS)
IDENTITY={'instanceId':'11111111-1111-4111-8111-111111111111','resourceTag':'0123456789ab',
          'runtimeEpoch':acceptance.EPOCH,'manifestDigest':'f'*64}

class EngineFixture:
    def __init__(self,root,source):
        self.root=root;self.events=[];self.bad_status={};self.dead=False;self.unhealthy=False
        self.counters={cap[:-5]+stage:10 for cap in CAPS for stage in ('Ingress','Uplink','Egress')}
        self.manifest={**IDENTITY,'paths':{'config':str(root/'config')},'users':{'proxy':'fixture'},'generation':7}
        self.owner={'phase':'applied-pending','candidate':{'path':str(root/'candidate'),'name':'b','generation':7,'sha256':'fixture'},
                    'previous':{'name':'a','generation':6},'appliedGeneration':6,'activeCandidate':'a'}
        for name in ('candidate','config','process','pending-process'):(root/name).write_bytes(b'fixture config')
        def configure(*args):
            self.events.append('configure');self.counters={key:0 for key in self.counters}
        def activate(*args):
            self.events.append('activate');self.counters={key:0 for key in self.counters}
        def health(*args):
            self.events.append('health')
            if self.unhealthy:raise EngineError('engine_process_failed')
        def checked_process(manager,path,generation,name):
            self.events.append('checked-process')
            if self.dead:raise EngineError('process_not_running')
            return {'generation':generation,'candidate':name}
        def status(manager,owner,expected_generation=None):
            self.events.append('fresh-status')
            return {**self.live(),**self.bad_status}
        def replace(source,target):self.events.append('promote-process')
        def atomic(path,data,*args):self.events.append('promote-config')
        def stop(manager,name,*args):self.events.append('stop:'+name)
        namespace={'Path':Path,'Error':EngineError,'candidate_config':lambda manager,path,name:({},1001,1002,1003,'fixture',False,False),
            'checked_process':checked_process,'proc_path':lambda manager,name:root/name,'configure':configure,'activate':activate,
            'health':health,'status':status,'pwd':types.SimpleNamespace(getpwnam=lambda user:types.SimpleNamespace(pw_gid=123)),
            'os':types.SimpleNamespace(replace=replace),'atomic':atomic,'stop':stop,
            'save_owner':lambda *args:self.events.append('save-owner'),'quarantine':lambda *args:self.events.append('quarantine'),
            'prerequisites':lambda:None,'validate_binary':lambda *args:None,'topology':lambda *args:None,
            'policy':lambda *args:None,'dataplane':lambda *args:None,'sha':lambda path:'fixture',
            'launch':lambda *args:object(),'outbound_ready':lambda *args:None}
        namespace['apply_stage']=lambda name,method,*args:method(*args)
        exec(compile('from __future__ import annotations\n'+source,'actual-native-engine-apply.py','exec'),namespace)
        self.apply=namespace['apply']
    def live(self):
        return {'ok':True,**IDENTITY,'generation':7,'phase':'active','quarantined':False,'structuralApplied':True,
                'dataPlaneVerified':False,'counters':copy.deepcopy(self.counters),'capabilities':copy.deepcopy(CAPS),
                'selectedNode':'','nodeCount':0,'nodes':[]}
    def commit(self):self.apply(self.manifest,self.owner,commit=True,expected_generation=7)
    def probe(self,increment=True):
        desired={'checkId':2,'generation':7,'source':{'udpAllowed':False,'ipv6Allowed':False}}
        before=self.live()
        def call(method,args):
            if increment:
                for capability,enabled in CAPS.items():
                    prefix=capability[:-5]
                    for stage in ('Ingress','Uplink','Egress'):
                        if stage!='Egress' or enabled:self.counters[prefix+stage]+=1
            return {'checkId':2,'capabilities':copy.deepcopy(CAPS),'elapsedMs':1,'errorCode':''}
        namespace={'AgentFailure':EngineError,'ChannelAuthenticationFailure':EngineError,'CAPABILITY_KEYS':set(CAPS),
            'ANDROID_PROBE_TIMEOUT_MS':45000,'_live_status':lambda *args:self.live()}
        exec(compile('from __future__ import annotations\n'+AGENT_PROBE,'actual-native-agent-probe.py','exec'),namespace)
        return namespace['_probe_data_plane'](types.SimpleNamespace(call=call),desired,self.manifest,self.root/'manifest',before)

class NativeProxyCommitTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
    def engine(self,source=NEW_APPLY):
        root=self.root/str(len(list(self.root.iterdir())));root.mkdir()
        return EngineFixture(root,source)
    def result(self,engine,report):
        snapshot,daemon=acceptance.fixture(host_count=100)
        published=[]
        namespace={'AgentFailure':EngineError,'_canonical':lambda value:json.dumps(value,sort_keys=True).encode()}
        exec(compile('from __future__ import annotations\n'+AGENT_REPORT,'actual-native-agent-report.py','exec'),namespace)
        namespace['_report_status'](types.SimpleNamespace(call=lambda method,value:published.append(copy.deepcopy(value))),
                                    {'generation':7,'checkId':2},report)
        daemon['proxy']['report']=published[-1]
        daemon['proxy']['probe']={'checkId':2,'capabilities':copy.deepcopy(CAPS)}
        return acceptance.inspect(acceptance.NEW,snapshot,daemon,acceptance.Host(engine.live()))
    def test_exact_native_function_fixtures_are_pinned(self):
        for source,digest in ((OLD_APPLY,OLD_APPLY_SHA256),(NEW_APPLY,NEW_APPLY_SHA256),(AGENT_PROBE,AGENT_PROBE_SHA256),(AGENT_REPORT,AGENT_REPORT_SHA256)):
            self.assertEqual(hashlib.sha256(source.encode()).hexdigest(),digest)
    def test_old_native_probe_then_commit_reproduces_counter_proof_invalidation(self):
        engine=self.engine(OLD_APPLY);report=engine.probe();engine.commit()
        self.assertTrue(report['dataPlaneVerified'])
        self.assertEqual(set(engine.counters.values()),{0})
        self.assertFalse(self.result(engine,report).values['proxyDataPlane']['ok'])
    def test_new_commit_preserves_real_probe_counters_and_native_acceptance_proof(self):
        engine=self.engine();report=engine.probe();before=copy.deepcopy(engine.counters);engine.commit()
        self.assertEqual(engine.counters,before)
        self.assertNotIn('configure',engine.events);self.assertNotIn('activate',engine.events)
        self.assertTrue(self.result(engine,report).values['proxyDataPlane']['ok'])
        self.assertLess(engine.events.index('health'),engine.events.index('stop:process'))
        self.assertLess(engine.events.index('fresh-status'),engine.events.index('stop:process'))
    def test_initial_candidate_stage_still_configures_and_activates(self):
        traces=[]
        for source in (OLD_APPLY,NEW_APPLY):
            engine=self.engine(source);engine.owner['phase']='prepared'
            engine.apply(engine.manifest,engine.owner,commit=False,expected_generation=7)
            self.assertIn('configure',engine.events);self.assertIn('activate',engine.events)
            self.assertEqual(engine.owner['phase'],'applied-pending');traces.append(engine.events)
        self.assertEqual(traces[0],traces[1])
    def test_rollback_previous_keeps_original_configuration_and_activation_behavior(self):
        traces=[]
        for source in (OLD_APPLY,NEW_APPLY):
            engine=self.engine(source);engine.apply(engine.manifest,engine.owner,target='previous',commit=True)
            self.assertEqual(engine.owner['activeCandidate'],'a');self.assertEqual(engine.owner['appliedGeneration'],6)
            self.assertIn('configure',engine.events);self.assertIn('activate',engine.events);traces.append(engine.events)
        self.assertEqual(traces[0],traces[1])
    def test_only_commit_algorithm_changed_in_actual_native_apply(self):
        def noncommit(source):
            node=ast.parse(source).body[0]
            node.body=[item for item in node.body if not(isinstance(item,ast.If) and isinstance(item.test,ast.Name) and item.test.id=='commit')]
            return ast.dump(node,include_attributes=False)
        self.assertEqual(noncommit(OLD_APPLY),noncommit(NEW_APPLY))
    def test_invalid_structure_generation_and_epoch_never_stop_lkg_or_promote(self):
        changes=({'ok':False},{'structuralApplied':False},{'phase':'quarantined'},{'generation':8},
                 {'runtimeEpoch':'b'*64},{'instanceId':'22222222-2222-4222-8222-222222222222'},
                 {'resourceTag':'fedcba987654'},{'manifestDigest':'0'*64})
        for change in changes:
            with self.subTest(change=change):
                engine=self.engine();engine.bad_status=change
                with self.assertRaises(EngineError):engine.commit()
                self.assertNotIn('stop:process',engine.events);self.assertNotIn('promote-process',engine.events)
                self.assertEqual(engine.owner['phase'],'applied-pending')
    def test_dead_worker_or_failed_health_never_stop_lkg_or_promote(self):
        for failure in ('dead','unhealthy'):
            with self.subTest(failure=failure):
                engine=self.engine();setattr(engine,failure,True)
                with self.assertRaises(EngineError):engine.commit()
                self.assertNotIn('stop:process',engine.events);self.assertNotIn('promote-process',engine.events)
    def test_actual_agent_probe_still_requires_real_counter_deltas(self):
        engine=self.engine()
        with self.assertRaisesRegex(EngineError,'data_plane_unverified'):engine.probe(increment=False)
        self.assertNotIn('promote-process',engine.events)

if __name__=='__main__':unittest.main()
