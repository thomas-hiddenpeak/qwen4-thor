"""Real pinned SDK against CPU mock transport; never GPU/business evidence."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools.native_tts import voice_sdk as sdk

AVAILABLE = all(importlib.util.find_spec(name) for name in ('openai', 'httpx2'))


@unittest.skipUnless(AVAILABLE, 'run with the separately pinned voice SDK environment')
class VoiceSdk(unittest.TestCase):
    def setUp(self):
        import httpx2
        import openai
        self.assertEqual(openai.__version__, sdk.VERSION)
        self.http = httpx2
        root = Path(__file__).resolve().parents[2]/'.q4t-work/voice-sdk-cpu'
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temp.cleanup)
        self.audio = Path(self.temp.name)/'audio.wav'
        self.audio.write_bytes(b'RIFF-cpu-input-not-model-evidence')
        self.clip = dict(path=str(self.audio), sha256=hashlib.sha256(self.audio.read_bytes()).hexdigest())
        self.consent = dict(id='cons_actual', object='audio.voice_consent',
                            name='test', language='en', created_at=1)
        self.voice = dict(id='voice_actual', object='audio.voice',
                          name='test', type='audio_sample', created_at=1)

    def call(self, action, body, response, status=200, audio=False):
        observed = []
        def handle(request):
            observed.append(request)
            return self.http.Response(status, json=response)
        config = dict(action=action, request=body, base_url='https://127.0.0.1:18490/v1',
                      api_key='test-credential')
        if audio: config['audio'] = self.clip
        result, wire, raw = sdk.execute(config, transport=self.http.MockTransport(handle))
        self.assertEqual(len(observed), 1)
        self.assertEqual(len(result['exchanges']), 1)
        self.assertEqual(json.loads(wire), response)
        self.assertEqual(raw, observed[0].content)
        return result, observed[0]

    def test_actual_consent_and_voice_methods_send_original_multipart(self):
        result, request = self.call('consent_create', dict(name='test', language='en'),
                                    self.consent, audio=True)
        self.assertTrue(result['ok']); self.assertEqual(result['result']['id'], 'cons_actual')
        self.assertEqual(request.url.path, '/v1/audio/voice_consents')
        self.assertIn(b'name="recording"', request.content)
        self.assertIn(self.audio.read_bytes(), request.content)
        result, request = self.call('voice_create', dict(name='test', consent='cons_actual',
                                   type='audio_sample'), self.voice, audio=True)
        self.assertTrue(result['ok']); self.assertEqual(result['result']['id'], 'voice_actual')
        self.assertEqual(request.url.path, '/v1/audio/voices')
        self.assertIn(b'name="audio_sample"', request.content)
        self.assertIn(b'cons_actual', request.content)

    def test_metadata_methods_use_actual_ids_and_verb_without_auto_pagination(self):
        for action, body, response, method, path in (
                ('consent_retrieve', dict(consent_id='cons_actual'), self.consent,
                 'GET', '/v1/audio/voice_consents/cons_actual'),
                ('consent_update', dict(consent_id='cons_actual', name='renamed'),
                 dict(self.consent, name='renamed'), 'POST', '/v1/audio/voice_consents/cons_actual'),
                ('consent_list', dict(limit=1), dict(object='list', data=[self.consent],
                 has_more=True, first_id='cons_actual', last_id='cons_actual'),
                 'GET', '/v1/audio/voice_consents'),
                ('consent_delete', dict(consent_id='cons_actual'),
                 dict(id='cons_actual', object='audio.voice_consent', deleted=True),
                 'DELETE', '/v1/audio/voice_consents/cons_actual'),
                ('voice_delete', dict(voice_id='voice_actual'),
                 dict(id='voice_actual', object='audio.voice', deleted=True),
                 'DELETE', '/v1/audio/voices/voice_actual')):
            result, request = self.call(action, body, response)
            self.assertTrue(result['ok']); self.assertEqual((request.method, request.url.path),(method,path))

    def test_error_statuses_preserve_wire_and_do_not_retry(self):
        for status, code in ((400,'voice_consent_mismatch'),(404,'object_not_found'),
                             (429,'client_tpm_exhausted'),(503,'capacity_exhausted')):
            response = dict(error=dict(message='CPU negative',type='invalid_request_error',
                                       param=None,code=code))
            result, _ = self.call('consent_retrieve', dict(consent_id='cons_actual'),response,status)
            self.assertFalse(result['ok']);self.assertEqual(result['error']['status'],status)
            self.assertEqual(result['exchanges'][0]['status'],status)

    def test_speech_and_chat_streams_use_sdk_iterators_and_preserve_wire(self):
        payload = b'data: {"type":"speech.audio.delta","audio":"YQ=="}\n\ndata: {"type":"speech.audio.done"}\n\n'
        stream = self.http.MockTransport(lambda _:self.http.Response(200,
                    headers={'content-type':'text/event-stream'},content=payload))
        config = dict(action='speech',base_url='https://127.0.0.1:18490/v1',api_key='test',
            request=dict(model='base',input='Hello',voice=dict(id='voice_actual'),
                         response_format='wav',stream_format='sse'))
        result, wire, _ = sdk.execute(config,transport=stream)
        self.assertTrue(result['ok']);self.assertEqual(wire,payload)
        event=dict(id='chatcmpl_cpu',object='chat.completion.chunk',created=1,model='cpu',
                   choices=[dict(index=0,delta=dict(content='Hello'),finish_reason='stop')])
        payload=('data: '+json.dumps(event)+'\n\ndata: [DONE]\n\n').encode()
        config.update(action='chat',request=dict(model='cpu',messages=[dict(role='user',content='x')],stream=True))
        stream=self.http.MockTransport(lambda _:self.http.Response(200,
                    headers={'content-type':'text/event-stream'},content=payload))
        result,wire,_=sdk.execute(config,transport=stream)
        self.assertTrue(result['ok']);self.assertEqual(wire,payload)
        self.assertEqual(result['result']['events'][0]['choices'][0]['delta']['content'],'Hello')

    def test_asr_has_no_prompt_or_language_and_hash_failure_precedes_network(self):
        result,request=self.call('transcribe',dict(model='asr',response_format='json'),
                                  dict(text='hello'),audio=True)
        self.assertTrue(result['ok'])
        for name in (b'name="prompt"',b'name="language"',b'name="text"'):
            self.assertNotIn(name,request.content)
        handler=mock.Mock()
        config=dict(action='transcribe',base_url='https://127.0.0.1/v1',api_key='test',
                    request=dict(model='asr'),audio=dict(self.clip,sha256='0'*64))
        with self.assertRaisesRegex(ValueError,'identity drift'):
            sdk.execute(config,transport=self.http.MockTransport(handler))
        handler.assert_not_called()

    def test_response_bound_and_external_endpoint_reject_without_retry(self):
        config=dict(action='consent_retrieve',base_url='https://127.0.0.1/v1',api_key='test',
                    request=dict(consent_id='cons_actual'))
        with mock.patch.object(sdk,'RESPONSE_LIMIT',16):
            result,wire,_=sdk.execute(config,transport=self.http.MockTransport(
                lambda _:self.http.Response(200,content=b'x'*17)))
        self.assertFalse(result['ok']);self.assertEqual(len(result['exchanges']),1)
        self.assertEqual(wire,b'')
        handler=mock.Mock()
        with self.assertRaisesRegex(ValueError,'loopback'):
            sdk.execute(dict(config,base_url='https://api.openai.com/v1'),
                        transport=self.http.MockTransport(handler))
        handler.assert_not_called()

    def test_chat_extensions_are_sent_in_original_json_for_both_modes(self):
        for stream in (False, True):
            body=dict(model='cpu',messages=[dict(role='user',content='x')],
                enable_thinking=False,request_id='owned-cpu',cancel_token='owned-token',
                stream=stream,audio=dict(voice=dict(id='voice_actual'),format='wav'),
                modalities=['text','audio'])
            event=dict(id='chatcmpl_cpu',object='chat.completion.chunk' if stream else 'chat.completion',
                created=1,model='cpu',choices=[dict(index=0,finish_reason='stop',
                    **({'delta':dict(content='Hello')} if stream else
                       {'message':dict(role='assistant',content='Hello')}))])
            observed=[]
            def handle(request):
                observed.append(json.loads(request.content))
                if stream:
                    return self.http.Response(200,headers={'content-type':'text/event-stream'},
                        content=('data: '+json.dumps(event)+'\n\ndata: [DONE]\n\n').encode())
                return self.http.Response(200,json=event)
            result,_,_=sdk.execute(dict(action='chat',request=body,
                base_url='https://127.0.0.1/v1',api_key='cpu'),
                transport=self.http.MockTransport(handle))
            self.assertTrue(result['ok']);self.assertEqual(observed,[body])
            self.assertEqual(len(result['exchanges']),1)


if __name__ == '__main__':
    unittest.main()
