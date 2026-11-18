"""Reject false-positive voice business evidence before using a GPU lease."""
import copy
import io
import json
from pathlib import Path
import struct
import tempfile
import time
import unittest
from unittest.mock import patch
import wave

from tools.native_tts import voice_service_acceptance as subject


class Contract(unittest.TestCase):
    def test_independent_asr_multipart_keeps_binary_without_expected_text(self):
        audio = b'RIFF\x00\xff\r\noriginal samples'
        fields = subject.requests()['transcription']
        raw, mime = subject.multipart(fields, 'file', audio)
        self.assertIn(audio, raw)
        self.assertIn('multipart/form-data; boundary=', mime)
        for forbidden in (b'name="prompt"', b'name="language"',
                          subject.PHRASE.encode(), subject.TARGETS[0].encode()):
            self.assertNotIn(forbidden, raw)
        with self.assertRaises(RuntimeError):
            subject.multipart(fields, 'file', b'q4t-voice-business-20261118')

    def test_negative_requires_exact_failure_class_not_any_failed_request(self):
        value = dict(error=dict(code='voice_speaker_mismatch'))
        subject.checked_error(400, json.dumps(value), 400, 'voice_speaker_mismatch')
        for status, row in ((503, value), (400, dict(error=dict(code='oom'))),
                            (400, dict(value, id='voice_wrong')), (200, value)):
            with self.assertRaises(RuntimeError):
                subject.checked_error(status, json.dumps(row), 400, 'voice_speaker_mismatch')

    def test_metadata_never_accepts_private_record_or_an_incomplete_success(self):
        good = dict(id='voice_a', object='audio.voice', created_at=1,
                    name='sample', type='audio_sample')
        subject.checked_metadata(200, json.dumps(good), 'voice')
        for row in (dict(good, condition=[1]*2048), dict(good, transcript='private'),
                    dict(good, object='audio.voice_consent'), dict(good, id='cons_a')):
            with self.assertRaises(RuntimeError):
                subject.checked_metadata(200, json.dumps(row), 'voice')

    def test_unprompted_content_rejects_wrong_words_negation_and_empty(self):
        subject.check_transcript(dict(text='Please, CLOSE the window!'), subject.TARGETS[1])
        for text in ('', 'Please open the window.', 'Please do not close the window.',
                     'Please close the window. Additional words.'):
            with self.assertRaises(RuntimeError):
                subject.check_transcript(dict(text=text), subject.TARGETS[1])

    def test_wave_rejects_silence_and_truncation(self):
        def encode(samples):
            out = io.BytesIO()
            with wave.open(out, 'wb') as wav:
                wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(24000)
                wav.writeframes(struct.pack('<'+'h'*len(samples), *samples))
            return out.getvalue()
        good = encode([1000, -1000]*1920)
        self.assertEqual(subject.check_wave(good)['samples'], 3840)
        for raw in (encode([0]*3840), good[:-10], encode([1000]*10)):
            with self.assertRaises(RuntimeError):
                subject.check_wave(raw)

    def test_fair_policy_cannot_exceed_global_queue_or_inflight(self):
        policy = dict(fair_queue=True, clients=[dict(max_inflight=1)]*3)
        def args(inflight, queued):
            return ['serve', '--max-inflight-requests', str(inflight),
                    '--max-queued-requests', str(queued)]
        subject.check_admission(args(4, 3), policy)
        for inflight, queued in ((2, 3), (4, 1), (2, 1)):
            with self.assertRaises(RuntimeError):
                subject.check_admission(args(inflight, queued), policy)
        policy['clients'][0] = dict(max_inflight=3)
        with self.assertRaises(RuntimeError):
            subject.check_admission(args(4, 3), policy)

    def test_request_time_and_frozen_plan_bounds_fail_closed(self):
        plan = dict(schema='q4t-voice-http-v1', requests=subject.requests(),
                    limits=subject.LIMITS, load_limits=subject.LOAD_LIMITS)
        subject.validate_plan(plan)
        changed = copy.deepcopy(plan); changed['limits']['posts'] += 1
        with self.assertRaises(RuntimeError): subject.validate_plan(changed)
        with tempfile.TemporaryDirectory() as folder:
            stage = subject.Stage.__new__(subject.Stage)
            stage.out = Path(folder); stage.started = time.monotonic()
            stage.counts = dict(posts=subject.LIMITS['posts']-1)
            with patch.object(stage, 'artifacts'):
                stage.budget('posts')
                with self.assertRaises(RuntimeError): stage.budget('posts')
                stage.counts['posts'] = 0
                stage.started -= subject.LIMITS['seconds']
                with self.assertRaises(RuntimeError): stage.budget('posts')
                self.assertEqual(stage.counts['posts'], 0)


if __name__ == '__main__':
    unittest.main()
