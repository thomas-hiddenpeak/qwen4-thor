"""CPU qualification contracts; synthetic passing records are never GPU evidence."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

from tools.native_asr import auxiliary_service as managed
from tools.serve_recovery import profile_evidence as evidence
from tools.serve_recovery import profile_contract as contract
from tools.serve_recovery import profile_acceptance as driver
from tools.serve_recovery import profile_migration as migration
from tools.serve_recovery import test_speaker_profiles as old
from tools.serve_service import package

ROOT = Path(__file__).resolve().parents[2]
OBSERVED = ROOT/'docs/evidence/serve-overlap-http-output-fix-20261118'


class SeparatorProfiles(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((OBSERVED/'final-config.wire').read_text())
        selected = dict(package.NATIVE_ASR,directory='/optional/asr',
                        limits=self.config['native_asr_limits'])
        for name,(fixed,_,_) in package.ASR_AUXILIARY.items():
            selected[name] = dict(fixed,directory='/optional/'+name,
                limits=package.asr_auxiliary_observed_limits(self.config,name))
        self.identity = dict(native_asr=contract.asr_binding(selected))
        self.actual = dict(source_sha256=evidence.SEPARATOR_FILE_SHA,
            request=evidence.separator_request(self.identity),
            response=json.loads((OBSERVED/'python-known.wire').read_text()),
            health=self.config['native_separator_health'],limits=self.config['native_separator_limits'])

    def synthetic(self):
        # Explicitly synthetic positive oracle for CPU control logic. The
        # original failed result remains immutable; this control has zero WER.
        value = copy.deepcopy(self.actual)
        for row in value['response']['segments']:
            row['text'] = row['text'].replace('hurts','hurt')
        value['response']['text'] = ' '.join(row['text'] for row in value['response']['segments'])
        return value

    def test_reviewed_inflection_retains_raw_wer_and_model_limitation(self):
        before = copy.deepcopy(self.actual)
        result = evidence.separator_business(self.actual,self.identity)
        self.assertEqual(result['voices'][0]['wer'],1/7)
        self.assertEqual(result['voices'][1]['wer'],0)
        self.assertTrue(result['voices'][0]['accepted_inflection'])
        self.assertTrue(result['model_quality_limitation'])
        self.assertGreaterEqual(result['overlap_seconds'],.3)
        actual = managed.separator_result(self.actual['response'],self.identity,self.config,
                                         evidence.SEPARATOR_FILE_SHA)
        self.assertEqual(actual['quality'],result)
        self.assertEqual(self.actual,before)
        self.assertFalse(evidence.separator_business(self.synthetic(),self.identity)
                         ['model_quality_limitation'])

    def test_review_does_not_accept_meaning_changes_or_missing_speech(self):
        for text in ['The child never hurts the small dog.',
                     'The child almost hurts the big cat.',
                     'The child hurts.', 'The child almost hurt the small cat twice.']:
            value = copy.deepcopy(self.actual)
            value['response']['segments'][0]['text'] = text
            value['response']['text'] = ' '.join(row['text'] for row in value['response']['segments'])
            with self.assertRaisesRegex(ValueError,'per-voice recognition quality'):
                evidence.separator_business(value,self.identity)
        value = copy.deepcopy(self.actual)
        value['response']['segments'].pop()
        value['response']['text'] = value['response']['segments'][0]['text']
        with self.assertRaises(ValueError):evidence.separator_business(value,self.identity)

    def test_mixed_source_time_words_usage_and_health_are_required(self):
        oracle = self.synthetic();evidence.separator_business(oracle,self.identity)
        defects = [lambda v:v.update(source_sha256='0'*64),
            lambda v:v['response']['segments'].pop(),
            lambda v:v['response']['segments'][0].update(speaker='first'),
            lambda v:v['response']['segments'][0].update(start=float('nan')),
            lambda v:v['response']['segments'][1].update(start=3.5,end=3.8),
            lambda v:(v['response']['segments'][0].update(start=.16,end=.8),
                      v['response']['segments'][1].update(start=1.,end=3.)),
            lambda v:v['response']['usage'].update(total_tokens=1),
            lambda v:v['response']['usage']['input_token_details'].update(audio_tokens=True),
            lambda v:v['response'].update(text='fake'),
            lambda v:v['health'].update(core_readiness_dependency=True),
            lambda v:v['health'].update(device_bytes=0),
            lambda v:v['health'].update(compute_failures=False),
            lambda v:v['limits'].update(max_audio_seconds=8)]
        for mutate in defects:
            value = copy.deepcopy(oracle);mutate(value)
            with self.assertRaises(ValueError):evidence.separator_business(value,self.identity)

    def test_new_qualification_format_and_real_request_accounting(self):
        # Use the existing declared profile shape instead of inventing fields.
        prior = old.SpeakerProfiles(methodName='test_qualification_requires_speaker_work_and_bound_backend')
        prior.setUp()
        try:
            _,_,registry,records = prior.extended()
            selected = records['primary']['record']['capacity']
            with_speaker = driver.limits_for(selected,registry['identity']['native_asr'])
            with_separator = driver.limits_for(selected,self.identity['native_asr'])
            self.assertEqual(with_separator['chats'],with_speaker['chats']+2)
            self.assertEqual(evidence.format_for(selected,self.identity),evidence.SEPARATOR_FORMAT)
            row = copy.deepcopy(records['primary']['record'])
            row['identity']['native_asr'] = copy.deepcopy(self.identity['native_asr'])
            with self.assertRaises(ValueError):evidence.business(row['business'],row['identity'],selected)
        finally:prior.doCleanups()

    def test_completion_declaration_and_migration_cannot_drop_separator(self):
        caps = json.loads((OBSERVED/'capabilities.json').read_text())
        caps['transcriptions']['remaining_native_work'] = []
        migration.semantic_capabilities(caps)
        for name in ['separator','speaker','alignment','vad']:
            bad = copy.deepcopy(caps);bad['transcriptions'].pop(name)
            with self.assertRaises(ValueError):migration.semantic_capabilities(bad)
        selected = self.identity['native_asr']
        migration.audio_change(selected,selected)
        for name in ['separator','speaker']:
            bad = copy.deepcopy(selected);bad.pop(name)
            with self.assertRaises((ValueError,RuntimeError)):
                migration.audio_change(selected,bad)
        bad = copy.deepcopy(caps)
        bad['transcriptions']['separator']['timestamps'] = 'serialized'
        with self.assertRaises(ValueError):migration.semantic_capabilities(bad)

    def test_managed_requests_preserve_full_pipeline_and_require_healthy_dependencies(self):
        before = copy.deepcopy(self.identity)
        value = self.synthetic()
        result = managed.separator_result(value['response'],self.identity,self.config,
                                           evidence.SEPARATOR_FILE_SHA,stream=True)
        self.assertEqual(result['auxiliaries'],['alignment','vad','speaker','separator'])
        for name in ['asr','alignment','vad','speaker','separator']:
            bad = copy.deepcopy(self.config);bad['native_'+name+'_health']['state'] = 'degraded'
            with self.assertRaises(ValueError):
                managed.separator_result(value['response'],self.identity,bad,evidence.SEPARATOR_FILE_SHA)
        request = managed.request(self.identity,stream=True)
        self.assertEqual(request['response_format'],'diarized_json')
        self.assertIn(('stream','true'),managed.fields(request))
        self.assertEqual(self.identity,before)
        with self.assertRaises(ValueError):managed.source(b'other audio',self.identity)

    def test_managed_cli_accepts_explicit_fixed_source(self):
        from tools.native_asr import service_acceptance as service
        args = ['service_acceptance.py','prepare','--package','/package',
                '--output','/output','--separator-audio','/private/mixed.wav']
        with mock.patch.object(sys,'argv',args), \
             mock.patch.dict(service.GLOBALS,prepare=mock.Mock()) as namespace:
            service.BASE['main']()
            self.assertEqual(namespace['prepare'].call_args.args[0].separator_audio,
                             Path('/private/mixed.wav'))


if __name__ == '__main__':unittest.main()
