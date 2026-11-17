"""ASR recovery bindings and retained wire; fixtures never qualify a GPU run."""
import base64
import copy
import contextlib
import io
import json
from pathlib import Path
import unittest
from unittest import mock

from tools.serve_recovery import ledger, profile_contract as contract
from tools.serve_recovery import profile_evidence as evidence
from tools.serve_recovery import profile_migration as migration, profile_policy
from tools.serve_recovery import profile_acceptance as driver
from tools.serve_recovery import test_profile_migration as previous
from tools.serve_recovery import service
from tools.serve_recovery.test_deployment import EmbeddingTargetTests
from tools.serve_service import package

ROOT = Path(__file__).resolve().parents[2]


class AsrProfiles(unittest.TestCase):
    setUp = previous.MigrationTests.setUp
    stamp = previous.MigrationTests.stamp

    def selected(self):
        return dict(package.NATIVE_ASR, directory='/optional/asr',
                    limits=dict(package.ASR_LIMITS, max_output_tokens=128))

    def audio(self):
        recorded = ROOT/'docs/evidence/serve-asr-http-20261117/service-http-05'
        pcm = ROOT/'tests/fixtures/asr/asr_zh.wav'
        raw = ROOT/'docs/evidence/serve-asr-sdk-values-20261117/service-http-04/python-zh-json.wire'
        config = json.loads((recorded/'final-config.wire').read_text())
        return dict(source_base64=base64.b64encode(pcm.read_bytes()).decode(),
            request=dict(model=package.NATIVE_ASR['model'], response_format='json',
                         include=['logprobs']), response=json.loads(raw.read_text()),
            health=config['native_asr_health'], limits=config['native_asr_limits'])

    def extended(self):
        registry = copy.deepcopy(self.new)
        registry['identity']['native_asr'] = {k:copy.deepcopy(v)
            for k,v in self.selected().items() if k != 'directory'}
        records = {n:copy.deepcopy(x['record']) for n,x in self.second.items()}
        for record in records.values():
            record['format'] = evidence.ASR_FORMAT
            record['capabilities']['transcriptions'] = dict(package.NATIVE_ASR, configured=True)
            record['business']['asr'] = self.audio()
        return registry, previous.bind_records(registry, records)

    def test_legacy_binding_is_unchanged_and_new_binding_excludes_only_path(self):
        old = self.old['identity']
        self.assertEqual(contract.identity(old), old)
        self.assertNotIn('native_asr', old)
        first, _ = self.extended()
        identity = first['identity']
        self.assertEqual(contract.identity(identity), identity)
        native = dict(old['native_embedding'], directory='/optional/embedding') \
            if old['native_embedding'] is not None else None
        self.assertEqual(contract.binding(identity['binary_sha256'], identity['protocol_sha256'],
            identity['model_files'], identity['model_assets'], native,
            dict(self.selected(), directory='/relocated/asr')), identity)
        for value in [None, {}, dict(identity, native_asr=None)]:
            with self.assertRaises((ValueError, RuntimeError)):
                contract.identity(value)

    def test_actual_audio_requires_real_source_usage_probabilities_and_health(self):
        registry, _ = self.extended();fixed = registry['identity']
        value = self.audio();evidence.audio_business(value, fixed)
        mutations = [('source_base64', ''), ('response', dict(value['response'], text='fake')),
            ('health', dict(value['health'], core_readiness_dependency=True)),
            ('health', dict(value['health'], compute_failures=False)),
            ('limits', dict(value['limits'], max_output_tokens=127))]
        for key, changed in mutations:
            with self.subTest(key=key), self.assertRaises(ValueError):
                evidence.audio_business(dict(value, **{key:changed}), fixed)
        for delta in [dict(usage=dict(value['response']['usage'], total_tokens=1)),
                      dict(logprobs=[]), dict(usage={'type':'seconds'})]:
            with self.assertRaises(ValueError):
                evidence.audio_business(dict(value, response=dict(value['response'], **delta)), fixed)

    def test_profile_receipt_cannot_omit_audio_or_bind_another_backend(self):
        registry, rows = self.extended()
        row = rows['primary']['record']
        evidence.validate(row, registry, 'primary', rows['primary']['sha256'])
        bad = copy.deepcopy(row);del bad['business']['asr']
        with self.assertRaises(ValueError):
            evidence.validate(bad, registry, 'primary', rows['primary']['sha256'])
        for change in [dict(configured=False), dict(revision='other')]:
            bad = copy.deepcopy(row)
            bad['capabilities']['transcriptions'].update(change)
            with self.assertRaises(ValueError):
                contract.qualification('primary', bad['capacity'], bad['identity'],
                    bad['capabilities'], bad['configuration'], bad['startup_memory'], 'a'*64)

    def test_explicit_asr_addition_preserves_pending_work_and_consumed_budget(self):
        registry, rows = self.extended()
        original = copy.deepcopy((self.old, self.first, self.pending))
        grant = migration.permit(self.old, registry, self.first, rows,
            self.config, self.config, allow_capability_extension=True)
        state = migration.candidate(self.pending, grant, self.stamp(999999))
        for key in ['history', 'pending', 'not_before_ns', 'selected', 'profiles',
                    'completed_generation', 'light_since_ns']:
            self.assertEqual(state[key], self.pending[key])
        self.assertEqual(state['clock']['logical_ns'], self.pending['clock']['logical_ns'])
        self.assertEqual((self.old, self.first, self.pending), original)
        migration.AuthorizedContract(grant).transition(self.pending, state)
        with self.assertRaises(ValueError):
            profile_policy.transition(self.pending, state)

    def test_disabled_backend_activation_is_explicit_and_preserves_other_capabilities(self):
        registry, rows = self.extended()
        records = {n:copy.deepcopy(x['record']) for n,x in self.first.items()}
        for record in records.values():
            record['capabilities']['transcriptions'] = dict(package.NATIVE_ASR, configured=False)
        prior = previous.bind_records(self.old, records)
        for options in ({}, dict(automatic=True, allow_capability_extension=True)):
            with self.assertRaises(ValueError):
                migration.permit(self.old, registry, prior, rows, self.config, self.config,
                                 **options)
        grant = migration.permit(self.old, registry, prior, rows, self.config, self.config,
                                  allow_capability_extension=True)
        self.assertEqual(grant['format'], migration.EXTENSION_FORMAT)
        with self.assertRaises(ValueError):
            migration.permit(registry, self.old, rows, prior, self.config, self.config,
                             allow_capability_extension=True)

    def test_explicit_asr_budget_migration_preserves_original_qualification_and_wait(self):
        registry, rows = self.extended()
        initial = profile_policy.initial(registry, self.stamp(0), self.config)
        pending = profile_policy.prepare(initial, 'light', 'sustained_memory_pressure',
            self.pending['pending']['request_id'], 7, self.stamp(1))
        newer = copy.deepcopy(registry)
        newer['identity']['native_asr']['limits']['max_output_tokens'] = 256
        records = {n:copy.deepcopy(x['record']) for n,x in rows.items()}
        for record in records.values():
            record['business']['asr']['limits']['max_output_tokens'] = 256
        following = previous.bind_records(newer, records)
        for options in ({}, dict(automatic=True, allow_capacity_change=True)):
            with self.assertRaises(ValueError):
                migration.permit(registry, newer, rows, following, self.config, self.config,
                                 **options)
        grant = migration.permit(registry, newer, rows, following, self.config, self.config,
                                  allow_capacity_change=True)
        self.assertEqual(grant['format'], migration.BACKEND_CAPACITY_FORMAT)
        value = migration.candidate(pending, grant, self.stamp(999999))
        for key in ['history', 'pending', 'not_before_ns', 'selected', 'profiles',
                    'completed_generation', 'light_since_ns']:
            self.assertEqual(value[key], pending[key])
        self.assertEqual(value['clock']['logical_ns'], pending['clock']['logical_ns'])
        self.assertEqual(value['history_bindings'][pending['pending']['request_id']]
                         ['registry_sha256'], contract.digest(registry))
        migration.AuthorizedContract(grant).transition(pending, value)
        for bad in [dict(grant, automatic=True),
                    dict(grant, to_native_asr=grant['from_native_asr'])]:
            with self.assertRaises(ValueError):
                migration.validate(bad)

    def test_native_cli_forwards_optional_asr_identity_and_only_explicit_limits(self):
        arguments = ['prepare']
        for name in ('binary', 'release', 'model', 'guardian', 'cuda-probe',
                     'policy', 'backend-policy', 'native-receipt', 'output', 'native-ingress'):
            arguments += ['--'+name, str(self.fixture.directory/name)]
        with mock.patch.object(service.bundle, 'prepare', return_value={}) as prepare, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(service.main([*arguments, '--asr-model-dir', '/optional/asr',
                '--asr-max-output-tokens', '128', '--asr-workspace-mib', '512']), 0)
        self.assertEqual(prepare.call_args.kwargs['asr_model_dir'], Path('/optional/asr'))
        self.assertEqual(prepare.call_args.kwargs['asr_limits'],
                         dict(max_output_tokens=128, workspace_mib=512))

    def test_automatic_migration_preserves_audio_and_rejects_addition_or_loss(self):
        registry, rows = self.extended()
        for old, new, before, after in [(self.old, registry, self.first, rows),
                                        (registry, self.old, rows, self.first)]:
            with self.assertRaises(ValueError):
                migration.permit(old, new, before, after, self.config, self.config,
                    automatic=True, allow_capability_extension=True)
        newer = copy.deepcopy(registry)
        newer['identity'].update(binary_sha256='e'*64, protocol_sha256='f'*64)
        following = previous.bind_records(newer,
            {n:copy.deepcopy(r['record']) for n,r in rows.items()})
        grant = migration.permit(registry, newer, rows, following,
                                 self.config, self.config, automatic=True)
        self.assertTrue(grant['automatic'])

    def test_deployment_cannot_drop_accepted_asr_and_allows_asset_relocation(self):
        target = EmbeddingTargetTests().target
        selected = self.selected()
        target({}, dict(native_asr=selected))
        target(dict(native_asr=selected), dict(native_asr=dict(selected, directory='/relocated')))
        for native in [None, dict(selected, revision='different')]:
            with self.assertRaisesRegex(ValueError, 'accepted ASR'):
                target(dict(native_asr=selected), {} if native is None else dict(native_asr=native))

    def test_driver_preserves_asr_parameters_in_both_real_commands(self):
        registry, _ = self.extended()
        base = dict(model_dir='/model', settings=dict(profile=registry['profiles']['primary'],
            native_asr=self.selected(), port=10000, restart_limit=0))
        plan = dict(base=base, profiles=registry['profiles'], port=10000,
                    operations_port=10001, binary='/candidate')
        for name in contract.NAMES:
            args = driver.command(plan, name, '/bundle')
            self.assertIn('--mtp' if name == 'primary' else '--no-mtp', args)
            for key, value in {'model_dir':'/optional/asr', **self.selected()['limits']}.items():
                flag = '--asr-'+key.replace('_', '-')
                self.assertEqual(args.count(flag), 1)
                self.assertEqual(args[args.index(flag)+1], str(value))
        self.assertEqual(driver.limits_for(registry['profiles']['primary'], self.selected())['chats'],
            driver.limits_for(registry['profiles']['primary'])['chats']+2)
        self.assertEqual(evidence.format_for(registry['profiles']['primary'], registry['identity']),
                         evidence.ASR_FORMAT)


if __name__ == '__main__':
    unittest.main()
