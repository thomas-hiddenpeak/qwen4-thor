"""Native registry schema and actual C++ values; no synthetic model claim."""
import json
import os
from pathlib import Path
import unittest

from tools.serve_responses.test_contract import errors


class PromptContract(unittest.TestCase):
    def test_closed_mutations_and_revision_bounds(self):
        definition = dict(request=dict(instructions='Answer {{who}}',
            input=[dict(role='user', content='{{question}}')]),
            variables=dict(who='string', question='input_text'))
        self.assertFalse(errors(dict(definition=definition), 'prompt_create'))
        self.assertFalse(errors(dict(definition=definition, expected_revision=1), 'prompt_append'))
        self.assertFalse(errors(dict(default_version='2', expected_revision=1), 'prompt_select'))
        for revision in (None, 0, -1, 1.5, 9007199254740992):
            self.assertTrue(errors(dict(default_version='2', expected_revision=revision), 'prompt_select'))
        self.assertTrue(errors(dict(definition=definition, owner='other'), 'prompt_create'))
        definition['request']['store'] = False
        self.assertTrue(errors(dict(definition=definition), 'prompt_create'))

    def test_official_prompt_reference_typed_variables(self):
        for value in ('42', dict(type='input_text', text='42'),
            dict(type='input_image', image_url='data:image/png;base64,AA==', detail='auto'),
            dict(type='input_file', file_id='file_a')):
            request = dict(prompt=dict(id='prompt_a', version='1', variables=dict(x=value)))
            self.assertFalse(errors(request, 'response_create'))
        self.assertTrue(errors(dict(prompt=dict(id='prompt_a', variables=dict(x=42))), 'response_create'))

    def test_actual_native_resources_response_and_events(self):
        path = Path(os.environ['Q4T_PROMPT_WIRE'])
        checked = 0
        for line in path.read_text().splitlines():
            value = json.loads(line)
            name = {'prompt': 'prompt_resource', 'prompt.version': 'prompt_version',
                    'prompt.deleted': 'prompt_deleted', 'list': 'prompt_list',
                    'response': 'response'}.get(value.get('object'))
            if 'sequence_number' in value:
                name = 'response_event'
            self.assertIsNotNone(name)
            failure = errors(value, name)
            self.assertFalse(failure, [e.message for e in failure][:3])
            checked += 1
        self.assertGreater(checked, 10)
        output = os.environ.get('Q4T_TEST_OUTPUT_DIR')
        if output:
            (Path(output)/'prompt-wire-review.json').write_text(json.dumps(dict(checked=checked))+'\n')


if __name__ == '__main__':
    unittest.main()
