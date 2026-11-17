"""Validate native CPU captures and strict web schemas; no model invocation."""
import copy
import io
import json
import os
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator
from tools.examples import chat_client
from tools.serve_protocol.build_contract import chat_schema
from tools.serve_protocol.check_contract import Validator
from tools.serve_responses.test_contract import errors

ROOT = Path(__file__).resolve().parents[2]
CAPTURES = Path(os.environ['Q4T_WEB_API_OUTPUT'])


def events(name):
    return [json.loads(line[6:]) for line in
            (CAPTURES / (name + '.wire')).read_text().splitlines()
            if line.startswith('data: ') and line[6:] != '[DONE]']


class WebContract(unittest.TestCase):
    def test_native_responses_json_sse_and_sources(self):
        for name in ('response', 'response-included'):
            response = json.loads((CAPTURES / (name + '.json')).read_text())
            self.assertFalse(errors(response, 'response'))
            wire = events(name)
            for event in wire:
                self.assertFalse(errors(event, 'response_event'), event['type'])
            self.assertEqual([e['sequence_number'] for e in wire], list(range(len(wire))))
            self.assertEqual(wire[-1]['response'], response)
            self.assertEqual('sources' in response['output'][0]['action'], name.endswith('included'))
            self.assertNotIn('VERIFIED_PRIVATE_PASSAGE', json.dumps(wire))

    def test_chat_schema_and_current_web_fields(self):
        request = dict(messages=[dict(role='user', content='Find alpha')],
                       web_search_options=dict(search_context_size='low',
                           user_location=dict(type='approximate', approximate=dict(country='CN'))))
        validator = Validator(chat_schema())
        self.assertTrue(validator.is_valid(request))
        request['web_search_options']['external_web_access'] = False
        self.assertFalse(validator.is_valid(request))
        tool = dict(type='web_search', filters=dict(allowed_domains=['example.com'],
            blocked_domains=['blocked.example.com']), return_token_budget='unlimited',
            search_content_types=['text', 'image'], image_settings=dict(max_results=2, caption=True))
        self.assertFalse(errors(dict(model='cpu', input='alpha', tools=[tool]), 'response_create'))
        tool['search_content_types'] = ['text']
        self.assertTrue(errors(dict(model='cpu', input='alpha', tools=[tool]), 'response_create'))

    def test_optional_config_is_redacted_and_strict(self):
        value = json.loads((CAPTURES / 'config.json').read_text())
        schema = json.loads((ROOT / 'docs/serve-config-v1.schema.json').read_text())
        validator = Draft202012Validator(schema)
        validator.validate(value)
        self.assertTrue(value['web_deployment']['configured'])
        self.assertFalse(value['web_deployment']['core_readiness_dependency'])
        self.assertNotIn('example.com', json.dumps(value))
        invalid = copy.deepcopy(value)
        invalid['web_deployment']['core_readiness_dependency'] = True
        self.assertFalse(validator.is_valid(invalid))
        for path, field, invalid_value in (
                ('limits', 'tunnel_poll_connections', 5),
                ('limits', 'tunnel_response_connections', -1),
                ('limits', 'tunnel_body_bytes', 0)):
            invalid = copy.deepcopy(value)
            invalid[path][field] = invalid_value
            self.assertFalse(validator.is_valid(invalid))
        invalid = copy.deepcopy(value)
        invalid['file_inputs']['video']['sample_frames'] = 17
        self.assertFalse(validator.is_valid(invalid))

    def test_python_client_json_stream_and_bad_offsets(self):
        raw = (CAPTURES / 'chat.json').read_bytes()
        finite = chat_client.read_json(io.BytesIO(raw))
        stream = chat_client.read_sse(io.BytesIO((CAPTURES / 'chat.wire').read_bytes()))
        self.assertEqual(stream.message, finite.message)
        self.assertEqual(len(stream.message['annotations']), 1)
        invalid = json.loads(raw)
        invalid['choices'][0]['message']['annotations'][0]['url_citation']['end_index'] = 100000
        with self.assertRaises(chat_client.ProtocolError):
            chat_client.read_json(io.BytesIO(json.dumps(invalid).encode()))
        # Unicode scalars, including a non-BMP character, count as one each.
        message = dict(role='assistant', content='中文🙂 source', annotations=[dict(
            type='url_citation', url_citation=dict(url='https://example.com/',
            title='source', start_index=4, end_index=10))])
        self.assertEqual(chat_client._message(message), message)
        message['annotations'][0]['url_citation']['end_index'] = 11
        with self.assertRaises(chat_client.ProtocolError):
            chat_client._message(message)


if __name__ == '__main__':
    unittest.main()
