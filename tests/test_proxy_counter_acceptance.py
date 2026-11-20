"""Run actual native acceptance code against counter-ordering and reset fixtures.

Set XENOID_PROXY_ACCEPTANCE_CANDIDATE to test an actual patched source file;
otherwise the pinned, complete native source fixtures run without external files.
Only the unrelated container_epoch import is stubbed; acceptance is unmodified.
"""
import base64
import copy
import gzip
import hashlib
import importlib.util
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

# Pinned complete native modules copied read-only from the owned deployed host.
# Compression only keeps this regression artifact portable for offline CI.
BASELINE_GZIP = (
    'H4sIAAAAAAAC/909a3PktpHf51fM8T6E3IzmtH7sxVOWK4pWjrfKm92s1q7UqVQsagajYcQhJyRHlrKl/35oPBtPcrSykzt/8I6I'
    'RqPRaDQa3Q1g3TbbaZ6v9/2+JXk+Lbe7pu2nRV03fdGXTd1NJuLbpug2VXkt/yzrbkeWvfzz711Ty9/bot/I3y2Rv/pySyZraG/Z'
    'VBWtCtjnxfVSNnpWVFVxXZHZ9G2x25X1DYfuH+C3BDqtH2bTdzuoXFQTDjFfkbtySfJyReq+7B8k7LKp+6KsSZuTXbPcTCaTH9/8'
    'fJ6fnp2dv/94+pez8/zi7Ifzt6fTk2lCUczvSd2Uq3lV3pGjYrkku76ol+S/7l4mk/z9h3d//nB+ceGvsmubm5Z0HYe9OP3+PH/7'
    '7vU5hWrJfNlsd2VF0ja5LI7+eQX/Oz765ujq0/Hs1ZePSTbJfzj/W/7qKweaQhVH66tPr75iUD/99Oa1CTOZ0v8Q4B8ej9Tvr+jv'
    'l0dfX6kPX9IPf/imuDa/yN8vv3hMJpmg3W6HNnF69D+c7iv9M58v/vh71o+XX/w3I5HVPhvoeQ41vvkaKkwmy6roumn+mhSrig7V'
    '+f2ubMkq/bCvQV7O27ZpswXr544C6gp/2q9uSM9LVmRNZbisyz7P045U6xn9xPEtlKxcrqum6K+y6dF30780tIRVhf/KtQKflp1V'
    'CP8BynkQI+0qVFE1SEURpgaCsqOzhQlTKtHMptdNU2UGWNNO6bTzQ6dl3c+mrMXMWwsm3bzs1sAGkjJAVR3VyMyutUXZkenPRbXn'
    'vE6Tsr4rqnKlWJJkqGddjDOUEVazEzU8LdnSmUjnsRifbXFfbvfbBa9Ba748nh+zsWEfjMExG/GOUEuo/qol1on+LFqlDZhYjpg6'
    'mm8byrqmLpdphlvU9b49mR77OGYL7MSmpKxTQc1Mo0MMWW7I8pYxwyORjFbNMjRPzqBaFxD7EKY7GN5uMV2Vy/6y69sZ+kX16RWI'
    '8KdHTVuxWqUGhpn6qy62dAZATfWpuV0wWdZf1kVZ0eXEgnuhf1Lp7vFEonByGnEg3yztyXZhEQ50G4OTNLcJpyZtbrOZWbZsVoSW'
    'Jly1k1UCQ93cMrGe5l2xJjmApIL8mYSEmZjDR1oF4XzEEoPmLOvcDPqe0XV0NdVKcb7eVxWdp8sNB7LmIvTwMmElCXSN/fKN5CWM'
    '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'
)
CANDIDATE_GZIP = (
    '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'
)
BASELINE_SHA256 = "3f359f93657f28f5b892005ecf69571c5522429b2399d768b35c0f403479546f"
CANDIDATE_SHA256 = "b8e409b67b2605c89dd451da233e9d1252fbb05df3ae0608c8b0cb153e91d868"
OLD_SOURCE = gzip.decompress(base64.b64decode(BASELINE_GZIP)).decode()
CANDIDATE_PATH = os.environ.get("XENOID_PROXY_ACCEPTANCE_CANDIDATE")
NEW_SOURCE = Path(CANDIDATE_PATH).read_text() if CANDIDATE_PATH else gzip.decompress(base64.b64decode(CANDIDATE_GZIP)).decode()


def load_native(source,name):
    package=types.ModuleType(name);package.__path__=[]
    identity=types.ModuleType(name+'.device_identity')
    identity.container_epoch=lambda *_args:None  # _proxy_checks never calls this host-only helper.
    module=types.ModuleType(name+'.live_observe');module.__package__=name
    module.__file__=name+'-pinned-live_observe.py'
    with patch.dict(sys.modules,{name:package,identity.__name__:identity,module.__name__:module}):
        exec(compile(source,module.__file__,'exec'),module.__dict__)
    return module

OLD=load_native(OLD_SOURCE,'native_proxy_old')
NEW=load_native(NEW_SOURCE,'native_proxy_candidate')
EPOCH='a'*64
GENERATION=7
CAPS={'v4DnsProxy':True,'v4TcpProxy':True,'v4UdpProxy':False,'v6DnsProxy':False,'v6TcpProxy':False,'v6UdpProxy':False}


def fixture(host_count=30,report_count=50):
    counters=lambda count:{'ingress':count,'proxyUplink':count,'egress':count}
    host={'ok':True,'generation':GENERATION,'runtimeEpoch':EPOCH,'phase':'active','quarantined':False,
          'structuralApplied':True,'dataPlaneVerified':False,'counters':counters(host_count)}
    report={'checkId':2,'generation':GENERATION,'phase':'active','structuralApplied':True,'dataPlaneVerified':True,
            'capabilities':copy.deepcopy(CAPS),'counters':counters(report_count)}
    daemon={'bootstrap':{'components':{'proxy':{'ok':True}}},'proxy':{'ok':True,'stateReadable':True,'enabled':True,
        'configured':True,'quarantined':False,'udpAllowed':False,'ipv6Allowed':False,'generation':GENERATION,
        'runtimeEpoch':EPOCH,'checkId':2,'report':report,'probe':{'checkId':2,'capabilities':copy.deepcopy(CAPS)}}}
    snapshot={'components':{'proxy':{'quarantineRequired':True,'engine':host}}}
    return snapshot,daemon


class Host:
    def __init__(self,status,error=None):self.status=copy.deepcopy(status);self.error=error;self.calls=[]
    def proxy_engine_status(self,timeout=None):
        self.calls.append('host_after_daemon_report')
        if self.error:raise self.error
        return copy.deepcopy(self.status)


def inspect(module,snapshot,daemon,host):
    checks=module._Checks()
    module.LiveAcceptance()._proxy_checks(host,copy.deepcopy(snapshot),{'proxyGeneration':GENERATION,'runtimeEpoch':EPOCH},
                                          {},copy.deepcopy(daemon),module._Budget(None),checks)
    return checks


def current_host(count=70):return fixture(host_count=count)[0]['components']['proxy']['engine']


class ProxyCounterAcceptanceTests(unittest.TestCase):
    def test_native_fixtures_match_the_reviewed_source_hashes(self):
        self.assertEqual(hashlib.sha256(OLD_SOURCE.encode()).hexdigest(),BASELINE_SHA256)
        self.assertEqual(hashlib.sha256(NEW_SOURCE.encode()).hexdigest(),CANDIDATE_SHA256)

    def test_old_native_code_deterministically_reproduces_snapshot_race(self):
        snapshot,daemon=fixture(host_count=30,report_count=50)
        host=Host(current_host(70))
        result=inspect(OLD,snapshot,daemon,host)
        self.assertFalse(result.values['proxyDataPlane']['ok'])
        self.assertEqual(result.values['proxyDataPlane']['code'],'proxy_data_plane_unverified')
        self.assertEqual(host.calls,[])  # Current host proof existed but was never sampled.

    def test_candidate_refreshes_host_after_report_and_accepts_monotonic_counters(self):
        snapshot,daemon=fixture(host_count=30,report_count=50)
        host=Host(current_host(70))
        result=inspect(NEW,snapshot,daemon,host)
        self.assertIsNone(result.first_failure())
        self.assertEqual(host.calls,['host_after_daemon_report'])

    def test_counter_reset_never_reuses_older_larger_snapshot_to_pass(self):
        snapshot,daemon=fixture(host_count=100,report_count=50)
        host=Host(current_host(10))
        result=inspect(NEW,snapshot,daemon,host)
        self.assertFalse(result.values['proxyDataPlane']['ok'])
        self.assertEqual(result.values['proxyDataPlane']['code'],'proxy_data_plane_unverified')
        self.assertEqual(host.calls,['host_after_daemon_report'])

    def test_fresh_host_lookup_failure_cannot_fall_back_to_passing_old_snapshot(self):
        snapshot,daemon=fixture(host_count=100,report_count=50)
        host=Host(current_host(),RuntimeError('fixture transport unavailable'))
        acceptance=NEW.LiveAcceptance()
        # Exercise the actual native outer observer's failure handling. Only
        # unrelated host/Google stages are replaced; proxy acceptance is real.
        acceptance._resolve_manager=lambda context:(host,{})
        acceptance._manager_snapshot=lambda manager,budget:snapshot
        acceptance._host_checks=lambda *args:{}
        acceptance._daemon_checks=lambda *args:daemon
        acceptance._google_and_protection_checks=lambda *args:None
        acceptance._component_actions=lambda *args:{}
        result=acceptance.observe({},expected={'proxyGeneration':GENERATION,'runtimeEpoch':EPOCH},
                                  mode='convergence-accept',deadline=None,progress=None)
        self.assertFalse(result['ok'])
        self.assertEqual(result['errorCode'],'live_acceptance_observation_failed')
        self.assertEqual(host.calls,['host_after_daemon_report'])

    def test_real_unverified_report_remains_rejected_with_fresh_counters(self):
        snapshot,daemon=fixture();daemon['proxy']['report']['dataPlaneVerified']=False
        self.assertFalse(inspect(NEW,snapshot,daemon,Host(current_host())).values['proxyDataPlane']['ok'])

    def test_host_generation_change_remains_rejected(self):
        snapshot,daemon=fixture();host=current_host();host['generation']=GENERATION+1
        self.assertFalse(inspect(NEW,snapshot,daemon,Host(host)).values['proxyDataPlane']['ok'])

    def test_host_runtime_epoch_change_remains_rejected(self):
        snapshot,daemon=fixture();host=current_host();host['runtimeEpoch']='b'*64
        self.assertFalse(inspect(NEW,snapshot,daemon,Host(host)).values['proxyDataPlane']['ok'])

    def test_expected_generation_or_runtime_epoch_mismatch_remains_rejected(self):
        snapshot,daemon=fixture();daemon['proxy']['generation']=GENERATION+1
        self.assertIsNotNone(inspect(NEW,snapshot,daemon,Host(current_host())).first_failure())
        snapshot,daemon=fixture();daemon['proxy']['runtimeEpoch']='b'*64
        self.assertIsNotNone(inspect(NEW,snapshot,daemon,Host(current_host())).first_failure())

    def test_check_ids_report_generation_and_capability_proofs_are_still_required(self):
        mutations=(lambda status:status['report'].update(checkId=1),lambda status:status['probe'].update(checkId=1),
                   lambda status:status['report'].update(generation=GENERATION+1),
                   lambda status:status['report']['capabilities'].update(v4TcpProxy=False),
                   lambda status:status['probe']['capabilities'].update(v4DnsProxy=False),
                   lambda status:status.update(checkId=0))
        for mutate in mutations:
            snapshot,daemon=fixture();mutate(daemon['proxy'])
            with self.subTest(mutate=mutate):
                self.assertFalse(inspect(NEW,snapshot,daemon,Host(current_host())).values['proxyDataPlane']['ok'])

    def test_counter_shapes_types_and_values_remain_strict(self):
        mutations=(lambda host:host['counters'].pop('ingress'),lambda host:host['counters'].update(ingress=True),
                   lambda host:host['counters'].update(ingress=70.0),lambda host:host['counters'].update(ingress=-1),
                   lambda host:host['counters'].update(ingress=49),lambda host:host.update(counters={}))
        for mutate in mutations:
            snapshot,daemon=fixture(host_count=100);host=current_host();mutate(host)
            with self.subTest(mutate=mutate):
                self.assertFalse(inspect(NEW,snapshot,daemon,Host(host)).values['proxyDataPlane']['ok'])

    def test_report_and_probe_errors_remain_rejected(self):
        for section in ('report','probe'):
            snapshot,daemon=fixture();daemon['proxy'][section]['errorCode']='fixture_probe_failed'
            with self.subTest(section=section):
                self.assertFalse(inspect(NEW,snapshot,daemon,Host(current_host())).values['proxyDataPlane']['ok'])

    def test_current_host_quarantine_is_not_hidden_by_old_active_snapshot(self):
        snapshot,daemon=fixture(host_count=100);host=current_host();host['quarantined']=True;host['phase']='quarantined'
        result=inspect(NEW,snapshot,daemon,Host(host))
        self.assertFalse(result.values['proxyHostQuarantine']['ok']);self.assertFalse(result.values['proxyDataPlane']['ok'])

    def test_planner_quarantine_intent_does_not_reject_verified_live_proxy(self):
        snapshot,daemon=fixture(host_count=100)
        result=inspect(NEW,snapshot,daemon,Host(current_host(100)))
        self.assertIsNone(result.first_failure())

    def test_no_live_host_api_uses_only_existing_compatible_snapshot_proof(self):
        snapshot,daemon=fixture(host_count=100)
        self.assertIsNone(inspect(NEW,snapshot,daemon,None).first_failure())
        snapshot,daemon=fixture(host_count=30)
        self.assertFalse(inspect(NEW,snapshot,daemon,None).values['proxyDataPlane']['ok'])

    def test_disabled_proxy_needs_current_verified_off_state(self):
        snapshot,daemon=fixture(host_count=100)
        daemon['proxy'].update(enabled=False)
        host=current_host();host.update(phase='off',dataPlaneVerified=True)
        result=inspect(NEW,snapshot,daemon,Host(host))
        self.assertTrue(result.values['proxyDataPlane']['ok'])
        host['dataPlaneVerified']=False
        self.assertFalse(inspect(NEW,snapshot,daemon,Host(host)).values['proxyDataPlane']['ok'])


if __name__=='__main__':unittest.main()
