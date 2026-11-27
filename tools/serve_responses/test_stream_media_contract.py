"""Check event payloads through the standard media schema clients consume."""
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator


class StreamMediaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[2] / 'docs/serve-openapi-bundle-v1.json'
        spec = json.loads(path.read_bytes())
        cls.root = Draft202012Validator(spec)
        cls.streams = {
            (method, route, status): body['content']['text/event-stream']
            for route, operations in spec['paths'].items()
            for method, operation in operations.items()
            for status, body in operation['responses'].items()
            if 'text/event-stream' in body.get('content', {})
        }

    def validator(self, route, method='post', status='200'):
        return self.root.evolve(schema=self.streams[method, route, status]['schema'])

    def test_sse_declarations_are_openapi_media_type_objects(self):
        for route, media in self.streams.items():
            with self.subTest(route=route):
                fields = {key for key in media if not key.startswith('x-')}
                self.assertLessEqual(fields, {'schema', 'example', 'examples', 'encoding'})

    def test_success_media_use_the_event_payload_schema(self):
        # Include the streaming 201 from session creation.
        self.assertIn(('post', '/v1/agents/sessions', '201'), self.streams)
        for (method, path, status), media in self.streams.items():
            with self.subTest(method=method, path=path, status=status):
                self.assertFalse(self.validator(path, method, status).is_valid('chunk'))
                self.assertFalse(self.validator(path, method, status).is_valid({}))
                success = media['x-q4t-event-schema']
                if path in ('/v1/completions', '/v1/chat/completions'):
                    self.assertIn(success, media['schema']['anyOf'])
                else:
                    self.assertEqual(media['schema'], success)

    def test_responses_argument_completion_on_creation_and_retrieval(self):
        marker = dict(type='response.function_call_arguments.done', item_id='fc_1',
                      output_index=0, sequence_number=3, arguments='{}')
        for method, path in [('post', '/v1/responses'), ('get', '/v1/responses/{response_id}')]:
            with self.subTest(path=path):
                schema = self.validator(path, method)
                schema.validate(marker)
                schema.validate(dict(marker, name='get_project_release'))
                self.assertFalse(schema.is_valid(dict(marker, output_index='zero')))
                self.assertFalse(schema.is_valid({k: v for k, v in marker.items() if k != 'item_id'}))

    def test_speech_audio_and_errors(self):
        speech = self.validator('/v1/audio/speech')
        usage = dict(input_tokens=2, output_tokens=5, total_tokens=7)
        speech.validate(dict(type='speech.audio.delta', audio='AQI='))
        speech.validate(dict(type='speech.audio.done', usage=usage))
        speech.validate(dict(type='error', code='request_failed', message='failed', param=None))
        for event in [dict(type='speech.audio.delta'), dict(type='speech.audio.done', audio='AQI='),
                      dict(type='speech.audio.done', usage=dict(usage, output_tokens=-1))]:
            self.assertFalse(speech.is_valid(event))

    def test_chat_and_text_in_band_errors(self):
        error = dict(error=dict(message='failed', type='server_error',
                                code='internal_error', param=None))
        for path in ('/v1/completions', '/v1/chat/completions'):
            self.validator(path).validate(error)
            self.assertFalse(self.validator(path).is_valid({'error': {'message': 'failed'}}))
