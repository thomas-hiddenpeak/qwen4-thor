"""Real retained diarized wire with synthetic CPU registries, never GPU claims."""
import contextlib
import copy
import io
import json
from pathlib import Path
import unittest
from unittest import mock

from tools.serve_recovery import profile_contract as contract
from tools.serve_recovery import profile_evidence as evidence
from tools.serve_recovery import profile_migration as migration, profile_policy as policy
from tools.serve_recovery import profile_acceptance as driver, service
from tools.serve_recovery import test_audio_auxiliary_profiles as old
from tools.serve_recovery.test_deployment import EmbeddingTargetTests
from tools.serve_service import package

ROOT = Path(__file__).resolve().parents[2]
OBSERVED = ROOT/'docs/evidence/serve-speaker-http-20261118'


class SpeakerProfiles(unittest.TestCase):
    setUp = old.AudioAuxiliaryProfiles.setUp
    stamp = old.AudioAuxiliaryProfiles.stamp
    selected = old.AudioAuxiliaryProfiles.selected
    selected_auxiliary = old.AudioAuxiliaryProfiles.selected_auxiliary
    audio = old.AudioAuxiliaryProfiles.audio
    auxiliary_audio = old.AudioAuxiliaryProfiles.auxiliary_audio
    asr_extended = old.AudioAuxiliaryProfiles.asr_extended
    auxiliary_extended = old.AudioAuxiliaryProfiles.extended
    pending_for = old.AudioAuxiliaryProfiles.pending_for

    def selected_speaker(self):
        value = self.selected_auxiliary()
        value['speaker'] = dict(package.NATIVE_SPEAKER, directory='/optional/speaker',
                                limits=dict(package.SPEAKER_LIMITS, max_windows=8))
        return value

    def witness(self, identity):
        config = json.loads((OBSERVED/'final-config.wire').read_text())
        return dict(source_sha256=evidence.SPEAKER_FILE_SHA,
            request=evidence.speaker_request(identity),
            response=json.loads((OBSERVED/'js-returning.wire').read_text()),
            health=config['native_speaker_health'], limits=config['native_speaker_limits'])

    def extended(self):
        _, _, prior, original = self.auxiliary_extended()
        following = copy.deepcopy(prior)
        following['identity']['native_asr'] = contract.asr_binding(self.selected_speaker())
        records = {n:copy.deepcopy(v['record']) for n,v in original.items()}
        observed = json.loads((OBSERVED/'capabilities.json').read_text())['transcriptions']
        for row in records.values():
            row['format'] = evidence.SPEAKER_FORMAT
            caps = row['capabilities']['transcriptions']
            for key in ('speaker','formats','stream_formats','segments',
                        'chunking_strategy','remaining_native_work'):
                caps[key] = copy.deepcopy(observed[key])
            row['business']['speaker'] = self.witness(following['identity'])
        return prior, original, following, old.old.previous.bind_records(following, records)

    def test_actual_recurring_voice_witness_rejects_fake_labels_times_usage_and_health(self):
        _, _, registry, _ = self.extended()
        value = self.witness(registry['identity'])
        evidence.speaker_business(value, registry['identity'])
        faults = [lambda v:v.update(source_sha256='0'*64),
            lambda v:v['request'].update(response_format='json'),
            lambda v:v['response']['segments'][1].update(speaker='A'),
            lambda v:v['response']['segments'][2].update(speaker='C'),
            lambda v:v['response']['segments'][0].update(start=float('nan')),
            lambda v:v['response']['segments'][1].update(start=0),
            lambda v:v['response']['segments'].pop(),
            lambda v:v['response'].update(text='fake'),
            lambda v:v['response']['usage'].update(total_tokens=0),
            lambda v:v['response']['usage']['input_token_details'].update(audio_tokens=True),
            lambda v:v['limits'].update(max_windows=9),
            lambda v:v['health'].update(device_bytes=0),
            lambda v:v['health'].update(core_readiness_dependency=True),
            lambda v:v['health'].update(compute_failures=False)]
        for mutate in faults:
            bad = copy.deepcopy(value);mutate(bad)
            with self.assertRaises(ValueError): evidence.speaker_business(bad, registry['identity'])

    def test_qualification_requires_speaker_work_and_bound_backend(self):
        _, _, registry, records = self.extended()
        row = records['primary']['record']
        evidence.validate(row, registry, 'primary', records['primary']['sha256'])
        bad = copy.deepcopy(row);del bad['business']['speaker']
        with self.assertRaises(ValueError): evidence.business(bad['business'], bad['identity'], bad['capacity'])
        for key, invalid in [('configured', False), ('revision', 'other')]:
            bad = copy.deepcopy(row);bad['capabilities']['transcriptions']['speaker'][key] = invalid
            with self.assertRaises(ValueError):
                contract.qualification('primary', bad['capacity'], bad['identity'],
                    bad['capabilities'], bad['configuration'], bad['startup_memory'], 'a'*64)
        self.assertEqual(evidence.format_for(row['capacity'], row['identity']), evidence.SPEAKER_FORMAT)

    def test_explicit_speaker_addition_preserves_history_pending_and_cooldown(self):
        prior, original, registry, records = self.extended()
        pending = self.pending_for(prior)
        for options in ({}, dict(automatic=True, allow_capability_extension=True)):
            with self.assertRaises(ValueError):
                migration.permit(prior, registry, original, records, self.config, self.config, **options)
        grant = migration.permit(prior, registry, original, records, self.config, self.config,
                                   allow_capability_extension=True)
        self.assertEqual(grant['format'], migration.AUDIO_EXTENSION_FORMAT)
        result = migration.candidate(pending, grant, self.stamp(999999))
        for key in ['history','pending','not_before_ns','selected','profiles',
                    'completed_generation','light_since_ns']:
            self.assertEqual(result[key], pending[key])
        self.assertEqual(result['clock']['logical_ns'], pending['clock']['logical_ns'])
        self.assertEqual(result['clock']['boot_id'], pending['clock']['boot_id'])
        self.assertEqual(result['clock']['monotonic_ns'], 999999 * 1000000000)
        migration.AuthorizedContract(grant).transition(pending, result)
        caps = records['primary']['record']['capabilities']
        faults = [lambda v:v['transcriptions'].pop('speaker'),
            lambda v:v['transcriptions']['speaker'].update(reference_scope='global'),
            lambda v:v['transcriptions']['formats'].append('invented'),
            lambda v:v['transcriptions'].update(stream_formats=['json','text']),
            lambda v:v['transcriptions'].update(remaining_native_work=[])]
        for mutate in faults:
            bad = copy.deepcopy(caps);mutate(bad)
            with self.assertRaises(ValueError): migration.semantic_capabilities(bad)

    def test_speaker_budget_migration_is_explicit_and_keeps_consumed_state(self):
        _, _, registry, records = self.extended()
        following = copy.deepcopy(registry)
        following['identity']['native_asr']['speaker']['limits']['max_windows'] = 9
        values = {n:copy.deepcopy(v['record']) for n,v in records.items()}
        for value in values.values():
            value['business']['speaker']['limits']['max_windows'] = 9
        changed = old.old.previous.bind_records(following, values)
        with self.assertRaises(ValueError):
            migration.permit(registry, following, records, changed, self.config, self.config)
        grant = migration.permit(registry, following, records, changed, self.config, self.config,
                                   allow_capacity_change=True)
        self.assertEqual(grant['format'], migration.BACKEND_CAPACITY_FORMAT)
        pending = self.pending_for(registry)
        result = migration.candidate(pending, grant, self.stamp(999999))
        for key in ['history','pending','not_before_ns']:
            self.assertEqual(result[key], pending[key])
        migration.AuthorizedContract(grant).transition(pending, result)
        with self.assertRaises(ValueError): migration.validate(dict(grant, automatic=True))

    def test_automatic_recovery_cannot_drop_speaker_or_change_its_budget(self):
        prior, original, registry, records = self.extended()
        following = copy.deepcopy(registry)
        following['identity'].update(binary_sha256='e'*64, protocol_sha256='f'*64)
        changed = old.old.previous.bind_records(following,
            {n:copy.deepcopy(v['record']) for n,v in records.items()})
        grant = migration.permit(registry, following, records, changed,
                                   self.config, self.config, automatic=True)
        self.assertTrue(grant['automatic'])
        for automatic in (True, False):
            with self.assertRaises(ValueError):
                migration.permit(registry, prior, records, original, self.config, self.config,
                    automatic=automatic, allow_capability_extension=True, allow_capacity_change=True)
        target = EmbeddingTargetTests().target
        native = self.selected_speaker();moved = copy.deepcopy(native)
        moved['speaker']['directory'] = '/relocated/speaker'
        target(dict(native_asr=native), dict(native_asr=moved))
        self.assertEqual(contract.asr_binding(native), contract.asr_binding(moved))
        for change in ('remove','identity'):
            bad = copy.deepcopy(moved)
            if change == 'remove':del bad['speaker']
            else:bad['speaker']['revision'] = 'other'
            with self.assertRaisesRegex(ValueError, 'accepted audio auxiliary'):
                target(dict(native_asr=native), dict(native_asr=bad))

    def test_native_cli_and_primary_light_commands_keep_speaker_budget_and_audio_count(self):
        arguments = ['prepare']
        for name in ('binary','release','model','guardian','cuda-probe','policy',
                     'backend-policy','native-receipt','output','native-ingress'):
            arguments += ['--'+name, str(self.fixture.directory/name)]
        with mock.patch.object(service.bundle, 'prepare', return_value={}) as prepare, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(service.main([*arguments,'--asr-model-dir','/audio',
                '--aligner-model-dir','/align','--vad-model-dir','/vad',
                '--speaker-model-dir','/speaker','--speaker-max-windows','8']),0)
        self.assertEqual(prepare.call_args.kwargs['asr_auxiliary']['speaker']['limits']['max_windows'],8)
        _, _, registry, _ = self.extended();native = self.selected_speaker()
        base = dict(model_dir='/model', settings=dict(profile=registry['profiles']['primary'],
            native_asr=native, port=10000, restart_limit=0))
        plan = dict(base=base, profiles=registry['profiles'], port=10000,
                    operations_port=10001, binary='/candidate')
        for name in contract.NAMES:
            command = driver.command(plan, name, '/bundle')
            for key, value in dict(model_dir=native['speaker']['directory'], **native['speaker']['limits']).items():
                flag = '--speaker-'+key.replace('_','-')
                self.assertEqual(command.count(flag),1)
                self.assertEqual(command[command.index(flag)+1],str(value))
        profile = registry['profiles']['primary']
        self.assertEqual(driver.limits_for(profile,native)['chats'],
                         driver.limits_for(profile,self.selected_auxiliary())['chats']+2)


if __name__ == '__main__':
    unittest.main()
