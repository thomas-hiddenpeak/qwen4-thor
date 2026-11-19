"""Retained actual audio plus synthetic lifecycle fixtures; no GPU qualification."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest

from tools.native_asr import auxiliary_service as audio
from tools.native_asr import service_acceptance as service

ROOT = Path(__file__).resolve().parents[2]
RECORDED = ROOT/'docs/evidence/serve-audio-auxiliary-profiles-20261117'


class AuxiliaryService(unittest.TestCase):
    def setUp(self):
        self.identity = json.loads((RECORDED/'qualified/registry.json').read_text())['identity']
        self.config = json.loads((RECORDED/'primary-asr-auxiliary-config.wire').read_text())
        self.response = json.loads((RECORDED/'primary-asr-auxiliary.wire').read_text())
        self.original = (ROOT/'tests/fixtures/asr/asr_zh.wav').read_bytes()
        self.source = audio.source(self.original, self.identity)

    def test_actual_multiclip_words_segments_and_fixed_source(self):
        value = audio.check(self.response, self.identity, self.config, self.source)
        self.assertEqual(value['words'], 26)
        self.assertEqual(value['segments'], 2)
        self.assertEqual(value['auxiliaries'], ['alignment', 'vad'])
        self.assertEqual(value['actual_audio_sha256'], audio.evidence.AUDIO_PAIR_SHA)

    def test_actual_stream_preserves_separator_probabilities_and_terminal(self):
        wire = (ROOT/'docs/evidence/serve-vad-http-20261117/js-repeated-stream.wire').read_bytes()
        response = service.stream_value(wire)
        result = audio.check(response, self.identity, self.config, self.source, stream=True)
        self.assertEqual(result['auxiliaries'], ['vad'])
        changed = copy.deepcopy(response);changed['logprobs'].pop()
        with self.assertRaises(ValueError):
            audio.check(changed, self.identity, self.config, self.source, stream=True)
        with self.assertRaises(RuntimeError): service.stream_value(wire+b'data: [DONE]\n\n')

    def test_missing_times_tokens_health_or_real_audio_never_pass(self):
        for field, changed in [('words', []), ('segments', []), ('duration', 1)]:
            with self.assertRaises(ValueError):
                audio.check(dict(self.response, **{field:changed}), self.identity,
                            self.config, self.source)
        changed = copy.deepcopy(self.config);changed['native_alignment_health']['state'] = 'configured'
        with self.assertRaises(ValueError):
            audio.check(self.response, self.identity, changed, self.source)
        with self.assertRaises(ValueError):
            audio.check(self.response, self.identity, self.config, self.original)

    def test_stream_does_not_claim_alignment_and_multipart_keeps_requested_shape(self):
        before = copy.deepcopy(self.identity)
        ordinary, streamed = audio.request(self.identity), audio.request(self.identity, True)
        self.assertEqual(ordinary['timestamp_granularities'], ['word','segment'])
        self.assertNotIn('timestamp_granularities', streamed)
        self.assertTrue(streamed['stream'])
        fields = audio.fields(ordinary)
        self.assertEqual([v for k,v in fields if k == 'timestamp_granularities[]'], ['word','segment'])
        self.assertEqual(json.loads(dict(fields)['chunking_strategy']), ordinary['chunking_strategy'])
        self.assertIn(('stream','true'), audio.fields(streamed))
        self.assertEqual(self.identity, before)

    def test_original_asr_only_business_remains_valid(self):
        identity = copy.deepcopy(self.identity)
        for key in ['alignment','vad']:identity['native_asr'].pop(key)
        self.assertEqual(audio.source(self.original, identity), self.original)
        response = json.loads((RECORDED/'primary-asr.wire').read_text())
        result = audio.check(response, identity, self.config, self.original)
        self.assertEqual(result['auxiliaries'], [])
        self.assertEqual(result['text'], audio.evidence.ASR_TEXT)

    def test_managed_fallback_requires_real_alignment_and_preserved_profile_history(self):
        root = Path(os.environ.get('Q4T_TEST_OUTPUT_DIR', ROOT/'.q4t-work/audio-service-cpu'))
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as temporary:
            directory = Path(temporary);out = directory/'execution';out.mkdir()
            pack = directory/'package/engine';pack.mkdir(parents=True)
            clip = directory/'audio.wav';clip.write_bytes(self.source)
            def save(name,value):
                (out/name).write_text(json.dumps(value))
            (pack/'manifest.json').write_text(json.dumps(dict(settings=dict(native_asr=self.identity['native_asr']))))
            (pack.parent/'manifest.json').write_text(json.dumps(dict(
                recovery_profiles=dict(identity=self.identity))))
            capabilities = json.loads((RECORDED/'primary-capabilities.json').read_text())
            save('capabilities.wire', capabilities)
            plan = dict(scenario='tool_version', audio_auxiliary=self.identity,
                auxiliary_clip=dict(path=str(clip),sha256=audio.evidence.AUDIO_PAIR_SHA),
                package=dict(package=str(pack.parent)))
            (directory/'plan.json').write_text(json.dumps(plan))
            for label in ['primary-asr','fallback-asr']:save(label+'.wire',self.response)
            save('final-config.wire',self.config)
            save('core-after.wire',dict(choices=[dict(finish_reason='stop',message=dict(content='42'))]))
            before = dict(guardian_pid=1,controller_pid=2,model_pid=3,gateway_pid=4,
                controller_generation=1,model_generation=1,gateway_generation=1)
            after = dict(guardian_pid=1,controller_pid=5,model_pid=6,gateway_pid=7,
                controller_generation=2,model_generation=2,gateway_generation=2,
                version_recovery=dict(selected='fallback',probe_count=1))
            save('core-continuity.json',dict(before=before,after=after))
            save('profile-continuity.json',dict(before={'charged':1},after={'charged':1}))
            for label in ['stored-before','stored-after']:save(label+'.wire',dict(choices=['kept']))
            result = service.check_business(out)
            self.assertEqual(result['results']['fallback-asr']['words'],26)
            # This historical fixture predates Realtime. If it claims a new
            # capability, it must supply the corresponding real evidence.
            claimed = copy.deepcopy(capabilities)
            claimed['realtime'] = dict(transcription_configured=True,
                automatic_replay=False, per_turn_client_admission=True)
            save('capabilities.wire', claimed)
            with self.assertRaisesRegex(RuntimeError, 'managed Realtime capability omitted'):
                service.check_business(out)
            save('capabilities.wire', capabilities)
            bad = copy.deepcopy(self.response);bad['words'] = []
            save('fallback-asr.wire',bad)
            with self.assertRaises(ValueError):service.check_business(out)
            save('fallback-asr.wire',self.response)
            save('profile-continuity.json',dict(before={'charged':1},after={'charged':0}))
            with self.assertRaises(RuntimeError):service.check_business(out)


if __name__ == '__main__':
    unittest.main()
