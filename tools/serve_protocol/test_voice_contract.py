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
        reject = self.paths['/v1/live/sessions/{session_id}/reject']['post']
        self.assertNotIn('content', reject['responses']['200'])
        rejection = reject['requestBody']['content']['application/json']['schema']
        self.assertEqual(rejection['$ref'], 'serve-application-v1.schema.json#/$defs/live_session_reject')
        for code in (300, 486, 699):
            self.assertTrue(valid(rejection, {'status_code':code}))
        for value in ({}, {'status_code':299}, {'status_code':700}, {'status_code':486.5},
                      {'status_code':'486'}, {'status_code':486,'extra':True}):
            self.assertFalse(valid(rejection, value))
        accepted = self.paths['/v1/live/sessions/{session_id}/accept']['post']
        schema = accepted['requestBody']['content']['application/json']['schema']
        self.assertTrue(valid(schema, {'session':{'model':'native','type':'live'}}))
        self.assertFalse(valid(schema, {'session':{'model':'native','type':'realtime'}}))
        self.assertNotIn('content', accepted['responses']['200'])
        for target in ('/v1/live/sessions/{session_id}/hangup', '/v1/realtime/calls/{call_id}/hangup'):
            self.assertNotIn('requestBody', self.paths[target]['post'])
            self.assertNotIn('content', self.paths[target]['post']['responses']['200'])
        for target in ('/v1/live/sessions/{session_id}/refer', '/v1/realtime/calls/{call_id}/refer'):
            op = self.paths[target]['post']
            self.assertNotIn('content', op['responses']['200'])
            transfer_body = op['requestBody']['content']['application/json']['schema']
            self.assertTrue(valid(transfer_body, {'target_uri':'tel:+12025550100'}))
            for uri in ('', '123', 'sip:', 'sip:a b', 'sips:a%0', 'tel:1\n', 'x:'+'a'*8191):
                self.assertFalse(valid(transfer_body, {'target_uri':uri}))
        rtc = self.paths['/v1/realtime/calls/{call_id}/reject']['post']
        self.assertFalse(rtc['requestBody']['required'])
        rejection = rtc['requestBody']['content']['application/json']['schema']
        self.assertEqual(rejection['$ref'], 'serve-application-v1.schema.json#/$defs/realtime_call_reject')
        for value in (None, {}, {'status_code':603}, {'status_code':300}):
            self.assertTrue(valid(rejection, value))
        for value in ([], {'status_code':None}, {'status_code':299}, {'status_code':700}, {'extra':True}):
            self.assertFalse(valid(rejection, value))
        rt_call = self.paths['/v1/realtime/calls/{call_id}/accept']['post']
        body_call = rt_call['requestBody']['content']['application/json']['schema']
        self.assertTrue(valid(body_call, {'type':'realtime', 'audio':{'input':{'transcription':None}}}))
        self.assertFalse(valid(body_call, {'session':{'type':'live'}}))
        self.assertFalse(valid(body_call, {'type':'realtime', 'id':'sess_other'}))
        self.assertIn('call_id', {p['name'] for p in self.paths['/v1/realtime']['get']['parameters']})
        fork = self.paths['/v1/live/sessions/{session_id}/fork']
        self.assertEqual(set(fork['get']['responses']), {'101','default'})
        self.assertEqual(set(fork['post']['responses']), {'201','default'})
        fork_body = fork['post']['requestBody']['content']['application/json']['schema']
        self.assertTrue(valid(fork_body, dict(transport=transport)))
        self.assertTrue(valid(fork_body, dict(transport=transport,session={'store':False})))
        self.assertFalse(valid(fork_body, dict(transport=transport,session={'model':'other'})))
        self.assertTrue(valid(body, dict(session=dict(model='native'),transport=transport)))
        self.assertTrue(valid(reply, dict(session=dict(id='live_id'),transport=transport)))
        for session, media in [(dict(id='live_id'), transport),
                               (dict(model='native'), dict(type='sip',sdp='v=0')),
                               (dict(model='native',audio=dict(format=dict(type='audio/pcm',rate=24000))), transport)]:
            self.assertFalse(valid(body, dict(session=session,transport=media)))

    def test_realtime_webrtc_raw_and_configured_offers(self):
        op = self.paths['/v1/realtime/calls']['post']
        body = op['requestBody']['content']
        self.assertEqual(set(body), {'application/sdp','multipart/form-data'})
        reply = op['responses']['201']
        self.assertTrue(reply['headers']['Location']['required'])
        self.assertEqual(reply['content']['application/sdp'], body['application/sdp'])
        form = body['multipart/form-data']
        self.assertEqual(form['schema']['required'], ['sdp'])
        self.assertEqual(form['encoding']['session']['contentType'], 'application/json')
        validator = Draft202012Validator({'$defs':self.defs, '$ref':'#/$defs/realtime_session'})
        self.assertTrue(validator.is_valid({'type':'realtime','audio':{'input':{'transcription':None}}}))
        for invalid in ({'session':{'type':'realtime'}},{'type':'live'},None):
            self.assertFalse(validator.is_valid(invalid))

    def test_realtime_client_secret_request_and_response_definitions(self):
        def valid(root, value):
            return Draft202012Validator({'$defs':self.defs, '$ref':'#/$defs/realtime_client_secret_'+root}).is_valid(value)
        self.assertTrue(valid('create',{}))
        for seconds in (10, 600, 7200):
            self.assertTrue(valid('create',{'expires_after':{'anchor':'created_at','seconds':seconds},
                'session':{'type':'transcription','audio':{'input':{'transcription':None}}}}))
        for seconds in (9, 7201, 10.5, '600', None):
            self.assertFalse(valid('create',{'expires_after':{'seconds':seconds}}))
        session = {'id':'sess_test','object':'realtime.session','type':'realtime'}
        self.assertTrue(valid('created', {'session':session,'expires_at':1000,'value':'ek_test'}))
        self.assertFalse(valid('created', {'session':session,'value':'ek_test'}))

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
