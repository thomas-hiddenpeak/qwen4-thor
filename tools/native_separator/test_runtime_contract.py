"""Native separator discovery/configuration; no CUDA or model execution."""
import copy
import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator
from tools.serve_protocol import build_contract

ROOT = Path(__file__).resolve().parents[2]


class RuntimeContract(unittest.TestCase):
    def test_config_recovery_and_complete_pipeline(self):
        validator = Draft202012Validator(json.loads(
            (ROOT / 'docs/serve-config-v1.schema.json').read_text()))
        value = json.loads((ROOT / 'docs/evidence/serve-speaker-http-20261118'
                           / 'final-config.wire').read_text())
        self.assertEqual(list(validator.iter_errors(value)), [])
        health = copy.deepcopy(value['native_speaker_health'])
        health.update(model='speechbrain-sepformer-wsj02mix', state='degraded',
                      reason='separator_assets_invalid')
        value.update(native_separator_health=health,
                     native_separator_limits=dict(max_audio_seconds=30,
                                                  workspace_mib=2048))
        self.assertEqual(list(validator.iter_errors(value)), [])
        for key in ['native_separator_health', 'native_separator_limits',
                    'native_speaker_health', 'native_asr_limits',
                    'native_alignment_health', 'native_vad_health']:
            bad = copy.deepcopy(value)
            del bad[key]
            self.assertTrue(list(validator.iter_errors(bad)), key)
        for field, invalid in [('max_audio_seconds', 31), ('workspace_mib', 2049),
                               ('max_audio_seconds', 0)]:
            bad = copy.deepcopy(value)
            bad['native_separator_limits'][field] = invalid
            self.assertTrue(list(validator.iter_errors(bad)))
        bad = copy.deepcopy(value)
        bad['native_separator_health']['core_readiness_dependency'] = True
        self.assertTrue(list(validator.iter_errors(bad)))

    def test_openapi_separation_identity_budget_and_actual_overlap(self):
        operation = build_contract.openapi()['paths'][
            '/v1/audio/transcriptions']['post']['x-q4t']
        overlap = operation['diarization']['overlap']
        self.assertEqual(overlap['configured_by'], 'separator-model-dir')
        self.assertEqual(overlap['requires'], ['speaker-model-dir'])
        self.assertEqual(overlap['simultaneous_sources'], 2)
        self.assertEqual(overlap['sample_rate'], 8000)
        self.assertEqual(overlap['output_sample_rate'], 16000)
        self.assertIn('cross-track', overlap['identity'])
        self.assertIn('crossing', overlap['timing'])
        self.assertIn('one original', overlap['quotas'])
        self.assertFalse(operation['core_readiness_dependency'])


if __name__ == '__main__':
    unittest.main()
