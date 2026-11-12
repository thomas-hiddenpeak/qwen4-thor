"""Validate native parser-produced refusal artifacts against the SDK graph."""
import json
import os
from pathlib import Path
import unittest

from tools.serve_responses.test_contract import errors
from tools.serve_responses import refusal_acceptance as business


class RefusalContract(unittest.TestCase):
    def test_business_cannot_accept_plain_text_or_fabricated_control_output(self):
        message = dict(role='assistant', content=None, refusal='I cannot share that.')
        business.check_refusal(message)
        for bad in (dict(role='assistant', content='Sorry, I cannot share that.'),
                    dict(message, refusal=''), dict(message, refusal=business.MARKER),
                    dict(message, refusal='<q4t_refusal>no</q4t_refusal>'),
                    dict(message, tool_calls=[dict(id='not-executable')])):
            with self.subTest(bad=bad), self.assertRaises(RuntimeError):
                business.check_refusal(bad)

    def test_business_has_six_frozen_generations_and_one_actual_tool_execution(self):
        rows = business.requests()
        self.assertEqual(len(rows), business.LIMITS['chats'])
        self.assertEqual(business.LIMITS['starts'], 1)
        self.assertEqual(business.LIMITS['seconds'], 900)
        for key in ('chat_refusal', 'response_refusal'):
            self.assertNotIn('<q4t_refusal>', json.dumps(rows[key]))
            self.assertNotIn('refusal', rows[key])
        self.assertEqual(rows['tool']['tool_choice'], 'required')
        self.assertEqual(rows['follow']['tool_choice'], 'none')
        self.assertNotIn('previous_response_id', rows['history'])
        self.assertEqual(rows['tool']['tools'][0]['function']['parameters']
                         ['properties']['path']['const'], 'docs/MODEL.md')

    def test_native_json_and_original_events_match_official_shapes(self):
        root = Path(os.environ['Q4T_TEST_OUTPUT_DIR']) / 'responses'
        count = 0
        for ending in ('complete', 'incomplete', 'cancel', 'fail'):
            value = json.loads((root / ('refusal-' + ending + '.json')).read_text())
            self.assertEqual(errors(value, 'response'), [], ending)
            self.assertEqual(value['output'][0]['content'][0], dict(
                type='refusal', refusal='不能提供私有记录✅'))
            events = [json.loads(line[6:]) for line in
                      (root / ('refusal-' + ending + '.wire')).read_text().splitlines()
                      if line.startswith('data: ')]
            self.assertEqual([event['sequence_number'] for event in events],
                             list(range(len(events))))
            for event in events:
                self.assertEqual(errors(event, 'response_event'), [], event['type'])
                count += 1
            self.assertEqual(any(e['type'] == 'response.completed' for e in events),
                             ending == 'complete')
        self.assertGreater(count, 25)

    def test_refusal_fields_and_event_discriminators_are_not_text_aliases(self):
        delta = dict(type='response.refusal.delta', sequence_number=4,
                     item_id='msg_refusal', output_index=0, content_index=0,
                     delta='No private records.')
        self.assertEqual(errors(delta, 'response_event'), [])
        done = {**delta, 'type': 'response.refusal.done'}
        self.assertTrue(errors(done, 'response_event'))
        done['refusal'] = done.pop('delta')
        self.assertEqual(errors(done, 'response_event'), [])
        for key in ('item_id', 'output_index', 'content_index', 'sequence_number'):
            invalid = dict(done)
            invalid.pop(key)
            self.assertTrue(errors(invalid, 'response_event'), key)


if __name__ == '__main__':
    unittest.main()
