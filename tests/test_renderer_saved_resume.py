"""Actual native plan/journal parsing with a bounded executor double; no runtime."""
import contextlib
import copy
import dataclasses
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'qa'))
import qa_graphics as g
PREFIX='renderer_resume_fixture54'
package=types.ModuleType(PREFIX);package.__path__=[];sys.modules[PREFIX]=package
lock=types.ModuleType(PREFIX+'.operation_lock')
lock.instance_operation_lock=lambda *a,**k:contextlib.nullcontext()
lock.operation_lock_is_held=lambda *a:True
sys.modules[lock.__name__]=lock
util=types.ModuleType(PREFIX+'.util')
util.bounded_timeout=lambda value=None:value
util.command_timeout=lambda *a:contextlib.nullcontext()
sys.modules[util.__name__]=util
spec=importlib.util.spec_from_file_location(PREFIX+'.convergence',ROOT/'nativebundle/source/src/xenoid/convergence.py')
c=importlib.util.module_from_spec(spec);sys.modules[spec.name]=c;spec.loader.exec_module(c)


class SavedRendererResume(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.root.chmod(0o700)
        self.depth=0;self.run_calls=0;self.observed=[];self.input_changed=False
        @contextlib.contextmanager
        def native_lock(**kwargs):
            self.depth+=1
            try:yield
            finally:self.depth-=1
        self.manager=types.SimpleNamespace(context=types.SimpleNamespace(
            state_root=self.root,instance_id='12345678-1234-1234-1234-123456789abc',resource_tag='0123456789ab',
            instance_name='phone-3',project_root=self.root),
            cfg=types.SimpleNamespace(graphics_renderer='Mali-G78'),instance_operation_lock=native_lock,
            _owned_container_record=lambda **kwargs:({'Id':'c'*64,'State':{'Running':True}},None))
        self.plan=c.ConvergencePlan(resolution='complete',artifact_records=(
            {'target':'glesRenderer','inputSha256':'a'*64,'toolSha256':'b'*64,'outputs':[]},),
            desired_image_input_sha256='d'*64,desired_image_boot_input_sha256='e'*64,
            runtime_action='reuse',image_action='reuse-selected',boot_seed_action='none',daemon_action='reuse',
            deploy_components={'glesRenderer':'deploy','rendererControl':'deploy'},
            identity_action='reuse',location_action='reuse',proxy_action='reuse',keybox_action='reuse',
            camera_action='reuse',google_action='reuse',protection_action='reuse')
        self.observation={'instanceId':self.manager.context.instance_id,
            'desiredImageInputSha256':'d'*64,'desiredImageBootInputSha256':'e'*64,
            'runtime':{'state':'running','containerId':'c'*64,'ownershipValid':True,'integrationIdentityValid':True,
                'storageValid':True,'dataUuid':'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
                'rootfsUuid':'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb','imageInputSha256':'d'*64,'imageBootInputSha256':'e'*64},
            'components':{'protection':{'expectedDigest':'f'*64,'currentDigest':'f'*64,
                'observed':{'ok':True,'engineId':'1'*64}},'proxy':{'generation':1,'enabled':True}},
            'controlPlane':{'ready':True}}
        self.manager.observe_convergence=self.observe
        self.journal=c.ConvergenceJournal.for_manager(self.manager)
        self.create()
        owner=self
        class Executor:
            def __init__(self,manager):
                self.manager=manager
                self.planner=types.SimpleNamespace(input_identity_digest=self.inputs)
            def inputs(self,**kwargs):
                owner.assertGreater(owner.depth,0)
                return 'changed' if owner.input_changed or owner.manager.cfg.graphics_renderer != 'Mali-G78' else c._input_identity_digest(owner.plan)
            def _validate_resume_state(self,saved,observation):
                owner.assertGreater(owner.depth,0)
                c.ConvergenceExecutor._validate_resume_state(self,saved,observation)
            def run(self,**kwargs):
                owner.assertGreater(owner.depth,0);owner.assertEqual(kwargs,{'skip_build':True});owner.run_calls+=1
                owner.journal.advance('accepted');owner.journal.clear()
                return {'ok':True,'resumed':True,'initialPlanDigest':owner.plan.plan_digest,'followUpPlanDigest':None}
        self.api=(Executor,c.ConvergenceJournal,c.ConvergencePlan,c._input_identity_digest,
                  lambda context:copy.copy(self.manager.cfg),lambda cfg:vars(cfg),self.deadline,c._refine_component_actions)

    @contextlib.contextmanager
    def deadline(self,value):
        self.assertEqual(value,7200);self.assertGreater(self.depth,0);yield

    def observe(self,**kwargs):
        self.assertGreater(self.depth,0);self.assertEqual(kwargs,{'skip_build':True})
        return copy.deepcopy(self.observation)

    def create(self,plan=None):
        self.journal.clear()
        if plan is not None:self.plan=plan
        self.journal.create(self.plan,observation=self.observation)
        self.journal.advance('quarantined',proxyQuarantined=True)
        self.journal.advance('components_deployed')
        self.journal.advance('control_ready')
        self.journal.advance('graphics_ready')

    def resume(self):
        with patch.object(g,'resume_api',return_value=self.api),patch.object(g,'other_pending',return_value=False):
            return g.resume_renderer_convergence(self.manager,g.DEFAULT_QA_GRAPHICS,3,'c'*64)

    def rejected(self):
        before=self.journal.path.read_bytes()
        with self.assertRaises(Exception):self.resume()
        self.assertEqual(self.journal.path.read_bytes(),before)
        self.assertEqual(self.run_calls,0);self.assertEqual(self.depth,0)

    def test_exact_interrupted_graphics_ready_resumes_with_same_lock_and_native_owner(self):
        self.assertTrue(self.resume()['resumed_renderer'])
        self.assertEqual(self.run_calls,1);self.assertFalse(self.journal.exists());self.assertEqual(self.depth,0)

    def test_nonrenderer_actions_build_recreate_and_inspect_plans_preserve_journal(self):
        original=self.plan
        for change in [{'runtime_action':'recreate'},{'image_action':'ensure-desired'},
                       {'daemon_action':'reconcile'},{'identity_action':'converge'},{'google_action':'reconcile'},
                       {'artifact_targets':('glesRenderer',)}, {'resolution':'requires-artifacts'},
                       {'deploy_components':{'glesRenderer':'deploy','input':'deploy'}}]:
            with self.subTest(change=change):
                self.create(dataclasses.replace(original,plan_digest='',**change));self.rejected()

    def test_changed_inputs_config_uuid_image_or_cid_never_resume(self):
        self.input_changed=True;self.rejected();self.input_changed=False
        self.manager.cfg.graphics_renderer='different';self.rejected();self.manager.cfg.graphics_renderer='Mali-G78'
        original=copy.deepcopy(self.observation)
        for change in [{'containerId':'9'*64},{'dataUuid':'cccccccc-cccc-cccc-cccc-cccccccccccc'},
                       {'imageInputSha256':'9'*64},{'imageBootInputSha256':'9'*64},{'ownershipValid':False}]:
            with self.subTest(change=change):
                self.observation=copy.deepcopy(original);self.observation['runtime'].update(change);self.rejected()
        self.observation=original

    def test_gen_or_profile_or_receipt_owner_always_wins(self):
        before=self.journal.path.read_bytes()
        with patch.object(g,'other_pending',return_value=True),patch.object(g,'resume_api') as api:
            with self.assertRaises(ValueError):g.resume_renderer_convergence(self.manager,g.DEFAULT_QA_GRAPHICS,3,'c'*64)
            api.assert_not_called()
        self.assertEqual(self.journal.path.read_bytes(),before)

    def test_hidden_live_resolution_mutation_and_regeneration_link_are_rejected(self):
        saved=self.journal.load()
        actions=c._plan_component_actions(self.plan);actions['identity']='converge'
        saved['liveResolution']={'observationSha256':'a'*64,'componentActions':actions,'acceptanceChecks':[]}
        self.journal._write(saved);self.rejected()
        saved['liveResolution']=None;saved['regenerationTransactionId']='a'*32
        self.journal._write(saved);self.rejected()

    def test_new_panel_label_or_native_default_finishes_old_fixed_plan_before_configure(self):
        import qa_resource_runtime as resources
        for label in ['New QA renderer',None]:
            with self.subTest(label=label):
                self.create();self.run_calls=0
                settings={'renderer_label':label}
                epoch={'containerId':'c'*64,'initPid':123,'startedAt':'same-boot','initStartTicks':456}
                original={'ok':True,'schema':'dev.xenoid.renderer/v1','state':'bound','reportedLabel':'Mali-G78',
                          'mapped':True,'owned':True,'readonly':True,'sourceSha256':'a'*64,'zygoteCount':2,'runtime':epoch}
                desired=dict(original,reportedLabel=label,state='native' if label is None else 'bound')
                calls=[]
                def native(upstream,name,arguments,value=None,**kwargs):
                    self.assertFalse(self.journal.exists())
                    self.assertEqual(self.run_calls,1)  # The old executor finished before any new-setting command.
                    self.assertEqual(self.manager.cfg.graphics_renderer,'Mali-G78')
                    calls.append((arguments,value))
                    if arguments==['configure','--json-stdin']:
                        self.assertEqual(value,settings)
                        return {'ok':True},0
                    return (original if len(calls)==1 else desired),0
                with patch.object(g,'resume_api',return_value=self.api),patch.object(g,'other_pending',return_value=False), \
                        patch.object(resources,'native_manager',return_value=self.manager),patch.object(g,'worker_inactive',return_value=True), \
                        patch.object(g,'native_document',side_effect=native),patch.object(g,'source_generation',return_value='same-source'), \
                        patch.object(g,'STATE',self.root/'graphics'),patch.object(g,'OWNER',os.getuid()):
                    result=g.reconcile_one('phone-3',settings)
                self.assertTrue(result['ok'],result)
                self.assertEqual([call[0] for call in calls],[['status'],['configure','--json-stdin'],['status']])

    def test_configuration_changed_since_manager_read_is_rejected_without_executor(self):
        changed=list(self.api)
        changed[4]=lambda context:types.SimpleNamespace(graphics_renderer='new-native-config')
        self.api=tuple(changed)
        self.rejected()

    def test_strict_schema_or_plan_digest_corruption_is_readonly_rejection(self):
        saved=json.loads(self.journal.path.read_text());saved['planDigest']='f'*64
        self.journal.path.write_text(json.dumps(saved));self.rejected()


if __name__=='__main__':unittest.main()
