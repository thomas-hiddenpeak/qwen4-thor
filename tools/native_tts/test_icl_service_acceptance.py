"""CPU negatives for ICL business evidence; no model or fake quality result."""
import copy
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from tools.native_tts import icl_service_acceptance as driver


class IclServiceEvidence(unittest.TestCase):
    def setUp(self):
        root = driver.ROOT/'.q4t-work/serve-tts-icl-20261119/driver-cpu-work'
        root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temp.cleanup)
        self.stage = driver.Stage.__new__(driver.Stage)
        self.stage.out = Path(self.temp.name)
        self.stage.started = time.monotonic()
        self.stage.counts = dict(starts=0, posts=0, controls=0, ingress_starts=0)
        self.stage.artifacts = mock.Mock()
        self.consent = dict(id='cons_actual', owner='owner',
                            data=dict(source_sha256='c'*64))
        self.clip = dict(sha256='b'*64)
        self.voice = dict(kind='voice', id='voice_actual', owner='owner', data=dict(
            consent='cons_actual', consent_source_sha256='c'*64, source_sha256='b'*64,
            transcript=driver.TEXT['reference'], duration_ns=4480000000,
            condition=[.25]*2048, reference=dict(format='q4t.tts-reference.v1',
                codec_sha256=driver.CODEC, sample_rate=24000, samples=107520,
                codes=[[i % 2048 for i in range(16)] for _ in range(56)])))

    def test_reference_requires_all_groups_bound_to_actual_owner_and_source(self):
        checked = driver.check_reference(self.voice, self.consent, 'voice_actual', self.clip)
        self.assertEqual(checked['codes'], 896)
        for mutate in range(9):
            value = copy.deepcopy(self.voice)
            if mutate == 0: value['owner'] = 'other'
            if mutate == 1: value['data']['source_sha256'] = 'f'*64
            if mutate == 2: value['data']['reference']['codes'][0].pop()
            if mutate == 3: value['data']['reference']['codes'][0][0] = 2048
            if mutate == 4: value['data']['reference']['codes'][0][0] = True
            if mutate == 5: value['data']['reference']['samples'] -= 1
            if mutate == 6: value['data']['reference']['codec_sha256'] = '0'*64
            if mutate == 7: value['data']['condition'][0] = float('nan')
            if mutate == 8: value['data']['reference'] = {}
            with self.subTest(mutate=mutate), self.assertRaises(RuntimeError):
                driver.check_reference(value, self.consent, 'voice_actual', self.clip)

    def test_usage_rejects_text_only_counts_budget_counts_and_missing_eos(self):
        value = dict(input_tokens=924, output_tokens=513, total_tokens=1437)
        driver.check_usage(value, 924, 32*1920)
        for mutate in range(5):
            row = dict(value)
            if mutate == 0: row.update(input_tokens=28, total_tokens=541)
            if mutate == 1: row.update(output_tokens=3201, total_tokens=4125)
            if mutate == 2: row.update(output_tokens=512, total_tokens=1436)
            if mutate == 3: row['total_tokens'] += 1
            if mutate == 4: row['input_tokens'] = 924.0
            with self.subTest(mutate=mutate), self.assertRaises(RuntimeError):
                driver.check_usage(row, 924, 32*1920)

    def test_settlement_requires_one_request_actual_usage_and_drained_credit(self):
        names = ['q4t_client_rate_settled_tokens_total', 'q4t_client_rate_admitted_total',
                 'q4t_client_rate_reserved_tokens']
        keys = [n+'{client="audio"}' for n in names]
        before = dict(zip(keys, (100, 1, 0), strict=True))
        after = dict(zip(keys, (1537, 2, 0), strict=True))
        driver.check_settlement(before, after, 1437)
        for key in keys:
            bad = dict(after); bad[key] += 1
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                driver.check_settlement(before, bad, 1437)

    def test_attempt_and_time_bounds_precede_every_action(self):
        for kind in ('starts', 'ingress_starts', 'posts', 'controls'):
            self.stage.counts[kind] = driver.LIMITS[kind]
            with self.assertRaisesRegex(RuntimeError, 'attempt bound'):
                self.stage.budget(kind)
            self.assertEqual(self.stage.counts[kind], driver.LIMITS[kind])
        self.stage.counts['controls'] = 0
        self.stage.started -= driver.LIMITS['seconds']
        with self.assertRaisesRegex(RuntimeError, 'deadline'):
            self.stage.budget('controls')
        self.assertEqual(self.stage.counts['controls'], 0)

    def test_plan_cannot_widen_bounds_or_supply_asr_expected_text(self):
        base = dict(schema='q4t-voice-http-v1', limits=driver.direct.LIMITS,
                    load_limits=driver.direct.LOAD_LIMITS, requests=driver.direct.requests())
        phase = dict(format='q4t-icl-service-v1', bounds=driver.LIMITS, text=driver.TEXT,
                     contract=driver.CONTRACT, http=base,
                     sdk=dict(version=driver.sdk_driver.voice_sdk.VERSION),
                     input_tokens=dict(speech=924, chat=927))
        driver.validate_plan(phase)
        for mutate in range(4):
            bad = copy.deepcopy(phase)
            if mutate == 0: bad['bounds']['starts'] = 2
            if mutate == 1: bad['http']['requests']['transcription']['prompt'] = driver.TEXT['speech']
            if mutate == 2: bad['input_tokens']['speech'] = 28
            if mutate == 3: bad['contract']['reference_frames'] = 55
            with self.subTest(mutate=mutate), self.assertRaises(RuntimeError):
                driver.validate_plan(bad)

    def test_entry_cleanup_failure_still_cleans_core_and_never_records_pass(self):
        self.stage.phase = dict(edge_port=18376)
        self.stage.plan = dict(defaults={})
        self.stage.start = mock.Mock(); self.stage.business = mock.Mock()
        self.stage.cleanup = mock.Mock()
        self.stage.edge = SimpleNamespace(close=mock.Mock(side_effect=RuntimeError('owned entry cleanup')))
        with mock.patch.object(driver.direct.chat, 'probe_ports'), \
                mock.patch.object(driver.direct, 'defaults', return_value={}):
            with self.assertRaisesRegex(RuntimeError, 'owned entry cleanup'):
                self.stage.run()
        self.stage.cleanup.assert_called_once()
        result = json.loads((self.stage.out/'result.json').read_text())
        self.assertEqual(result['status'], 'FAILED')
        self.assertIn('owned entry cleanup', result['entry_cleanup_error'])


if __name__ == '__main__': unittest.main()
