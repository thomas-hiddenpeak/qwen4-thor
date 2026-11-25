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

    def test_audio_output_requires_matching_modalities_and_options(self):
        text = dict(messages=[dict(role='user', content='Read the result')])
        options = dict(format='pcm16', voice='ryan')
        for schema in (chat_schema(), vision_schema()):
            check = Validator(schema)
            for modes in (['audio'], ['text', 'audio'], ['audio', 'text']):
                for voice in ('ryan', {'id': 'voice_example'}):
                    check.validate(dict(text, modalities=modes, audio=dict(options, voice=voice)))
            for modes in (None, ['text']):
                check.validate(dict(text, modalities=modes, audio=None))
            for fields in [dict(audio=options), dict(modalities=['audio']),
                           dict(modalities=['audio'], audio=None),
                           dict(modalities=['audio', 'audio'], audio=options),
                           dict(modalities=['audio'], audio=dict(options, format='pcm')),
                           dict(modalities=['audio'], audio=dict(options, voice={})),
                           dict(modalities=['audio'], audio=dict(options, voice='a\0b'))]:
                self.assertFalse(check.is_valid(dict(text, **fields)), fields)

    def test_assistant_audio_id_round_trips_without_repeating_content(self):
        audio = dict(id='audio_original')
        for schema in (chat_schema(), vision_schema(), message_schema()):
            check = Validator(schema)
            for content in ({}, {'content': None}, {'content': 'Earlier result'}):
                check.validate(dict(messages=[dict(role='assistant', audio=audio, **content)]))
            bad = [
                dict(role='user', content='hello', audio=audio),
                dict(role='assistant', audio=dict(audio, transcript='unexpected')),
                dict(role='assistant', audio=dict(id='audio_')),
                dict(role='assistant', audio=dict(id='audio_hello\n')),
                dict(role='assistant', audio=None),
            ]
            for message in bad:
                self.assertFalse(check.is_valid(dict(messages=[message])), message)

    def test_transport_checks_completed_audio_and_individual_stream_fragments(self):
        transport = json.loads((ROOT/'docs/serve-transport-v1.schema.json').read_bytes())
        check = lambda name, field: Validator(
            transport['$defs'][name]['properties']['choices']['items']['properties']
            [field]['properties']['audio'])
        complete = check('completion', 'message')
        delta = check('chunk', 'delta')
        audio = dict(id='audio_result', data='YQ==', transcript='hello', expires_at=1700000000)
        complete.validate(audio)
        for key in audio:
            delta.validate({key: audio[key]})
            self.assertFalse(complete.is_valid({k: v for k, v in audio.items() if k != key}))
        for invalid in ({}, {'bogus': 1}, {'expires_at': 0}, {'data': 4}, {'transcript': []}):
            self.assertFalse(delta.is_valid(invalid))

    def test_only_the_stream_expiry_choice_omits_finish_reason(self):
        transport = json.loads((ROOT/'docs/serve-transport-v1.schema.json').read_bytes())
        schema = transport['$defs']['chunk']['properties']['choices']['items']
        check = Validator(dict(schema, **{'$defs': transport['$defs']}))
        expiry = dict(index=0, delta={'audio': {'expires_at': 1700000000}}, logprobs=None)
        ordinary = dict(index=0, delta={'audio': {'data': 'YQ=='}}, logprobs=None)
        check.validate(expiry)
        check.validate(dict(ordinary, finish_reason=None))
        self.assertFalse(check.is_valid(dict(expiry, finish_reason=None)))
        self.assertFalse(check.is_valid(ordinary))

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
