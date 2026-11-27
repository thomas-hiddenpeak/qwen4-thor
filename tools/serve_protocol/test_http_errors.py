"""Standard HTTP recovery errors on inference operations and their SSE boundary."""
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator


class HttpErrors(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[2] / 'docs/serve-openapi-bundle-v1.json'
        cls.document = json.loads(path.read_bytes())
        cls.validator = Draft202012Validator(cls.document)

    def test_media_and_vector_header_contracts(self):
        paths = self.document['paths']
        store = '/v1/vector_stores/{vector_store_id}'
        targets = [('get', '/v1/live/sessions/{session_id}/content', '503'),
                   ('delete', store, '409'), ('post', store+'/file_batches', '409'),
                   ('post', store+'/file_batches/{batch_id}/cancel', '409'),
                   ('post', store+'/files', '409'), ('post', store+'/files/{file_id}', '409'),
                   ('delete', store+'/files/{file_id}', '409')]
        for method, path, code in targets:
            with self.subTest(method=method, path=path):
                replies = paths[path][method]['responses']
                reply = replies[code]
                self.assertEqual(reply['content'], replies['default']['content'])
                self.assertEqual(reply['headers']['X-Request-Id'], replies['default']['headers']['X-Request-Id'])
                header = reply['headers']['Retry-After']
                self.assertFalse(header.get('required', False))
                delay = self.validator.evolve(schema=header['schema'])
                self.assertTrue(delay.is_valid(1))
                for invalid in (0, -1, 1.5, True):
                    self.assertFalse(delay.is_valid(invalid))
                self.assertNotIn('Retry-After', replies['200'].get('headers', {}))
        speech = paths['/v1/audio/speech']['post']['responses']['200']['headers']['Transfer-Encoding']
        self.assertFalse(speech.get('required', False))
        encoding = self.validator.evolve(schema=speech['schema'])
        self.assertTrue(encoding.is_valid('chunked'))
        self.assertFalse(encoding.is_valid('gzip'))
        self.assertNotIn('409', paths[store+'/search']['post']['responses'])

    def test_event_type_catalog_bearer_challenge(self):
        responses = self.document['paths']['/v1/webhook_event_types']['get']['responses']
        self.assertEqual(responses['401']['headers']['X-Request-Id'],
                         responses['default']['headers']['X-Request-Id'])
        challenge = responses['401']['headers']['WWW-Authenticate']
        self.assertTrue(challenge['required'])
        self.validator.evolve(schema=challenge['schema']).validate('Bearer realm="q4t"')
        self.assertNotIn('WWW-Authenticate', responses['200'].get('headers', {}))
        error = responses['401']['content']['application/json']['schema']
        for kind in ('authentication_error', 'gateway_error'):
            self.validator.evolve(schema=error).validate({'error': {
                'message': 'Authentication required', 'code': 'invalid_api_key',
                'type': kind, 'param': None}})

    def test_inference_retry_headers_and_bodies(self):
        for route in ('audio/speech', 'audio/transcriptions', 'audio/translations',
                      'chat/completions', 'completions', 'embeddings', 'moderations',
                      'responses', 'responses/compact'):
            responses = self.document['paths']['/v1/' + route]['post']['responses']
            for code, native, service in [('429', 'gateway_error', 'rate_limit_error'),
                                          ('503', 'service_error', 'service_unavailable_error')]:
                with self.subTest(route=route, status=code):
                    response = responses[code]
                    replay = response['headers']['X-Should-Retry']
                    self.assertFalse(replay.get('required', False))
                    replay_hint = self.validator.evolve(schema=replay['schema'])
                    self.assertTrue(replay_hint.is_valid('false'))
                    self.assertFalse(replay_hint.is_valid('true'))
                    header = response['headers']['Retry-After']
                    self.assertFalse(header.get('required', False))
                    delay = self.validator.evolve(schema=header['schema'])
                    self.assertTrue(delay.is_valid(1))
                    self.assertFalse(delay.is_valid(0))
                    schema = response['content']['application/json']['schema']
                    self.assertEqual(schema, responses['default']['content']['application/json']['schema'])
                    validate = self.validator.evolve(schema=schema)
                    for kind in (native, service):
                        body = dict(message='Busy', type=kind, param=None, code='unavailable')
                        validate.validate(dict(error=body))
                        self.assertFalse(validate.is_valid(dict(error=dict(body, param=0))))
                        self.assertFalse(validate.is_valid(dict(error=dict(body, type='unknown'))))

    def test_gateway_rejection_precedes_the_successful_stream(self):
        op = self.document['paths']['/v1/chat/completions']['post']['responses']
        early = dict(error=dict(message='Busy', type='service_error', code='service_recovering',
                                param=None))
        self.validator.evolve(schema=op['503']['content']['application/json']['schema']).validate(early)
        events = self.validator.evolve(schema=op['200']['content']['text/event-stream']['schema'])
        self.assertFalse(events.is_valid(early))
        self.assertTrue(events.is_valid(dict(error=dict(early['error'], type='server_error'))))
