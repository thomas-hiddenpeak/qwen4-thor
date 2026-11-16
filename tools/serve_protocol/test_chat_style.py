"""Published Chat style and streaming contracts, without model execution."""
import copy
import json
import unittest

from jsonschema import Draft202012Validator
from tools.serve_protocol.build_contract import ROOT, chat_schema, vision_schema


class ChatStyleContracts(unittest.TestCase):
    def test_style_null_defaults_and_stream_exclusivity(self):
        base = {'messages': [{'role': 'user', 'content': 'question'}]}
        for name, schema in (('chat', chat_schema()), ('vision', vision_schema())):
            published = json.loads((ROOT / f'docs/serve-{name}-request-v1.schema.json').read_text())
            self.assertEqual(published, schema)
            Draft202012Validator.check_schema(schema)
            validator = Draft202012Validator(schema)
            for effort in (None, 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'):
                for style in (None, 'low', 'medium', 'high'):
                    validator.validate(dict(base, reasoning_effort=effort, verbosity=style))
                    validator.validate(dict(base, chat_template_kwargs=dict(reasoning_effort=effort)))
            for padding in (True, False):
                validator.validate(dict(base, stream=True,
                    stream_options=dict(include_usage=True, include_obfuscation=padding)))
            validator.validate(dict(base, stream_options=None))
            for changed in (dict(base, stream_options={}),
                            dict(base, stream=False, stream_options={'include_usage': False}),
                            dict(base, stream=True, stream_options={'include_obfuscation': None}),
                            dict(base, verbosity='verbose'), dict(base, reasoning_effort='other'),
                            dict(prompt='q', verbosity='low')):
                self.assertFalse(validator.is_valid(changed), changed)
            self.assertEqual(schema['properties']['reasoning_effort']['default'], 'medium')

    def test_padded_stream_schema_keeps_usage_and_terminal_shape(self):
        schema = json.loads((ROOT / 'docs/serve-transport-v1.schema.json').read_text())
        validator = Draft202012Validator(dict(schema, **{'$ref': '#/$defs/chunk'}))
        event = dict(id='style', object='chat.completion.chunk', created=42, model='qwen',
            choices=[dict(index=0, delta=dict(content='资料'), finish_reason=None, logprobs=None)],
            usage=None, obfuscation='a1' * 64)
        validator.validate(event)
        unpadded = copy.deepcopy(event)
        del unpadded['obfuscation']
        validator.validate(unpadded)
        for bad in (None, 1, '', 'x' * 257):
            self.assertFalse(validator.is_valid(dict(event, obfuscation=bad)))
        terminal = dict(event, choices=[], usage=dict(prompt_tokens=10,
            completion_tokens=2, total_tokens=12))
        del terminal['obfuscation']
        validator.validate(terminal)


if __name__ == '__main__':
    unittest.main()
