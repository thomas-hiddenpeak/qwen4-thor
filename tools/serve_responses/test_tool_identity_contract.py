"""Native namespace artifacts preserve the pinned SDK's public identities."""
import json
import os
from pathlib import Path
import unittest

from tools.serve_responses.test_contract import errors


class ToolIdentityContract(unittest.TestCase):
    def test_native_async_outputs_and_events_match_official_shapes(self):
        root = Path(os.environ['Q4T_TEST_OUTPUT_DIR']) / 'responses'
        for name in ('async-function', 'async-custom'):
            value = json.loads((root / (name + '.json')).read_text())
            self.assertEqual([(list(e.path), e.message[:120])
                              for e in errors(value, 'response')], [])
            events = [json.loads(line[6:])
                      for line in (root / (name + '.wire')).read_text().splitlines()
                      if line.startswith('data: ')]
            self.assertEqual([x['sequence_number'] for x in events], list(range(len(events))))
            for event in events:
                self.assertEqual([(list(e.path), e.message[:120])
                                  for e in errors(event, 'response_event')], [])
            self.assertTrue(value['output'][0]['async'])
            self.assertEqual(value['output'][0]['caller'], dict(type='direct'))

    def test_native_namespace_json_and_events_match_official_shapes(self):
        root = Path(os.environ['Q4T_TEST_OUTPUT_DIR']) / 'responses'
        events_seen = 0
        for name in ('namespace', 'namespace-incomplete',
                     'custom-projection-complete', 'custom-projection-incomplete',
                     'custom-projection-fail', 'custom-projection-cancel'):
            response = json.loads((root / (name + '.json')).read_text())
            issues = errors(response, 'response')
            self.assertEqual([(list(e.path), e.message[:100]) for e in issues], [])
            if name.startswith('namespace'):
                self.assertEqual([(x['namespace'], x['name']) for x in response['output']],
                                 [('crm', 'read'), ('crm', 'query')])
            wire = (root / (name + '.wire')).read_text()
            self.assertNotIn('q4t_ns_', wire)
            events = [json.loads(line[6:]) for line in wire.splitlines()
                      if line.startswith('data: ')]
            self.assertEqual([x['sequence_number'] for x in events], list(range(len(events))))
            for event in events:
                issues = errors(event, 'response_event')
                self.assertEqual([(list(e.path), e.message[:100]) for e in issues], [],
                                 event['type'])
                events_seen += 1
        self.assertGreater(events_seen, 16)
        for name, shape in [('custom-conversation', 'conversation_item'),
                            ('custom-input-list', 'input_items_list'),
                            ('custom-conversation-list', 'conversation_items')]:
            value = json.loads((root / (name + '.json')).read_text())
            issues = errors(value, shape)
            self.assertEqual([(list(e.path), e.message[:100]) for e in issues], [], name)


if __name__ == '__main__':
    unittest.main()
