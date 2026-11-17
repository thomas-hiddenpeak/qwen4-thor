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
                      dict(chunking_strategy=dict(type='server_vad', unknown=1)),
                      dict(stream=True, response_format='vtt')]:
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
                         {'application/json', 'text/plain', 'text/event-stream',
                          'text/vtt', 'application/x-subrip'})
        self.assertFalse(operation['x-q4t']['core_readiness_dependency'])
        self.assertEqual(operation['x-q4t']['required_pending_executors'], ['overlap_diarization'])
        self.assertIn('server_vad', operation['x-q4t']['chunking']['strategies'])

    def test_actual_multi_clip_segments_stream_and_silent_usage(self):
        evidence = ROOT/'docs/evidence/serve-vad-http-20261117'
        verbose = json.loads((evidence/'python-repeated-segments.wire').read_text())
        self.assertEqual(errors(verbose, 'transcription_verbose_response'), [])
        for field in ['seek', 'tokens', 'avg_logprob', 'no_speech_prob']:
            bad = copy.deepcopy(verbose); del bad['segments'][0][field]
            self.assertTrue(errors(bad, 'transcription_verbose_response'))
        bad = copy.deepcopy(verbose);bad['segments'][0]['tokens'] = [-1]
        self.assertTrue(errors(bad, 'transcription_verbose_response'))
        for line in (evidence/'js-repeated-stream.wire').read_text().splitlines():
            if line.startswith('data: {'):
                self.assertEqual(errors(json.loads(line[6:]), 'transcription_event'), [])
        empty = json.loads((evidence/'python-natural-silence.wire').read_text())
        self.assertEqual(errors(empty, 'transcription_response'), [])

    def test_actual_three_backend_health_is_separate_and_config_complete(self):
        validator = Draft202012Validator(json.loads((ROOT/'docs/serve-config-v1.schema.json').read_text()))
        evidence = ROOT/'docs/evidence/serve-vad-http-20261117'
        for name in ['missing-vad-config.wire', 'final-config.wire']:
            value = json.loads((evidence/name).read_text())
            self.assertEqual(list(validator.iter_errors(value)), [])
        for key in ['native_alignment_health', 'native_alignment_limits',
                     'native_vad_health', 'native_vad_max_chunks']:
            bad = copy.deepcopy(value);del bad[key]
            self.assertTrue(list(validator.iter_errors(bad)))
        for domain in ['native_alignment_health', 'native_vad_health']:
            bad = copy.deepcopy(value);bad[domain]['core_readiness_dependency'] = True
            self.assertTrue(list(validator.iter_errors(bad)))
        bad = copy.deepcopy(value);bad['native_vad_health']['device_bytes'] = 1
        self.assertTrue(list(validator.iter_errors(bad)))

    def test_diarized_segments_deltas_done_and_negative_contracts(self):
        segment = dict(id='0', type='transcript.text.segment', start=.1,
                       end=1.2, speaker='caller', text='你好！')
        usage = dict(type='tokens', input_tokens=30, output_tokens=4,
                     total_tokens=34, input_token_details=dict(audio_tokens=20, text_tokens=10))
        result = dict(task='transcribe', duration=2, text='你好！',
                      segments=[segment], usage=usage)
        self.assertEqual(errors(result, 'transcription_diarized_response'), [])
        self.assertEqual(errors(segment, 'transcription_event'), [])
        self.assertEqual(errors(dict(type='transcript.text.delta', delta='你好！',
                                     segment_id='0'), 'transcription_event'), [])
        done = dict(result, type='transcript.text.done')
        self.assertEqual(errors(done, 'transcription_event'), [])
        for key in ['id', 'type', 'start', 'end', 'speaker', 'text']:
            bad = copy.deepcopy(segment);del bad[key]
            self.assertTrue(errors(bad, 'transcription_diarized_segment'))
        for key, value in [('id', 0), ('speaker', ''), ('start', -1), ('end', 601)]:
            bad = dict(segment);bad[key] = value
            self.assertTrue(errors(bad, 'transcription_diarized_segment'))
        for key in ['task', 'duration', 'segments']:
            bad = dict(done);del bad[key]
            self.assertTrue(errors(bad, 'transcription_event'))

    def test_speaker_config_is_optional_complete_and_never_core_dependency(self):
        schema = json.loads((ROOT/'docs/serve-config-v1.schema.json').read_text())
        validator = Draft202012Validator(schema)
        value = json.loads((ROOT/'docs/evidence/serve-vad-http-20261117/final-config.wire').read_text())
        self.assertEqual(list(validator.iter_errors(value)), [])
        speaker = copy.deepcopy(value['native_alignment_health'])
        speaker.update(model='speechbrain-ecapa-voxceleb', state='degraded',
                       reason='speaker_assets_invalid', device_bytes=0)
        value.update(native_speaker_health=speaker, native_speaker_limits=dict(
            window_ms=3000, step_ms=750, max_windows=256, max_speakers=16,
            workspace_mib=128))
        self.assertEqual(list(validator.iter_errors(value)), [])
        for key in ['native_speaker_health', 'native_speaker_limits']:
            bad = copy.deepcopy(value);del bad[key]
            self.assertTrue(list(validator.iter_errors(bad)))
        bad = copy.deepcopy(value);bad['native_speaker_health']['core_readiness_dependency'] = True
        self.assertTrue(list(validator.iter_errors(bad)))
        bad = copy.deepcopy(value);bad['native_speaker_limits']['max_windows'] = 1025
        self.assertTrue(list(validator.iter_errors(bad)))


if __name__ == '__main__':
    unittest.main()
