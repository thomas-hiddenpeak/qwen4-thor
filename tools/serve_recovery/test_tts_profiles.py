"""TTS recovery contracts against retained real audio; no GPU qualification."""
import base64
import contextlib
import copy
import io
import json
from pathlib import Path
import tarfile
import unittest
from unittest import mock

from tools.serve_recovery import ledger, profile_contract as contract
from tools.serve_recovery import profile_evidence as evidence
from tools.serve_recovery import profile_migration as migration, profile_policy
from tools.serve_recovery import profile_acceptance as driver, service
from tools.serve_recovery import test_profile_migration as previous
from tools.serve_recovery.test_deployment import EmbeddingTargetTests
from tools.serve_recovery.test_asr_profiles import AsrProfiles
from tools.serve_service import package

ROOT = Path(__file__).resolve().parents[2]


def retained():
    # These copies are CPU validator fixtures, never new qualification receipts.
    archive = ROOT/'docs/evidence/serve-chat-audio-output-http-20261118/records.tar.gz'
    with tarfile.open(archive, 'r:gz') as tar:
        def read(name):
            return tar.extractfile('chat-http-06/'+name).read()
        caps = json.loads(read('model-1-capabilities.wire'))
        config = json.loads(read('model-1-config.wire'))
        value = dict(requests=evidence.tts_requests(),
            speech_base64=base64.b64encode(read('speech.mp3')).decode(),
            chat_response=json.loads(read('json.wire')), limits=config['native_tts_limits'],
            health=dict(state='ready', model=package.NATIVE_TTS['model'],
                core_readiness_dependency=False, reason='none',
                compute_failures=0, device_bytes=8123997956))
    caps['speech'].update(package.NATIVE_TTS)
    return value, {k:caps[k] for k in ('speech', 'chat_audio_output')}


class TtsProfiles(unittest.TestCase):
    setUp = previous.MigrationTests.setUp
    stamp = previous.MigrationTests.stamp

    def selected(self):
        value, _ = retained()
        return dict(package.NATIVE_TTS, directory='/optional/tts', limits=value['limits'])

    def extended(self):
        registry = copy.deepcopy(self.new)
        registry['identity']['native_tts'] = contract.tts_binding(self.selected())
        records = {n:copy.deepcopy(x['record']) for n,x in self.second.items()}
        for record in records.values():
            value, caps = retained()
            record['format'] = evidence.TTS_FORMAT
            record['capabilities'].update(caps)
            record['business']['tts'] = value
        return registry, previous.bind_records(registry, records)

    def test_binding_preserves_legacy_and_paired_weights_but_not_deployment_path(self):
        old = self.old['identity'];self.assertEqual(contract.identity(old), old)
        registry, _ = self.extended();fixed = registry['identity']
        self.assertEqual(contract.identity(fixed), fixed)
        native = dict(old['native_embedding'], directory='/embedding') \
            if old['native_embedding'] is not None else None
        self.assertEqual(contract.binding(fixed['binary_sha256'], fixed['protocol_sha256'],
            fixed['model_files'], fixed['model_assets'], native,
            native_tts=dict(self.selected(), directory='/relocated')), fixed)
        for value in [None, {}, dict(fixed['native_tts'], codec_weight_sha256='0'*64)]:
            with self.assertRaises((ValueError, RuntimeError)):
                contract.identity(dict(fixed, native_tts=value))

    def test_whole_audio_evidence_rejects_silence_forgery_and_budget_or_health_drift(self):
        registry, _ = self.extended();value, _ = retained()
        evidence.tts_business(value, registry['identity'])
        mutations = [
            lambda d:d.update(speech_base64=base64.b64encode(b'\0'*40704).decode()),
            lambda d:d['chat_response']['choices'][0]['message']['audio'].update(transcript='fake'),
            lambda d:d['chat_response']['choices'][0]['message']['audio'].update(data=''),
            lambda d:d['chat_response']['usage'].update(total_tokens=1),
            lambda d:d['chat_response']['usage']['q4t_audio_synthesis'].update(output_tokens=1),
            lambda d:d['health'].update(core_readiness_dependency=True),
            lambda d:d['health'].update(compute_failures=False),
            lambda d:d['limits'].update(max_frames=159),
            lambda d:d['requests']['speech'].update(speed=4)]
        for mutate in mutations:
            bad = copy.deepcopy(value);mutate(bad)
            with self.assertRaises(ValueError):evidence.tts_business(bad, registry['identity'])

    def test_profile_cannot_omit_spoken_business_or_misbind_the_codec(self):
        registry, rows = self.extended();row = rows['primary']['record']
        evidence.validate(row, registry, 'primary', rows['primary']['sha256'])
        bad = copy.deepcopy(row);del bad['business']['tts']
        with self.assertRaises(ValueError):
            evidence.validate(bad, registry, 'primary', rows['primary']['sha256'])
        for key, value in [('enabled', False), ('codec_weight_sha256', '0'*64)]:
            bad = copy.deepcopy(row);bad['capabilities']['speech'][key] = value
            with self.assertRaises(ValueError):
                contract.qualification('primary', bad['capacity'], bad['identity'],
                    bad['capabilities'], bad['configuration'], bad['startup_memory'], 'a'*64)
        with self.assertRaisesRegex(ValueError, 'lacks qualification'):
            contract.tts_capability_binding(None, row['capabilities'])

    def test_explicit_addition_keeps_pending_recovery_budget_and_never_downgrades(self):
        registry, rows = self.extended()
        for options in ({}, dict(automatic=True, allow_capability_extension=True)):
            with self.assertRaises(ValueError):
                migration.permit(self.old, registry, self.first, rows,
                    self.config, self.config, **options)
        grant = migration.permit(self.old, registry, self.first, rows,
            self.config, self.config, allow_capability_extension=True)
        value = migration.candidate(self.pending, grant, self.stamp(999999))
        for key in ['history', 'pending', 'not_before_ns', 'selected', 'profiles',
                    'completed_generation', 'light_since_ns']:
            self.assertEqual(value[key], self.pending[key])
        self.assertEqual(value['clock']['logical_ns'], self.pending['clock']['logical_ns'])
        migration.AuthorizedContract(grant).transition(self.pending, value)
        with self.assertRaises(ValueError):
            migration.permit(registry, self.old, rows, self.first, self.config, self.config,
                             allow_capability_extension=True)

    def test_disabled_to_enabled_is_explicit_and_all_other_capabilities_stay_bound(self):
        registry, rows = self.extended()
        records = {n:copy.deepcopy(x['record']) for n,x in self.first.items()}
        for record in records.values():
            _, caps = retained()
            for v in caps.values():v['enabled'] = False
            record['capabilities'].update(caps)
        prior = previous.bind_records(self.old, records)
        grant = migration.permit(self.old, registry, prior, rows, self.config, self.config,
                                 allow_capability_extension=True)
        self.assertEqual(grant['format'], migration.EXTENSION_FORMAT)
        for options in ({}, dict(automatic=True, allow_capability_extension=True)):
            with self.assertRaises(ValueError):
                migration.permit(self.old, registry, prior, rows,
                                 self.config, self.config, **options)

    def test_automatic_version_recovery_requires_identical_tts_identity_and_budgets(self):
        registry, rows = self.extended();newer = copy.deepcopy(registry)
        newer['identity'].update(binary_sha256='e'*64, protocol_sha256='f'*64)
        records = {n:copy.deepcopy(r['record']) for n,r in rows.items()}
        following = previous.bind_records(newer, records)
        grant = migration.permit(registry, newer, rows, following,
                                 self.config, self.config, automatic=True)
        self.assertTrue(grant['automatic'])
        newer['identity']['native_tts']['limits']['max_frames'] = 161
        for record in records.values():record['business']['tts']['limits']['max_frames'] = 161
        following = previous.bind_records(newer, records)
        with self.assertRaises(ValueError):
            migration.permit(registry, newer, rows, following, self.config, self.config,
                             automatic=True, allow_capacity_change=True)

    def test_explicit_budget_change_retains_original_history_qualification(self):
        registry, rows = self.extended();newer = copy.deepcopy(registry)
        initial = profile_policy.initial(registry, self.stamp(0), self.config)
        pending = profile_policy.prepare(initial, 'light', 'sustained_memory_pressure',
            self.pending['pending']['request_id'], 7, self.stamp(1))
        newer['identity']['native_tts']['limits']['max_frames'] = 161
        records = {n:copy.deepcopy(r['record']) for n,r in rows.items()}
        for record in records.values():record['business']['tts']['limits']['max_frames'] = 161
        following = previous.bind_records(newer, records)
        grant = migration.permit(registry, newer, rows, following, self.config, self.config,
                                 allow_capacity_change=True)
        self.assertEqual(grant['format'], migration.TTS_CAPACITY_FORMAT)
        value = migration.candidate(pending, grant, self.stamp(999999))
        for key in ['history', 'pending', 'not_before_ns', 'selected', 'profiles',
                    'completed_generation', 'light_since_ns']:
            self.assertEqual(value[key], pending[key])
        self.assertEqual(value['history_bindings'][pending['pending']['request_id']]
                         ['registry_sha256'], contract.digest(registry))
        migration.AuthorizedContract(grant).transition(pending, value)
        for bad in [dict(grant, automatic=True),
                    dict(grant, to_native_tts=grant['from_native_tts'])]:
            with self.assertRaises(ValueError):migration.validate(bad)

    def test_combined_asr_addition_and_tts_budget_migration_has_both_evidence(self):
        registry, rows = self.extended();newer = copy.deepcopy(registry)
        newer['identity']['native_tts']['limits']['max_frames'] = 161
        newer['identity']['native_asr'] = dict(package.NATIVE_ASR,
            limits=dict(package.ASR_LIMITS, max_output_tokens=128))
        records = {n:copy.deepcopy(r['record']) for n,r in rows.items()}
        for record in records.values():
            record['business']['tts']['limits']['max_frames'] = 161
            record['business']['asr'] = AsrProfiles().audio()
            record['capabilities']['transcriptions'] = dict(package.NATIVE_ASR, configured=True)
        following = previous.bind_records(newer, records)
        grant = migration.permit(registry, newer, rows, following, self.config, self.config,
            allow_capacity_change=True, allow_capability_extension=True)
        self.assertEqual(grant['format'], migration.TTS_CAPACITY_FORMAT)
        self.assertIsNone(grant['from_native_asr'])
        bad = copy.deepcopy(grant);bad['to_native_asr']['weight_sha256'] = '0'*64
        with self.assertRaises((ValueError, RuntimeError)):migration.validate(bad)

    def test_native_cli_and_both_profile_commands_keep_tts(self):
        arguments = ['prepare']
        for name in ('binary', 'release', 'model', 'guardian', 'cuda-probe',
                     'policy', 'backend-policy', 'native-receipt', 'output', 'native-ingress'):
            arguments += ['--'+name, str(self.fixture.directory/name)]
        with mock.patch.object(service.bundle, 'prepare', return_value={}) as prepare, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(service.main([*arguments, '--tts-model-dir', '/optional/tts',
                '--tts-max-frames', '160', '--tts-workspace-mib', '2048']), 0)
        self.assertEqual(prepare.call_args.kwargs['tts_model_dir'], Path('/optional/tts'))
        self.assertEqual(prepare.call_args.kwargs['tts_limits'],
                         dict(max_frames=160, workspace_mib=2048))
        registry, _ = self.extended()
        base = dict(model_dir='/model', settings=dict(profile=registry['profiles']['primary'],
            native_tts=self.selected(), port=10000, restart_limit=0))
        plan = dict(base=base, profiles=registry['profiles'], port=10000,
                    operations_port=10001, binary='/candidate')
        for name in contract.NAMES:
            args = driver.command(plan, name, '/bundle')
            self.assertIn('--mtp' if name == 'primary' else '--no-mtp', args)
            for key, value in {'model_dir':'/optional/tts', **self.selected()['limits']}.items():
                flag = '--tts-'+key.replace('_', '-')
                self.assertEqual(args.count(flag), 1)
                self.assertEqual(args[args.index(flag)+1], str(value))
        profile = registry['profiles']['primary']
        self.assertEqual(driver.limits_for(profile, native_tts=self.selected())['chats'],
                         driver.limits_for(profile)['chats']+4)
        self.assertEqual(evidence.format_for(profile, registry['identity']), evidence.TTS_FORMAT)

    def test_driver_audio_budget_covers_full_frames_without_changing_text_fixture(self):
        stage = object.__new__(driver.Stage)
        stage.plan = dict(base=dict(settings={}), profiles=self.old['profiles'])
        self.assertEqual(stage.client_policy()['clients'][0]['max_output_tokens'], 512)
        stage.plan['base']['settings']['native_tts'] = self.selected()
        policy = stage.client_policy()['clients'][0]
        self.assertEqual(policy['max_output_tokens'],
            self.old['profiles']['primary']['max_tokens']+160*16+1)
        self.assertEqual(policy['tokens_per_minute'], 32768)

    def test_deployment_preserves_talker_codec_and_allows_only_asset_relocation(self):
        target = EmbeddingTargetTests().target;selected = self.selected()
        target({}, dict(native_tts=selected))
        target(dict(native_tts=selected), dict(native_tts=dict(selected, directory='/relocated')))
        for native in [None, dict(selected, revision='different'),
                       dict(selected, codec_weight_sha256='0'*64)]:
            with self.assertRaisesRegex(ValueError, 'accepted TTS'):
                target(dict(native_tts=selected), {} if native is None else dict(native_tts=native))


if __name__ == '__main__':unittest.main()
