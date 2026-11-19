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
            for key in totals:totals[key]+=limits[key]
        self.assertEqual(totals,dict(starts=5,chats=18,websocket_sessions=6,seconds=1200))
        with self.assertRaises(RuntimeError):managed.phases(dict(scenario='tls',prompt_recovery=dict(format='unknown')))
        self.assertEqual(len(managed.phases(dict(scenario='tls'))),2)

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
