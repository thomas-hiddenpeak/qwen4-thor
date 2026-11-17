"""Validate actual retained ASR wire and the published transport contract."""
import copy
import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator
from tools.serve_responses.test_contract import errors
from tools.serve_responses import transcriptions_contract as audio
from tools.serve_protocol import build_contract

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT/'docs/evidence/serve-asr-http-20261117/service-http-05'


class TranscriptionsContract(unittest.TestCase):
    def test_multipart_shapes_and_cross_field_constraints(self):
        base = dict(file='binary', model='qwen3-asr-0.6b')
        for extra in [{}, dict(stream=True, include=['logprobs']),
                      dict(stream=None, language='zh', prompt='context', temperature=.3),
                      dict(response_format='verbose_json', timestamp_granularities=['word']),
                      dict(chunking_strategy=dict(type='server_vad', threshold=.5))]:
            self.assertEqual(errors(dict(base, **extra), 'transcription_create'), [])
        for extra in [dict(stream='true'), dict(temperature=2), dict(extra=True),
                      dict(response_format='text', include=['logprobs']),
                      dict(timestamp_granularities=['word']),
                      dict(chunking_strategy=dict(type='server_vad', unknown=1))]:
            self.assertTrue(errors(dict(base, **extra), 'transcription_create'))

    def test_actual_json_and_every_stream_event(self):
        path = ROOT/'docs/evidence/serve-asr-sdk-values-20261117/service-http-04/python-zh-json.wire'
        value = json.loads(path.read_text())
        self.assertEqual(errors(value, 'transcription_response'), [])
        count = 0
        for line in (EVIDENCE/'js-en-stream.wire').read_text().splitlines():
            if line.startswith('data: {'):
                self.assertEqual(errors(json.loads(line[6:]), 'transcription_event'), [])
                count += 1
        self.assertGreater(count, 2)
        self.assertEqual(errors(dict(type='error', code='failed', message='failed', param=None),
                                'transcription_event'), [])
        bad = copy.deepcopy(value);bad['logprobs'][0]['bytes'] = [256]
        self.assertTrue(errors(bad, 'transcription_response'))

    def test_actual_config_health_and_bounded_native_options(self):
        schema = json.loads((ROOT/'docs/serve-config-v1.schema.json').read_text())
        validator = Draft202012Validator(schema)
        for name in ['config.json', 'final-config.wire']:
            value = json.loads((EVIDENCE/name).read_text())
            self.assertEqual(list(validator.iter_errors(value)), [])
        for key in ['native_asr_health', 'native_asr_limits']:
            bad = copy.deepcopy(value);del bad[key]
            self.assertTrue(list(validator.iter_errors(bad)))
        bad = copy.deepcopy(value)
        bad['native_asr_health']['core_readiness_dependency'] = True
        self.assertTrue(list(validator.iter_errors(bad)))
        bad = copy.deepcopy(value)
        bad['runtime']['media_limits']['video_high_min_frame_pixels'] = 0
        self.assertTrue(list(validator.iter_errors(bad)))
        legacy = copy.deepcopy(value)
        for key in ['video_detail', 'video_high_min_frame_pixels']:
            del legacy['runtime']['media_limits'][key]
        self.assertEqual(list(validator.iter_errors(legacy)), [])

    def test_pinned_sdk_and_native_openapi_publication(self):
        self.assertEqual(audio.source_sha(ROOT/'.q4t-work/serve-protocol-20261103/sdk'),
                         audio.SDK_SOURCES[audio.SDK_FILE])
        operation = build_contract.openapi()['paths']['/v1/audio/transcriptions']['post']
        self.assertIn('multipart/form-data', operation['requestBody']['content'])
        self.assertEqual(set(operation['responses']['200']['content']),
                         {'application/json', 'text/plain', 'text/event-stream'})
        self.assertFalse(operation['x-q4t']['core_readiness_dependency'])
        self.assertIn('actual_alignment', operation['x-q4t']['required_pending_executors'])


if __name__ == '__main__':
    unittest.main()
