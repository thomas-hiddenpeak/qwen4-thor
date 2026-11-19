"""ICL recovery counterexamples with synthetic CPU records, never GPU proof."""
import copy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from tools.serve_recovery import profile_acceptance as driver
from tools.serve_recovery import profile_contract as contract
from tools.serve_recovery import profile_evidence as evidence
from tools.serve_recovery import profile_migration as migration
from tools.serve_recovery import profile_voice
from tools.serve_recovery import test_profile_migration as previous
from tools.serve_recovery import test_voice_profiles as legacy
from tools.serve_service import package
from tools.serve_service.test_voice_package import capabilities, registration_asr, selected

ROOT = Path(__file__).resolve().parents[2]


def conditioning(consent_id, voice_id):
    consent_sha = 'f07f5bb483a614d5edc4ad29f786c58963c816c40a3d590c3aca3b5d09713dd1'
    common = dict(owner='a'*64, revision=1, expires_at=0)
    return dict(consent=dict(common, kind='voice_consent', id=consent_id,
        data=dict(source_sha256=consent_sha)),
        voice=dict(common, kind='voice', id=voice_id, data=dict(
            source_sha256='a0edb0a022ae9e22d57e3a710654a2cfb13f5cea070abddf2a78b663e3b6ad88',
            consent=consent_id, consent_source_sha256=consent_sha,
            transcript='The meeting starts tomorrow morning. Please bring your notebook.',
            duration_ns=4480000000, condition=[.1]*2048,
            condition_model='Qwen3-TTS-12Hz-1.7B-Base',
            condition_revision=package.NATIVE_TTS_BASE['revision'],
            reference=dict(format='q4t.tts-reference.v1',
                codec_sha256=package.NATIVE_TTS_BASE['codec_weight_sha256'],
                sample_rate=24000, samples=107520,
                codes=[[i]*16 for i in range(56)]))))


def record():
    value = legacy.cpu_record()
    value['conditioning'] = conditioning(value['consent']['id'], value['voice']['id'])
    speech = value['speech']['usage']
    speech.update(input_tokens=920, total_tokens=920+speech['output_tokens'])
    usage = value['chat']['usage']
    usage['q4t_audio_synthesis']['input_tokens'] = 918
    usage.update(prompt_tokens=938, total_tokens=938+usage['completion_tokens'])
    return value


class IclEvidence(unittest.TestCase):
    def setUp(self):
        self.identity = dict(native_tts_base=contract.tts_binding(selected(), base=True),
            native_asr=contract.asr_binding(registration_asr(), voice_registration=True))
        self.caps = capabilities(registration=True)
        self.caps['speech']['registered_voice_conditioning'] = copy.deepcopy(
            package.VOICE_CONDITIONING)

    def test_declaration_is_exact_and_legacy_remains_readable(self):
        for caps in (self.caps, capabilities(registration=True)):
            package.check_tts_capabilities(caps, None,
                self.identity['native_tts_base'], self.identity['native_asr'])
        for key, value in (('code_groups', 1), ('max_reference_frames', True),
                           ('invalid_reference', 'speaker_embedding_only'),
                           ('reference_format', 'future'), ('extra', True)):
            wrong = copy.deepcopy(self.caps)
            wrong['speech']['registered_voice_conditioning'][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(RuntimeError, 'conditioning'):
                package.check_tts_capabilities(wrong, None,
                    self.identity['native_tts_base'], self.identity['native_asr'])

    def test_old_receipt_cannot_qualify_actual_icl_registration(self):
        value = legacy.cpu_record()
        evidence.voice_business(value, self.identity)
        with self.assertRaises(ValueError):
            evidence.voice_business(value, self.identity, icl=True)
        self.assertEqual(evidence.format_for({}, self.identity, self.caps),
                         evidence.VOICE_ICL_FORMAT)
        with self.assertRaises(ValueError):
            evidence.voice_business(record(), self.identity)

    def test_complete_fixed_reference_and_input_accounting_are_required(self):
        value = record(); before = copy.deepcopy(value)
        evidence.voice_business(value, self.identity, icl=True)
        self.assertEqual(value, before)
        r = value['conditioning']
        self.assertEqual(evidence.voice_reference(r, value['consent']['id'], value['voice']['id']),
                         dict(samples=107520, frames=56, input_codes=896))

    def test_partial_bad_or_wrong_codec_codes_fail_without_vector_fallback(self):
        for change in (lambda r:r.pop('reference'),
                       lambda r:r['reference'].update(codes=[]),
                       lambda r:r['reference']['codes'][0].pop(),
                       lambda r:r['reference']['codes'][0].__setitem__(0, True),
                       lambda r:r['reference']['codes'][0].__setitem__(0, -1),
                       lambda r:r['reference']['codes'][0].__setitem__(0, 2048),
                       lambda r:r['reference'].update(codec_sha256='0'*64),
                       lambda r:r['reference'].update(samples=107519),
                       lambda r:r['reference'].update(format='future')):
            value = record(); change(value['conditioning']['voice']['data'])
            with self.assertRaises(ValueError):
                evidence.voice_business(value, self.identity, icl=True)

    def test_owner_consent_source_and_model_condition_are_bound(self):
        changes = (lambda r:r['voice'].update(owner='b'*64),
                   lambda r:r['consent'].update(id='cons_other'),
                   lambda r:r['voice'].update(expires_at=1),
                   lambda r:r['voice']['data'].update(consent_source_sha256='0'*64),
                   lambda r:r['voice']['data'].update(source_sha256='0'*64),
                   lambda r:r['voice']['data'].update(transcript='another sentence'),
                   lambda r:r['voice']['data'].update(condition=[0.0]*2048),
                   lambda r:r['voice']['data']['condition'].__setitem__(0, float('nan')),
                   lambda r:r['voice']['data'].update(condition_revision='0'*40))
        for change in changes:
            value = record(); change(value['conditioning'])
            with self.assertRaises(ValueError):
                evidence.voice_business(value, self.identity, icl=True)

    def test_text_only_input_cannot_pass_even_with_consistent_totals(self):
        value = record()
        value['speech']['usage'].update(input_tokens=24, total_tokens=89)
        with self.assertRaisesRegex(ValueError, 'reference input'):
            evidence.voice_business(value, self.identity, icl=True)
        value = record(); usage = value['chat']['usage']
        usage['q4t_audio_synthesis']['input_tokens'] = 22
        usage.update(prompt_tokens=42, total_tokens=42+usage['completion_tokens'])
        with self.assertRaisesRegex(ValueError, 'reference input'):
            evidence.voice_business(value, self.identity, icl=True)

    def test_consumer_registration_and_disabled_backend_are_distinct(self):
        self.assertTrue(evidence.voice_icl_enabled(self.identity, self.caps))
        self.assertFalse(evidence.voice_icl_enabled({}, self.caps))
        caps = copy.deepcopy(self.caps);caps['voice_registration']['enabled'] = False
        self.assertFalse(evidence.voice_icl_enabled(self.identity, caps))
        self.assertEqual(evidence.format_for({},
            dict(native_tts_base=self.identity['native_tts_base']), caps),
            evidence.VOICE_CONSUMER_FORMAT)

    def test_driver_snapshots_actual_persisted_records_and_rejects_missing_or_link(self):
        parent = ROOT/'.q4t-work/voice-icl-profile-cpu';parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as tmp:
            out = Path(tmp);store = out/'application-objects';store.mkdir()
            values = conditioning('cons_cpu', 'voice_cpu')
            paths = []
            for item in values.values():
                path = store/('a'*64+'.'+item['kind']+'.'+item['id']+'.json')
                package.save(path, dict(record=item));paths.append(path)
            actor = object.__new__(profile_voice.Lifecycle)
            actor.stage = SimpleNamespace(out=out, profile_name='primary')
            self.assertEqual(actor.conditioning('cons_cpu', 'voice_cpu'), values)
            saved = package.read(out/'primary-voice-conditioning.json')
            self.assertEqual(saved, values)
            actual = paths[1].with_name('held.json'); paths[1].rename(actual)
            with self.assertRaisesRegex(RuntimeError, 'one bounded'):
                actor.conditioning('cons_cpu', 'voice_cpu')
            paths[1].symlink_to(actual)
            with self.assertRaisesRegex(RuntimeError, 'one bounded'):
                actor.conditioning('cons_cpu', 'voice_cpu')

    def test_profile_driver_requires_explicit_icl_plan_before_any_business(self):
        stage = object.__new__(driver.Stage)
        stage.out = Path('/unused');stage.profile_name = 'primary'
        stage.plan = dict(identity=self.identity)
        with mock.patch.object(driver, 'read', return_value=self.caps):
            with self.assertRaisesRegex(RuntimeError, 'explicit conditioning'):
                stage.profile_business()
        with mock.patch.object(driver, 'prepare') as prepare, mock.patch('sys.argv', [
                'profile_acceptance.py', 'prepare', '--binary', '/binary', '--package',
                '/package', '--output', '/output', '--voice-icl']):
            driver.main()
        self.assertTrue(prepare.call_args.args[0].voice_icl)


class IclMigration(unittest.TestCase):
    setUp = legacy.VoiceProfiles.setUp
    stamp = legacy.VoiceProfiles.stamp
    selected = legacy.VoiceProfiles.selected
    voice_registry = legacy.VoiceProfiles.voice_registry

    def extended(self):
        registry, rows = self.voice_registry()
        newer = copy.deepcopy(registry)
        newer['identity'].update(binary_sha256='e'*64, protocol_sha256='f'*64)
        records = {n:copy.deepcopy(row['record']) for n,row in rows.items()}
        for row in records.values():
            row['format'] = evidence.VOICE_ICL_FORMAT
            row['capabilities']['speech']['registered_voice_conditioning'] = copy.deepcopy(
                package.VOICE_CONDITIONING)
            row['business']['voice'] = record()
        return registry, rows, newer, previous.bind_records(newer, records)

    def test_explicit_upgrade_retains_old_qualification_and_refuses_auto_downgrade(self):
        old, first, new, second = self.extended()
        before = copy.deepcopy(first)
        grant = migration.permit(old, new, first, second, self.config, self.config,
                                 allow_capability_extension=True)
        self.assertEqual(grant['format'], migration.VOICE_FORMAT)
        self.assertEqual(first, before)
        for a,b,x,y in ((old,new,first,second), (new,old,second,first)):
            with self.assertRaisesRegex(ValueError, 'capabilities'):
                migration.permit(a,b,x,y,self.config,self.config,
                    automatic=True, allow_capability_extension=True)
        with self.assertRaisesRegex(ValueError, 'capabilities'):
            migration.permit(new,old,second,first,self.config,self.config,
                             allow_capability_extension=True)

    def test_old_business_relabelled_with_icl_capability_is_rejected(self):
        _,_,new,rows = self.extended()
        values = {n:copy.deepcopy(row['record']) for n,row in rows.items()}
        for row in values.values():
            row['business']['voice'] = legacy.cpu_record()
        fake = previous.bind_records(new, values)
        for name in contract.NAMES:
            with self.assertRaises(ValueError):
                evidence.validate(fake[name]['record'], new, name, fake[name]['sha256'])

    def test_same_icl_contract_allows_qualified_automatic_recovery(self):
        _,_,new,rows = self.extended()
        target = copy.deepcopy(new)
        target['identity'].update(binary_sha256='c'*64, protocol_sha256='d'*64)
        values = {n:copy.deepcopy(row['record']) for n,row in rows.items()}
        other = previous.bind_records(target, values)
        grant = migration.permit(new, target, rows, other, self.config, self.config,
                                 automatic=True)
        self.assertTrue(grant['automatic'])


if __name__ == '__main__':
    unittest.main()
