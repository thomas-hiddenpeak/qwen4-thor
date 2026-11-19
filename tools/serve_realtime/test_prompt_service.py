"""Prompt recovery evidence counterexamples; no real model qualification."""
import copy
import json
import unittest

from tools.serve_realtime import conversation_managed as managed
from tools.serve_realtime import prompt_service_acceptance as service


class Contract(unittest.TestCase):
    def test_resolved_definition_and_reference_must_both_match(self):
        definition=json.loads((service.ROOT/'tools/serve_realtime/prompt_definitions.json').read_text())['tool']
        reference=dict(id='prompt_cpu_only',version='1',variables={})
        session=dict(type='realtime',prompt=reference,**definition['request'])
        raw=dict(events=[dict(type='session.updated',session=session)])
        original=copy.deepcopy(raw)
        service.check_prompt(raw,reference,definition)
        self.assertEqual(raw,original)
        for field,value in [('prompt',dict(reference,version='2')),
            ('instructions','ignored registry'),('tools',[]),('max_output_tokens',256),
            ('output_modalities',['audio'])]:
            wrong=copy.deepcopy(raw);wrong['events'][0]['session'][field]=value
            with self.assertRaises(RuntimeError):service.check_prompt(wrong,reference,definition)
        wrong=copy.deepcopy(raw);wrong['events']*=2
        with self.assertRaises(RuntimeError):service.check_prompt(wrong,reference,definition)

    def test_fixed_work_includes_controller_owned_model_recovery(self):
        totals=dict(starts=0,chats=0,websocket_sessions=0,seconds=0)
        for scenario,limits in service.LIMITS.items():
            plan=dict(scenario=scenario,prompt_recovery=dict(format='q4t-prompt-recovery-v1'))
            phases=managed.phases(plan)
            self.assertEqual(limits['websocket_sessions'],len(phases))
            self.assertEqual(limits['chats'],limits['starts']+
                sum(3 if mode=='hold_stream' else 2 for _,mode in phases))
            if scenario!='tls_controller':
                for key in totals:totals[key]+=limits[key]
        self.assertEqual(totals,dict(starts=5,chats=18,websocket_sessions=6,seconds=1200))
        with self.assertRaises(RuntimeError):managed.phases(dict(scenario='tls',prompt_recovery=dict(format='unknown')))
        self.assertEqual(len(managed.phases(dict(scenario='tls'))),2)

    def test_cold_controller_usage_requires_real_expired_wait(self):
        usage=dict(phase='active',generation=2,source=None,snapshot=None)
        original=copy.deepcopy(usage)
        value=service.cold_controller_usage(usage,2,160.,160.2)
        self.assertEqual(value['mode'],'full_window_after_unsealed_controller_death')
        self.assertEqual(usage,original)
        for deadline,now in [(None,160.2),(160.,159.),(0.,160.)]:
            with self.assertRaises(RuntimeError):
                service.cold_controller_usage(usage,2,deadline,now)

    def test_cold_controller_cannot_forge_active_generation_or_snapshot(self):
        usage=dict(phase='active',generation=2,source=None,snapshot=None)
        for change in [dict(phase='sealed'),dict(generation=1),dict(source={}),dict(snapshot={})]:
            with self.assertRaises(RuntimeError):
                service.cold_controller_usage(dict(usage,**change),2,160.,160.2)

    def test_controller_supplement_does_not_repeat_gateway_interruption(self):
        plan=dict(scenario='tls_controller',prompt_recovery=dict(format='q4t-prompt-recovery-v1'))
        self.assertEqual(managed.phases(plan),[
            ('controller-before','complete'),('controller-after','complete')])
        self.assertEqual(service.LIMITS['tls_controller'],dict(
            starts=2,chats=6,probes=0,controls=2000,websocket_sessions=2,seconds=360))

    def test_quality_failure_is_not_changed_by_prompt_binding(self):
        definition=json.loads((service.ROOT/'tools/serve_realtime/prompt_definitions.json').read_text())['tool']
        reference=dict(id='prompt_cpu_only',version='1',variables={})
        value=dict(events=[dict(type='session.updated',session=dict(
            prompt=reference,**definition['request']))],
            model_quality=dict(status='FAIL',actual='wrong',expected='rev356'))
        original=copy.deepcopy(value)
        service.check_prompt(value,reference,definition)
        self.assertEqual(value,original)


if __name__=='__main__':unittest.main()
