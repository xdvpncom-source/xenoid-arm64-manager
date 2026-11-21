"""SDK readiness must use the current native host engine, not cached Android reports."""
import contextlib
import copy
import importlib.util
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

QA=Path(__file__).resolve().parents[1]/'qa'
sys.path.insert(0,str(QA))
spec=importlib.util.spec_from_file_location('sdk_proxy_live_state_review',QA/'script_host.py')
h=importlib.util.module_from_spec(spec);spec.loader.exec_module(h)
CID='a'*64
EPOCH='current-runtime-epoch'
CACHED={'ok':True,'stateReadable':True,'enabled':True,'configured':True,'quarantined':False,
        'mustBlock':True,'generation':3,'runtimeEpoch':EPOCH,
        'report':{'phase':'active','structuralApplied':True,'dataPlaneVerified':True,
                  'runtimeEpoch':EPOCH,'generation':3},
        'agent':{'ready':True,'phase':'ready','runtimeEpoch':EPOCH}}
LIVE={'ok':True,'phase':'active','structuralApplied':True,'quarantined':False,
      'runtimeEpoch':EPOCH,'generation':3,'dataPlaneVerified':False}
DEAD={'ok':False,'code':'ipc_unavailable','error':'ipc_unavailable'}


class SDKLiveProxyStateTests(unittest.TestCase):
    def fixture(self,status=None,live=None):
        logs=[]
        client=SimpleNamespace(proxy_status=Mock(return_value=copy.deepcopy(CACHED if status is None else status)),
                               proxy_source=Mock(),proxy_enabled=Mock())
        manager=SimpleNamespace(_owned_container_record=Mock(return_value=({'Id':CID},None)),
            daemon_client=Mock(return_value=client),proxy_engine_status=Mock(return_value=copy.deepcopy(LIVE if live is None else live)),
            reconcile_proxy_desired=Mock(return_value={'ok':True}),proxy_quarantine=Mock())
        broker=h.Broker(manager,lambda *entry:logs.append(entry),config={'expected_container_id':CID,'command_timeout':0})
        broker.display_raw_docker=Mock(side_effect=lambda *_args,**_kwargs:contextlib.nullcontext())
        return broker,manager,client,logs

    def assert_no_replacement_or_source_mutation(self,manager,client):
        client.proxy_source.assert_not_called();client.proxy_enabled.assert_not_called()
        manager.proxy_quarantine.assert_not_called()

    def test_healthy_fastpath_reads_live_host_without_mutating_desired_source(self):
        broker,manager,client,_=self.fixture()
        result=broker.display_proxy_reconcile(manager,None)
        self.assertTrue(result['ready']);self.assertTrue(result['live_host_ready'])
        self.assertFalse(result['reconciled']);manager.proxy_engine_status.assert_called()
        manager.reconcile_proxy_desired.assert_not_called()
        self.assert_no_replacement_or_source_mutation(manager,client)

    def test_cached_success_with_dead_host_reconciles_only_via_native_api_and_rechecks(self):
        broker,manager,client,_=self.fixture()
        manager.proxy_engine_status.side_effect=[copy.deepcopy(DEAD),copy.deepcopy(LIVE)]
        result=broker.display_proxy_reconcile(manager,None)
        self.assertTrue(result['ready']);self.assertTrue(result['reconciled'])
        manager.reconcile_proxy_desired.assert_called_once_with()
        self.assertTrue(result['live_host_checked']);self.assertTrue(result['live_host_ready'])
        self.assert_no_replacement_or_source_mutation(manager,client)

    def test_native_success_with_still_dead_host_is_never_ready(self):
        broker,manager,client,_=self.fixture(live=DEAD)
        result=broker.display_proxy_reconcile(manager,None)
        self.assertFalse(result['ready']);self.assertFalse(result['ok'])
        self.assertTrue(result['reconciled']);self.assertFalse(result['live_host_ready'])
        manager.reconcile_proxy_desired.assert_called_once_with()
        self.assert_no_replacement_or_source_mutation(manager,client)

    def test_native_failure_is_preserved_despite_cached_success(self):
        broker,manager,client,_=self.fixture(live=DEAD)
        manager.reconcile_proxy_desired.return_value={'ok':False,'code':'proxy_check_timeout'}
        result=broker.display_proxy_reconcile(manager,None)
        self.assertFalse(result['ready']);self.assertEqual(result['code'],'proxy_check_timeout')
        self.assert_no_replacement_or_source_mutation(manager,client)

    def test_wrong_generation_epoch_phase_structure_or_quarantine_never_fastpath(self):
        for mutation in ({'generation':4},{'runtimeEpoch':'previous-runtime'}, {'phase':'quarantined'},
                         {'structuralApplied':False},{'quarantined':True},{'ok':False}):
            with self.subTest(mutation=mutation):
                broker,manager,client,_=self.fixture(live={**LIVE,**mutation})
                result=broker.display_proxy_reconcile(manager,None)
                self.assertFalse(result['ready']);self.assertTrue(result['reconciled'])
                manager.reconcile_proxy_desired.assert_called_once_with()

    def test_host_dying_between_readiness_checks_cannot_be_reported_ready(self):
        broker,manager,client,_=self.fixture()
        manager.proxy_engine_status.side_effect=[copy.deepcopy(LIVE),copy.deepcopy(DEAD)]
        result=broker.display_proxy_reconcile(manager,None)
        self.assertFalse(result['ready']);self.assertFalse(result['live_host_ready'])
        manager.reconcile_proxy_desired.assert_not_called()

    def test_raw_host_does_not_need_to_fabricate_data_plane_verification(self):
        broker,manager,client,_=self.fixture(live={**LIVE,'dataPlaneVerified':False})
        result=broker.display_proxy_reconcile(manager,None)
        self.assertTrue(result['ready'])
        self.assertFalse(manager.proxy_engine_status.return_value['dataPlaneVerified'])
        self.assertTrue(client.proxy_status.return_value['report']['dataPlaneVerified'])

    def test_missing_cached_binding_fields_cannot_match_vacuously(self):
        for key in ('generation','runtimeEpoch'):
            with self.subTest(key=key):
                status=copy.deepcopy(CACHED);status.pop(key)
                broker,manager,client,_=self.fixture(status=status)
                result=broker.display_proxy_reconcile(manager,None)
                self.assertFalse(result['ready'])

    def test_invalid_cached_generation_and_epoch_types_cannot_claim_current_binding(self):
        for key,value in [('generation',True),('generation',None),('generation','3'),('runtimeEpoch',''),('runtimeEpoch',None)]:
            with self.subTest(key=key,value=value):
                status=copy.deepcopy(CACHED);status[key]=value
                broker,manager,client,_=self.fixture(status=status)
                result=broker.display_proxy_reconcile(manager,None)
                self.assertFalse(result['ready'])

    def test_enabled_but_unreadable_or_unconfigured_status_is_not_ready(self):
        for mutation in ({'stateReadable':False},{'configured':False}):
            with self.subTest(mutation=mutation):
                broker,manager,client,_=self.fixture(status={**CACHED,**mutation})
                result=broker.display_proxy_reconcile(manager,None)
                self.assertFalse(result['ready'])

    def test_disabled_or_unconfigured_proxy_preserves_legacy_no_mutation_path(self):
        for configured in (False,True):
            with self.subTest(configured=configured):
                broker,manager,client,_=self.fixture(status={'ok':True,'stateReadable':True,'enabled':False,'configured':configured,'quarantined':False})
                result=broker.display_proxy_reconcile(manager,None)
                self.assertTrue(result['ready']);self.assertFalse(result['enabled'])
                manager.proxy_engine_status.assert_not_called();manager.reconcile_proxy_desired.assert_not_called()

    def test_force_reconcile_requires_current_host_proof_after_native_success(self):
        broker,manager,client,_=self.fixture(live=DEAD)
        result=broker.display_proxy_reconcile(manager,None,force=True)
        self.assertFalse(result['ready']);self.assertTrue(result['reconciled'])
        manager.reconcile_proxy_desired.assert_called_once_with();manager.proxy_engine_status.assert_called()

    def test_runtime_identity_change_is_fatal_before_native_mutation(self):
        broker,manager,client,_=self.fixture(live=DEAD)
        manager._owned_container_record.return_value=({'Id':'b'*64},None)
        with self.assertRaises(h.RuntimeIdentityError):broker.display_proxy_reconcile(manager,None)
        manager.reconcile_proxy_desired.assert_not_called();manager.proxy_engine_status.assert_not_called()

    def test_stop_and_expired_deadline_do_not_start_recovery(self):
        broker,manager,client,_=self.fixture(live=DEAD);broker.stopping.set()
        self.assertFalse(broker.display_proxy_reconcile(manager,None)['ready'])
        manager.reconcile_proxy_desired.assert_not_called();manager.proxy_engine_status.assert_not_called()
        broker.stopping.clear()
        with patch.object(h.time,'monotonic',return_value=100):result=broker.display_proxy_reconcile(manager,99)
        self.assertFalse(result['ready']);self.assertEqual(result['code'],'proxy_recovery_timeout')
        manager.reconcile_proxy_desired.assert_not_called();manager.proxy_engine_status.assert_not_called()

    def test_finite_budget_flows_into_native_context_without_source_or_input_fallback(self):
        broker,manager,client,_=self.fixture()
        manager.proxy_engine_status.side_effect=[copy.deepcopy(DEAD),copy.deepcopy(LIVE)]
        budget=Mock(side_effect=lambda _value:contextlib.nullcontext())
        with patch.dict(sys.modules,{'xenoid.util':SimpleNamespace(command_timeout=budget)}),patch.object(h.time,'monotonic',return_value=10):
            result=broker.display_proxy_reconcile(manager,50)
        self.assertTrue(result['ready']);budget.assert_called()
        self.assertTrue(all(0<call.args[0]<=40 for call in budget.call_args_list))
        self.assert_no_replacement_or_source_mutation(manager,client)

    def test_arbitrary_native_exception_text_never_leaks_credentials(self):
        import json
        broker,manager,client,logs=self.fixture()
        manager.proxy_engine_status.side_effect=RuntimeError('https://username:private-password@proxy.example')
        result=broker.display_proxy_reconcile(manager,None)
        self.assertFalse(result['ready'])
        self.assertNotIn('private-password',json.dumps([result,logs]))


if __name__=='__main__':unittest.main()
