"""Chat speech input schema and original content preservation; no ASR claims."""
import copy
import json
from pathlib import Path
import subprocess
import unittest

from tools.examples import chat_client
from tools.serve_protocol.build_contract import chat_schema, message_schema, vision_schema
from tools.serve_protocol.check_contract import Validator

ROOT = Path(__file__).resolve().parents[2]


def request():
    return dict(messages=[dict(role='user', content=[
        dict(type='text', text='Read this speech'),
        dict(type='input_audio', input_audio=dict(data='aGVhZGVy', format='wav'),
             prompt_cache_breakpoint=dict(mode='explicit', ttl='30m'))])],
        modalities=['text'], audio=None)


class ChatAudio(unittest.TestCase):
    def test_source_shape_and_role_are_consistent_in_all_schemas(self):
        original = request()
        good = [original]
        mp3 = copy.deepcopy(original)
        mp3['messages'][0]['content'][1]['input_audio']['format'] = 'mp3'
        good.append(mp3)
        bad = []
        for role in ('system', 'developer', 'assistant', 'tool'):
            row = copy.deepcopy(original)
            row['messages'][0]['role'] = role
            bad.append(row)
        for key, value in (('format', 'flac'), ('data', ''), ('data', 1),
                           ('url', 'https://example.invalid/voice.wav')):
            row = copy.deepcopy(original)
            row['messages'][0]['content'][1]['input_audio'][key] = value
            bad.append(row)
        for schema in (chat_schema(), message_schema(), vision_schema()):
            validator = Validator(schema)
            for row in good:
                self.assertTrue(validator.is_valid(row), row)
            for row in bad:
                self.assertFalse(validator.is_valid(row), row)
        # Base64/content/codec validity belongs to native preparation.
        self.assertEqual(original, request())

    def test_python_and_javascript_preserve_audio_order_data_and_cache_marker(self):
        row = request()
        original = copy.deepcopy(row)
        prepared = chat_client.with_json_schema(
            chat_client.make_request(row['messages'], []), dict(type='object'))
        self.assertEqual(prepared['messages'][0]['content'][:-1],
                         row['messages'][0]['content'])
        self.assertEqual(prepared['messages'][0]['content'][-1]['type'], 'text')
        script = '''
import {makeRequest, withJsonSchema} from './tools/examples/js/chat_client.mjs';
import fs from 'node:fs';
const messages = JSON.parse(fs.readFileSync(0, 'utf8'));
const value = withJsonSchema(makeRequest(messages), {type:'object'});
process.stdout.write(JSON.stringify(value.messages));
'''
        js = subprocess.run(['node', '--input-type=module', '-e', script],
            cwd=ROOT, input=json.dumps(row['messages']), text=True,
            capture_output=True, timeout=15)
        self.assertEqual(js.returncode, 0, js.stderr)
        messages = json.loads(js.stdout)
        self.assertEqual(messages[0]['content'][:-1], row['messages'][0]['content'])
        self.assertEqual(messages[0]['content'][-1]['type'], 'text')
        self.assertEqual(row, original)


if __name__ == '__main__':
    unittest.main()
