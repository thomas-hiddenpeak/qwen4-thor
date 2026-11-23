"""Check named Responses history independently of the stable SDK schema."""
import json
import unittest

from tools.serve_responses.test_contract import ROOT, errors


class AgentHistoryContract(unittest.TestCase):
    def test_named_history_and_stable_requests(self):
        call = dict(type='multi_agent_call', action='list_agents',
                    call_id='call_1', arguments='{}')
        result = dict(type='multi_agent_call_output', action='list_agents',
                      call_id='call_1', output=[dict(type='output_text',
                      annotations=[], text='37')])
        for name in ('response_create', 'input_tokens_request', 'response_compact'):
            with self.subTest(name=name):
                body = dict(model='qwen3.8-flash-next', input=[call, result])
                self.assertFalse(errors(body, 'agent_history_'+name))
                self.assertTrue(errors(body, name))
                body['input'] = [dict(call, action='other'), result]
                self.assertTrue(errors(body, 'agent_history_'+name))

    def test_selected_header_only_on_responses(self):
        spec = json.loads((ROOT/'docs/serve-openapi-v1.json').read_text())
        advertised = {(path, method) for path, methods in spec['paths'].items()
                      for method, op in methods.items() if isinstance(op, dict)
                      and any(p.get('name') == 'OpenAI-Beta' and p['in'] == 'header'
                              for p in op.get('parameters', []))}
        self.assertEqual(advertised, {
            ('/v1/responses', 'post'), ('/v1/responses/compact', 'post'),
            ('/v1/responses/input_tokens', 'post'),
            ('/v1/responses/{response_id}', 'get'),
            ('/v1/responses/{response_id}', 'delete'),
            ('/v1/responses/{response_id}/cancel', 'post'),
            ('/v1/responses/{response_id}/input_items', 'get')})
