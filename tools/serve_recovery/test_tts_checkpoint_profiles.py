"""Versioned validator tests using retained complete audio; no GPU acceptance."""
import base64
import copy
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock

from tools.serve_recovery import profile_evidence as evidence
from tools.serve_recovery import profile_contract as contract
from tools.serve_recovery import profile_acceptance as driver
from tools.serve_recovery import test_profile_migration as migration_fixture
from tools.serve_recovery import test_tts_profiles as legacy
from tools.native_tts import managed_service as managed
from tools.serve_service import package

ROOT = Path(__file__).resolve().parents[2]


def retained():
    archive = ROOT/'docs/evidence/serve-tts-checkpoint-witness-20261119/records.tar.gz'
    prefix = '.q4t-work/serve-custom-voices-20261118/tts-checkpoint-witness-01/run/'
    with tarfile.open(archive) as source:
        def raw(name):
            return source.extractfile(prefix+name).read()
        config = json.loads(raw('chat-config.wire'))
        return dict(requests=evidence.tts_requests(),
            sampling_profile=evidence.TTS_CHECKPOINT_SAMPLING,
            speech_base64=base64.b64encode(raw('speech.mp3')).decode(),
            chat_response=json.loads(raw('chat.wire')),
            health=config['native_tts_health'], limits=config['native_tts_limits'])


class CheckpointWitness(unittest.TestCase):
    def setUp(self):
        self.value = retained()
        self.selected = dict(package.NATIVE_TTS, directory='/fixture/tts',
                             limits=self.value['limits'])
        self.identity = dict(native_tts=contract.tts_binding(self.selected))

    def test_whole_audio_keeps_legacy_and_checkpoint_identities_separate(self):
        evidence.tts_business(self.value, self.identity)
        old, _ = legacy.retained()
        old_identity = dict(native_tts=contract.tts_binding(
            dict(self.selected, limits=old['limits'])))
        evidence.tts_business(old, old_identity)
        changes = [lambda d:d.pop('sampling_profile'),
            lambda d:d.update(sampling_profile=None),
            lambda d:d.update(sampling_profile='future'),
            lambda d:d.update(speech_base64=old['speech_base64']),
            lambda d:d['chat_response']['choices'][0]['message']['audio'].update(
                data=old['chat_response']['choices'][0]['message']['audio']['data']),
            lambda d:d.update(speech_base64=base64.b64encode(b'\0'*58368).decode()),
            lambda d:d.update(speech_base64=base64.b64encode(
                base64.b64decode(d['speech_base64'])[:-1]).decode())]
        for change in changes:
            bad = copy.deepcopy(self.value);change(bad)
            with self.assertRaises(ValueError):evidence.tts_business(bad,self.identity)
        with self.assertRaises(ValueError):
            evidence.tts_business(dict(old,sampling_profile=evidence.TTS_CHECKPOINT_SAMPLING),old_identity)

    def test_receipt_version_binds_sampling_without_rewriting_old_records(self):
        fixture = legacy.TtsProfiles();fixture.setUp();self.addCleanup(fixture.doCleanups)
        registry, rows = fixture.extended()
        original = copy.deepcopy(rows)
        for name, row in rows.items():
            evidence.validate(row['record'],registry,name,row['sha256'])
        registry['identity']['native_tts'] = self.identity['native_tts']
        records = {name:copy.deepcopy(row['record']) for name,row in rows.items()}
        for value in records.values():
            value['business']['tts'] = copy.deepcopy(self.value)
            value['format'] = evidence.TTS_CHECKPOINT_FORMAT
        current = migration_fixture.bind_records(registry,records)
        for name,row in current.items():
            evidence.validate(row['record'],registry,name,row['sha256'])
            wrong = copy.deepcopy(row['record']);wrong['format']=evidence.TTS_FORMAT
            with self.assertRaisesRegex(ValueError,'identity'):
                evidence.validate(wrong,registry,name,row['sha256'])
        self.assertEqual(rows,original)
        both = dict(self.identity,native_tts_base={})
        self.assertEqual(evidence.format_for({},both,None,dict(tts=self.value)),
                         evidence.VOICE_CHECKPOINT_FORMAT)
        self.assertEqual(evidence.format_for({},both),evidence.VOICE_FORMAT)
        self.assertEqual(evidence.format_for({},self.identity),evidence.TTS_FORMAT)

    def test_reload_requires_actual_checkpoint_bytes_and_evicted_input_weights(self):
        previous = dict(self.value['health'])
        before = dict(native_tts_health=dict(previous,state='metadata_ready',device_bytes=0))
        after = dict(native_tts_health=dict(previous,load_attempts=previous['load_attempts']+1))
        for name in ('embedding','asr'):
            after['native_'+name+'_health']=dict(state='metadata_ready',device_bytes=0,
                reason='none',core_readiness_dependency=False)
        args = (before,after,previous,self.value['speech_base64'],self.identity)
        driver.check_tts_family_reload(*args,sampling_profile=evidence.TTS_CHECKPOINT_SAMPLING)
        with self.assertRaises(ValueError):driver.check_tts_family_reload(*args)
        after['native_asr_health']['device_bytes']=1
        with self.assertRaises((ValueError,RuntimeError)):
            driver.check_tts_family_reload(*args,sampling_profile=evidence.TTS_CHECKPOINT_SAMPLING)

    def test_driver_preserves_explicit_sampling_and_complete_wire(self):
        root=Path(os.environ['Q4T_TEST_OUTPUT_DIR']);root.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as temporary:
            stage=object.__new__(driver.Stage);stage.out=Path(temporary);stage.profile_name='primary'
            stage.plan=dict(tts_sampling_profile=evidence.TTS_CHECKPOINT_SAMPLING)
            stage.budget=mock.Mock();stage.same_model=mock.Mock();stage.drained=mock.Mock()
            stage.request=mock.Mock(return_value=self.value['chat_response'])
            config=dict(native_tts_health=self.value['health'],native_tts_limits=self.value['limits'])
            stage.http=mock.Mock(side_effect=[(200,base64.b64decode(self.value['speech_base64'])),
                                             (200,json.dumps(config).encode())])
            observed=stage.tts_request();self.assertEqual(observed,self.value)
            evidence.tts_business(observed,self.identity)
            self.assertEqual(stage.budget.call_args_list,[mock.call('chats')])

    def test_cli_freezes_sampling_selection_explicitly(self):
        with mock.patch.object(driver,'prepare') as prepare,mock.patch('sys.argv',[
                'profile_acceptance.py','prepare','--binary','/candidate',
                '--package','/bundle','--output','/out','--tts-checkpoint-sampling']):
            driver.main()
        self.assertTrue(prepare.call_args.args[0].tts_checkpoint_sampling)

    def test_managed_phases_cannot_reuse_greedy_audio_or_omit_sampling(self):
        root=Path(os.environ['Q4T_TEST_OUTPUT_DIR']);root.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as temporary:
            out=Path(temporary);plan=dict(scenario='tool_version',
                native_tts=self.identity['native_tts'],
                tts_sampling_profile=evidence.TTS_CHECKPOINT_SAMPLING)
            for label in managed.labels(plan):
                prefix=label+'-tts'
                package.save(out/(prefix+'-config.wire'),dict(native_tts_health=self.value['health'],
                    native_tts_limits=self.value['limits']))
                package.save(out/(prefix+'-chat.wire'),self.value['chat_response'])
                (out/(prefix+'-speech.wire')).write_bytes(base64.b64decode(self.value['speech_base64']))
                for kind,request in evidence.tts_requests().items():
                    package.save(out/(prefix+'-'+kind+'-request.json'),request)
                    package.save(out/(prefix+'-'+kind+'-http.json'),dict(status=200))
            result=managed.check(out,plan,self.selected)
            self.assertEqual(set(result),{'primary-tts','fallback-tts'})
            self.assertTrue(all(row['actual_chat_audio_sha256']==evidence.TTS_CHECKPOINT_AUDIO['chat'][1]
                                for row in result.values()))
            old=copy.deepcopy(plan);old.pop('tts_sampling_profile')
            with self.assertRaises(ValueError):managed.check(out,old,self.selected)
            raw,_=legacy.retained()
            (out/'fallback-tts-speech.wire').write_bytes(base64.b64decode(raw['speech_base64']))
            with self.assertRaises(ValueError):managed.check(out,plan,self.selected)


if __name__=='__main__':unittest.main()
