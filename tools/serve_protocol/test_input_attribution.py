"""Targeted request/wire/client contracts and bounded acceptance oracles."""
import copy
import io
import json
import unittest

from jsonschema import Draft202012Validator
from tools.serve_protocol import input_attribution_acceptance as a
from tools.serve_protocol.build_contract import ROOT, chat_schema, vision_schema
from tools.serve_responses.test_contract import SCHEMA, errors, resolve
from chat_client import read_sse


def frames(events):
    return b''.join(b'data: ' + json.dumps(e).encode() + b'\r\n\r\n'
                    for e in events) + b'data: [DONE]\r\n\r\n'


def chunks():
    common = dict(id='attribution-test', object='chat.completion.chunk',
                  created=42, model='qwen', service_tier='default')
    return [dict(common, choices=[dict(index=0, delta=dict(role='assistant', content='42'),
                                      finish_reason=None, logprobs=None)], usage=None),
            dict(common, choices=[dict(index=0, delta={}, finish_reason='stop', logprobs=None)],
                 usage=None),
            dict(common, choices=[], usage=dict(prompt_tokens=8, completion_tokens=1, total_tokens=9))]


class InputAttribution(unittest.TestCase):
    def test_chat_and_vision_public_input_boundaries(self):
        for name, schema in [('chat', chat_schema()), ('vision', vision_schema())]:
            self.assertEqual(schema, json.loads(
                (ROOT / f'docs/serve-{name}-request-v1.schema.json').read_text()))
            validator = Draft202012Validator(schema)
            base = dict(messages=[dict(role='user', content='q')])
            for tier in (None, 'auto', 'default', 'flex', 'scale', 'priority', 'fast'):
                for identifier in (None, '', '用' * 128, '😀' * 128):
                    validator.validate(dict(base, service_tier=tier, safety_identifier=identifier))
            for identifier in ('用' * 129, '😀' * 129, True, {}):
                self.assertFalse(validator.is_valid(dict(base, safety_identifier=identifier)))
            for tier in ('ultrafast', 'unknown', True, {}):
                self.assertFalse(validator.is_valid(dict(base, service_tier=tier)))

    def test_responses_limit_and_medium_default(self):
        for reasoning in (None, {}, dict(effort=None), dict(effort='xhigh')):
            self.assertFalse(errors(dict(input='q', reasoning=reasoning,
                                         safety_identifier='用' * 128), 'response_create'))
        for bad in ('用' * 129, False, []):
            self.assertTrue(errors(dict(input='q', safety_identifier=bad), 'response_create'))
        properties = resolve(SCHEMA['$defs']['response_create'])['properties']
        self.assertEqual(properties['reasoning']['default'], dict(effort='medium'))

    def test_actual_tier_wire_schema_and_existing_python_client(self):
        schema = json.loads((ROOT / 'docs/serve-transport-v1.schema.json').read_text())
        validator = Draft202012Validator(dict(schema, **{'$ref': '#/$defs/chunk'}))
        events = chunks()
        for event in events:
            validator.validate(event)
        value = read_sse(io.BytesIO(frames(events)))
        self.assertEqual(value.message['content'], '42')
        self.assertEqual(value.usage['total_tokens'], 9)
        self.assertEqual(a.chat_wire(frames(events), True)['actual_tier'], 'default')
        for field, bad in [('service_tier', 'invented'), ('service_tier', 1)]:
            self.assertFalse(validator.is_valid(dict(events[0], **{field: bad})))

    def test_acceptance_rejects_echoed_preference_private_data_and_missing_usage(self):
        for index in range(3):
            changed = chunks()
            changed[index]['service_tier'] = 'fast'
            with self.assertRaises(Exception):
                a.chat_wire(frames(changed), True)
        for name in ('safety_identifier', 'request_attribution'):
            changed = chunks()
            changed[0][name] = 'private'
            with self.assertRaises(Exception):
                a.chat_wire(frames(changed), True)
        with self.assertRaises(Exception):
            a.chat_wire(frames(chunks()[:-1]), True)

    def test_frozen_business_budget_and_real_tool_history(self):
        self.assertEqual(a.LIMITS, dict(starts=1, chats=6, controls=2000, seconds=900))
        rows = a.requests()
        self.assertEqual(len(rows), 6)
        self.assertNotIn('reasoning', rows['response_default'])
        self.assertIsNone(rows['response_null']['reasoning'])
        self.assertTrue(rows['response_null']['stream'])
        self.assertEqual(rows['tool']['tool_choice'], 'required')
        self.assertEqual(rows['follow']['tool_choice'], 'none')
        self.assertEqual(rows['follow']['response_format']['json_schema']['schema']['required'],
                         ['title', 'citation', 'sha256'])
        self.assertNotIn('function_call_output', json.dumps(rows))
        self.assertNotEqual(a.expected_digest('same', 'client:a'),
                            a.expected_digest('same', 'client:b'))
        self.assertEqual(len(a.expected_digest(a.IDENTIFIER)), 64)


if __name__ == '__main__':
    unittest.main()
