"""Focused publication checks against native HTTP operations and SDK wire shapes."""
import json
import unittest

from jsonschema import Draft202012Validator
from tools.serve_protocol import build_contract
from tools.serve_responses.build_contract import standalone_openapi

ROOT = build_contract.ROOT


class VoiceContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = build_contract.openapi()
        cls.paths = cls.spec['paths']
        cls.defs = json.loads((ROOT/'docs/serve-application-v1.schema.json').read_text())['$defs']

    def test_live_http_offer_and_answer_are_independent_of_socket_upgrade(self):
        ops = self.paths['/v1/live/sessions']
        self.assertEqual(set(ops), {'get','post'})
        side = self.paths['/v1/live/sessions/{session_id}/attach']['get']
        self.assertIn('101',side['responses'])
        self.assertEqual([p['name'] for p in side['parameters']],['session_id','graceful_close'])
        self.assertEqual(set(ops['post']['responses']), {'201','default'})
        body = ops['post']['requestBody']['content']['application/json']['schema']
        reply = ops['post']['responses']['201']['content']['application/json']['schema']

        def valid(schema, value):
            return Draft202012Validator({'$defs':self.defs, '$ref':'#'+schema['$ref'].split('#',1)[1]}).is_valid(value)

        transport = dict(type='webrtc', sdp='v=0')
        self.assertTrue(valid(body, dict(session=dict(model='native'),transport=transport)))
        self.assertTrue(valid(reply, dict(session=dict(id='live_id'),transport=transport)))
        for session, media in [(dict(id='live_id'), transport),
                               (dict(model='native'), dict(type='sip',sdp='v=0')),
                               (dict(model='native',audio=dict(format=dict(type='audio/pcm',rate=24000))), transport)]:
            self.assertFalse(valid(body, dict(session=session,transport=media)))

    def test_decision_question_bounds_and_wire_results(self):
        def valid(value, name):
            return Draft202012Validator({'$ref':'#/$defs/decisions_'+name,
                                         '$defs':self.defs}).is_valid(value)
        value = dict(model='qwen3.8-flash-next',input='retained record',questions=[
            dict(type='predicate',instructions='This project serves language models.')])
        self.assertTrue(valid(value,'request'))
        for changes in (dict(questions=[]),dict(questions=value['questions']*401),
                        dict(questions=[dict(type='choice',instructions='Select.',choices=[dict(value='A')])]),
                        dict(input=42),dict(unknown=True)):
            self.assertFalse(valid(dict(value,**changes),'request'))
        self.assertIn('/$defs/decisions_decision',self.paths['/v1/decisions']['post']
            ['responses']['200']['content']['application/json']['schema']['$ref'])

    def test_audio_bytes_multipart_and_upgrade_are_distinct(self):
        speech = self.paths['/v1/audio/speech']['post']
        validate = Draft202012Validator(speech['requestBody']['content']['application/json']['schema']).is_valid
        base = dict(input='hello',model='tts',voice='vivian')
        self.assertTrue(validate(dict(base,speed=4,stream_format='sse')))
        self.assertFalse(validate(dict(base,speed=4.01)))
        self.assertFalse(validate(dict(base,voice={})))
        types = speech['responses']['200']['content']
        self.assertEqual(set(types),{'audio/wav','audio/pcm','audio/mpeg','audio/ogg','audio/flac','audio/aac','text/event-stream'})
        consent = self.paths['/v1/audio/voice_consents']['post']['requestBody']['content']
        self.assertEqual(set(consent),{'multipart/form-data'})
        self.assertIn('recording',consent['multipart/form-data']['schema']['required'])
        recorded = self.paths['/v1/live/sessions/{session_id}/content']['get']
        self.assertEqual(recorded['parameters'][0]['name'],'session_id')
        self.assertEqual(set(recorded['responses']['200']['content']),{'audio/wav'})
        for path in ('/v1/realtime','/v1/live/sessions'):
            self.assertEqual(set(self.paths[path]['get']['responses']),{'101','default'})
        self.assertFalse(any('?' in p for p in self.paths))

    def test_shipped_contract_pair_and_option_are_current(self):
        self.assertEqual(json.loads((ROOT/'docs/serve-openapi-v1.json').read_text()),self.spec)
        bundled = json.loads((ROOT/'docs/serve-openapi-bundle-v1.json').read_text())
        self.assertEqual(bundled,standalone_openapi(self.spec))
        cfg = bundled['components']['schemas']['serve-config-v1.schema.json']
        option = cfg['properties']['application_state']['properties']['live_recordings']
        validator = Draft202012Validator(option)
        self.assertTrue(validator.is_valid(False))
        self.assertFalse(validator.is_valid(1))

    def test_stored_audio_projection_and_dangling_component(self):
        message = dict(id='msg_1', role='user', name=None, content=None,
                       content_parts=[dict(type='input_audio', input_audio=dict(
                           data='YQ==', format='wav'))])
        validator = Draft202012Validator({'$defs':self.defs,
            '$ref':'#/$defs/stored_chat_message'})
        self.assertTrue(validator.is_valid(message))
        message['content_parts'][0]['input_audio']['format'] = 'unknown'
        self.assertFalse(validator.is_valid(message))
        with self.assertRaisesRegex(ValueError, 'unresolved bundled reference'):
            standalone_openapi({'$ref':'serve-application-v1.schema.json#/$defs/absent'})
