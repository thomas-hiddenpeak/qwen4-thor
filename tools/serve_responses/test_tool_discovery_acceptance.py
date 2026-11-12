"""SDK schema and failure-sensitive oracles for real tool discovery short runs."""
import copy
import json
import os
from pathlib import Path
import unittest

from tools.serve_responses import tool_discovery_acceptance as driver
from tools.serve_responses.test_contract import errors


class ToolDiscoveryAcceptance(unittest.TestCase):
    def artifact(self):
        root = Path(os.environ['Q4T_TEST_OUTPUT_DIR']) / 'responses'
        response = json.loads((root / 'discovery-parser.json').read_text())
        events = [json.loads(line[6:]) for line in
                  (root / 'discovery-parser.wire').read_text().splitlines()
                  if line.startswith('data: ')]
        return response, events

    def test_requests_match_sdk_and_frozen_output_capacity(self):
        self.assertEqual(driver.LIMITS, dict(starts=1, chats=10, controls=2000, seconds=900))
        self.assertEqual(driver.Stage.max_output_tokens, 256)
        rows = driver.requests()
        for name, row in rows.items():
            self.assertEqual([(list(e.path), e.message[:120])
                              for e in errors(row, 'response_create')], [], name)
            self.assertEqual(row['max_output_tokens'], 256)
        self.assertFalse(rows['server_function']['stream'])
        self.assertTrue(rows['server_custom']['stream'])
        self.assertEqual(rows['client_search']['tools'][0]['execution'], 'client')
        self.assertEqual(rows['additional']['tools'], [])
        self.assertEqual(rows['additional']['input'][0]['role'], 'developer')

    def test_actual_cpp_parser_and_executor_match_sdk_wire_shapes(self):
        response, events = self.artifact()
        self.assertEqual(errors(response, 'response'), [])
        self.assertEqual([e['sequence_number'] for e in events], list(range(len(events))))
        self.assertEqual(events[-1]['type'], 'response.completed')
        for event in events:
            self.assertEqual(errors(event, 'response_event'), [], event['type'])
        definitions = response['output'][1]['tools']
        driver.search_items(response, server=True, definitions=definitions)
        self.assertEqual([x['type'] for x in response['output']],
                         ['tool_search_call', 'tool_search_output'])

    def test_search_oracle_rejects_wrong_identity_definitions_and_order(self):
        original, _ = self.artifact()
        definitions = original['output'][1]['tools']
        for index, key, value in [(0, 'call_id', 'client_call'),
                                  (0, 'status', 'incomplete'),
                                  (0, 'arguments', dict(paths=['unauthorized'])),
                                  (1, 'tools', []),
                                  (1, 'execution', 'client')]:
            changed = copy.deepcopy(original)
            changed['output'][index][key] = value
            with self.assertRaises(RuntimeError):
                driver.search_items(changed, server=True, definitions=definitions)
        changed = copy.deepcopy(original)
        changed['output'].reverse()
        with self.assertRaises(RuntimeError):
            driver.search_items(changed, server=True, definitions=definitions)
        client = copy.deepcopy(original)
        client['output'] = [dict(client['output'][0], execution='client',
                                 call_id='actual_client_id', arguments=dict(query='project'))]
        driver.search_items(client, server=False)
        client['output'].append(original['output'][1])
        with self.assertRaises(RuntimeError):
            driver.search_items(client, server=False)

    def test_stream_oracle_rejects_missing_done_and_function_adapter_leak(self):
        response, events = self.artifact()
        call = dict(type='custom_tool_call', id='ct_fixture', call_id='call_fixture',
                    name='raw', namespace='project', input='read docs/MODEL.md')
        response['output'].append(call)
        driver.streamed_search(events, response)
        bad = copy.deepcopy(events)
        search_id = response['output'][0]['id']
        bad = [e for e in bad if not (e['type'] == 'response.output_item.done' and
                                      e['item']['id'] == search_id)]
        for i, event in enumerate(bad):
            event['sequence_number'] = i
        with self.assertRaises(RuntimeError):
            driver.streamed_search(bad, response)
        bad = copy.deepcopy(events)
        bad.append(dict(type='response.function_call_arguments.done',
                        item_id=search_id, sequence_number=len(bad)))
        with self.assertRaises(RuntimeError):
            driver.streamed_search(bad, response)
        response['output'].insert(0, response['output'].pop())
        with self.assertRaises(RuntimeError):
            driver.streamed_search(events, response)


if __name__ == '__main__':
    unittest.main()
