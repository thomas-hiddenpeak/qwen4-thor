"""Independent wire and typed SDK result oracles for the bounded driver."""
import copy
import json
import unittest

from tools.serve_protocol import chat_style_acceptance as a
from tools.serve_protocol.js_client import _choices_completion


class DriverContracts(unittest.TestCase):
    def test_wire_padding_and_terminal_negative_controls(self):
        def encode(events):
            return b''.join(b'data: '+json.dumps(e).encode()+b'\r\n\r\n'
                            for e in events)+b'data: [DONE]\r\n\r\n'
        events = [dict(choices=[dict(index=0, finish_reason=None)], usage=None),
                  dict(choices=[dict(index=0, finish_reason='stop')], usage=None),
                  dict(choices=[], usage=dict(prompt_tokens=10, completion_tokens=1, total_tokens=11))]
        a.check_padding(encode(events), False)
        padded = copy.deepcopy(events)
        for item in padded[:-1]:
            item['obfuscation'] = 'a'
            item['obfuscation'] += '1' * ((-len(json.dumps(item).encode())) % 128)
        a.check_padding(encode(padded), True)
        for changed in (events[:-1], events+[events[-1]], [events[1], events[1], events[2]]):
            with self.assertRaises(Exception):
                a.check_padding(encode(changed), False)
        with self.assertRaises(Exception):
            a.check_padding(encode(padded), False)
        with self.assertRaises(Exception):
            a.check_padding(encode(events), True)

    def test_actual_bridge_multichoice_type_and_frozen_budget(self):
        usage = dict(prompt_tokens=8, completion_tokens=2, total_tokens=10)
        choices = [dict(index=i, message=dict(role='assistant', content='42'),
                        finish_reason='stop', logprobs=None) for i in range(2)]
        actual = _choices_completion('id', 'm', choices, usage)
        self.assertIsInstance(actual, a.MultiCompletion)
        a.check_answer(actual)
        choices[1]['message']['content'] = '41'
        with self.assertRaises(Exception):
            a.check_answer(_choices_completion('id', 'm', choices, usage))
        self.assertEqual(a.LIMITS, dict(starts=1, chats=6, controls=2000, seconds=900))
        rows = a.requests()
        self.assertEqual(len(rows), 6)
        self.assertNotIn('reasoning_effort', rows['default'])
        self.assertIsNone(rows['ordinary']['reasoning_effort'])
        self.assertEqual(rows['tool']['reasoning_effort'], 'none')
        self.assertNotIn('include_obfuscation', rows['tool']['stream_options'])
        self.assertFalse(rows['follow']['stream_options']['include_obfuscation'])


if __name__ == '__main__':
    unittest.main()
