"""CPU contracts for validating observed Responses producer output and provenance."""
import copy
import unittest

from tools.serve_responses import producer_profiles as profile


def previous():
    return dict(output_items=['message', 'web_search_call'],
                multi_agent=dict(selector='responses_multi_agent=v1', default_max_concurrent_subagents=3,
                                shared_output_budget=True, agent_events=True, actions=['spawn_agent',
                                'interrupt_agent', 'list_agents', 'send_message', 'followup_task', 'wait_agent']))


def observation():
    interpreter = dict(id='resp_interpreter', status='completed', output=[
        dict(type='code_interpreter_call', id='ci_one', status='completed', container_id='cntr_one',
             outputs=[dict(type='logs', logs='603\n')]),
        dict(type='message', content=[dict(type='output_text', text='603')])])
    agents = dict(id='resp_agents', status='completed', output=[
        dict(type='multi_agent_call', action='list_agents', call_id='call_agent'),
        dict(type='multi_agent_call_output', action='list_agents', call_id='call_agent', output=[
            dict(type='output_text', text='{"agents":[{"agent_name":"root","status":"running"}]}')])])
    return dict(container='cntr_one', interpreter=interpreter, agents=agents)


class ProducerProfilesTest(unittest.TestCase):
    def test_additions_preserve_prior_contract_and_selector(self):
        old = previous()
        before = copy.deepcopy(old)
        declared = profile.extended_responses(old)
        self.assertEqual(old, before)
        self.assertEqual(declared['output_items'], ['message','web_search_call','code_interpreter_call'])
        self.assertEqual(declared['multi_agent']['output_items'], list(profile.BETA_TYPES))
        profile.check(declared, observation())
        for key in profile.BETA_TYPES:
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                profile.check(dict(declared, output_items=declared['output_items']+[key]), observation())
        with self.assertRaises(RuntimeError):
            profile.extended_responses(declared)

    def test_both_execution_results_and_request_identity_are_required(self):
        declared = profile.extended_responses(previous())
        for mutate in (
            lambda r: r.update(container='cntr_foreign'),
            lambda r: r['interpreter'].update(id=r['agents']['id']),
            lambda r: r['interpreter']['output'][0].update(outputs=[dict(type='logs', logs='604\n')]),
            lambda r: r['interpreter']['output'][1]['content'][0].update(text='602'),
            lambda r: r['agents'].update(status='in_progress'),
            lambda r: r['agents']['output'][1].update(call_id='call_foreign'),
            lambda r: r['agents']['output'][1].update(output=[dict(type='output_text',text='{"agents":[]}')]),
            lambda r: r['agents']['output'].append(dict(type='unknown_output')),
        ):
            row = observation()
            mutate(row)
            with self.subTest(row=row), self.assertRaises(RuntimeError):
                profile.check(declared, row)


if __name__ == '__main__':
    unittest.main()
