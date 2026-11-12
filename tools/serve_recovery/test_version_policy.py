"""Root fault contracts; registration/proofs are explicit CPU fixtures only."""
import copy
import unittest
import uuid

from tools.serve_recovery import version_policy as policy


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.boot = str(uuid.uuid4())
        self.registry = dict(format=policy.REGISTRY_FORMAT, cycle_id=str(uuid.uuid4()),
            root_manifest_sha256='0'*64, compatibility_sha256='1'*64,
            qualification_sha256='2'*64, payloads=dict(
                primary=dict(manifest_sha256='a'*64,binding_sha256='b'*64),
                fallback=dict(manifest_sha256='c'*64,binding_sha256='d'*64)))
        self.state = policy.initial(self.registry,self.boot)
        self.event = dict(role='model', generation=5, boot_id=self.boot,
            payload_sha256='a'*64, kind='native_process_signal', signal=11, fingerprint='e'*64)
        self.proof = dict(desired='RUNNING', boot_id=self.boot, payload_sha256='a'*64,
            generations=dict(controller=2,model=5,gateway=3), termination_requested=False,
            **{k:True for k in policy.GUARDS})
        self.request = str(uuid.uuid4())

    def pending(self):
        return policy.observe(self.state,self.event,self.proof,self.request)

    def fallback_proof(self):
        return dict(self.proof,payload_sha256='c'*64,
                    generations=dict(controller=3,model=6,gateway=4))

    def test_first_independent_code_failure_persists_one_intent_without_launch_authority(self):
        value=self.pending();policy.transition(self.state,value)
        self.assertEqual(value['selected'],'primary')
        self.assertEqual(policy.launch_payload(value),'fallback')
        self.assertEqual(value['switch']['request_id'],self.request)
        self.assertEqual(value['switch']['generations'],self.proof['generations'])
        self.assertEqual(value['faults'],[self.event])
        description=policy.describe(value)
        self.assertTrue(description['fallback_consumed'])
        self.assertFalse(description['model_actions'])
        self.assertFalse(description['role_launch_authority'])

    def test_auxiliary_unknown_resource_device_and_owned_signals_cannot_select_version(self):
        for kind in ('NVML_UNAVAILABLE','LOG_UNAVAILABLE','METRICS_UNAVAILABLE',
            'UNKNOWN_MODEL_EXIT','RESOURCE_EXHAUSTED','DEVICE_FAILURE','PLE_UNAVAILABLE',
            'UNCONFIRMED_STALL','EXECUTION_DEADLINE','STATE_UNAVAILABLE'):
            event=dict(self.event,kind=kind,signal=0)
            self.assertEqual(policy.assess(self.state,event,self.proof)['reason'],'not_version_fault')
            with self.assertRaises(ValueError):policy.observe(self.state,event,self.proof,self.request)
        for signal in (0,9,15):
            self.assertFalse(policy.assess(self.state,dict(self.event,signal=signal),self.proof)['accepted'])
        for guard in policy.GUARDS:
            self.assertEqual(policy.assess(self.state,self.event,dict(self.proof,**{guard:False}))
                             ['reason'],'independent_safety_proof_missing')
        self.assertEqual(policy.assess(self.state,self.event,
            dict(self.proof,termination_requested=True))['reason'],'owned_termination')

    def test_stale_payload_boot_and_role_generations_are_not_fault_evidence(self):
        for event in (dict(self.event,payload_sha256='f'*64),
                      dict(self.event,boot_id=str(uuid.uuid4())),
                      dict(self.event,generation=4)):
            self.assertEqual(policy.assess(self.state,event,self.proof)['reason'],'stale_version_fault')
        for role in policy.ROLES:
            event=dict(self.event,role=role,generation=self.proof['generations'][role])
            value=policy.observe(self.state,event,self.proof,self.request)
            policy.transition(self.state,value)

    def test_stop_intent_dominates_pending_and_resume_never_replenishes_cycle(self):
        value=self.pending();after=policy.resume(value,str(uuid.uuid4()))
        policy.transition(value,after)
        for key in ('registry','switch','faults','selected','completed'):
            self.assertEqual(after[key],value[key])
        event=dict(self.event,boot_id=after['boot_id'],payload_sha256='c'*64,generation=6)
        proof=dict(self.fallback_proof(),boot_id=after['boot_id'])
        for desired in ('STOPPED','MAINTENANCE'):
            stopped=dict(proof,desired=desired)
            self.assertEqual(policy.assess(after,event,stopped)['reason'],'operator_intent')
            with self.assertRaises(ValueError):policy.finish(after,stopped,'d'*64)
        self.assertTrue(policy.describe(after)['fallback_consumed'])

    def test_completion_requires_entire_new_tree_and_exact_independent_binding(self):
        value=self.pending();proof=self.fallback_proof()
        for role in policy.ROLES:
            stale=dict(proof,generations=dict(proof['generations'],
                       **{role:self.proof['generations'][role]}))
            with self.assertRaises(ValueError):policy.finish(value,stale,'d'*64)
        with self.assertRaises(ValueError):policy.finish(value,proof,'f'*64)
        for guard in policy.GUARDS:
            with self.assertRaises(ValueError):policy.finish(value,dict(proof,**{guard:False}),'d'*64)
        result=policy.finish(value,proof,'d'*64);policy.transition(value,result)
        self.assertEqual(result['selected'],'fallback')
        self.assertEqual(result['switch'],value['switch'])
        self.assertFalse(policy.describe(result)['pending'])

    def test_fallback_failure_before_or_after_readiness_latches_protection(self):
        value=self.pending();proof=self.fallback_proof()
        event=dict(self.event,payload_sha256='c'*64,generation=6)
        for previous in (value,policy.finish(value,proof,'d'*64)):
            protected=policy.observe(previous,event,proof,str(uuid.uuid4()))
            policy.transition(previous,protected)
            self.assertEqual(protected['switch'],value['switch'])
            self.assertTrue(policy.describe(protected)['protected'])
            resumed=policy.resume(protected,str(uuid.uuid4()))
            self.assertEqual(resumed['terminal_fault'],event)
            self.assertEqual(policy.assess(protected,event,proof)['reason'],'version_recovery_exhausted')
            with self.assertRaises(ValueError):policy.finish(protected,proof,'d'*64)

    def test_runtime_cannot_clear_budget_replace_registration_or_retarget_intent(self):
        value=self.pending();candidate=policy.resume(value,self.boot)
        for mutate in (lambda v:v.update(switch=None,faults=[]),
                       lambda v:v['registry'].update(cycle_id=str(uuid.uuid4())),
                       lambda v:v['switch'].update(request_id=str(uuid.uuid4())),
                       lambda v:v['switch']['generations'].update(controller=0),
                       lambda v:v.update(faults=[])):
            bad=copy.deepcopy(candidate);mutate(bad)
            with self.assertRaises((ValueError,KeyError,TypeError,IndexError)):
                policy.transition(value,bad)
        self.assertFalse(policy.assess(value,self.event,self.proof)['accepted'])

    def test_execution_contract_fault_and_boolean_aliases_have_exact_types(self):
        event=dict(self.event,kind='execution_contract_failure',signal=0)
        value=policy.observe(self.state,event,self.proof,self.request)
        policy.transition(self.state,value)
        for bad in (dict(event,generation=True),dict(event,signal=False)):
            with self.assertRaises(ValueError):policy.assess(self.state,bad,self.proof)
        with self.assertRaises(ValueError):
            policy.assess(self.state,event,dict(self.proof,identity_valid=1))


if __name__=='__main__':unittest.main()
