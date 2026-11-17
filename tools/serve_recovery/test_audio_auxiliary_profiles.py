"""Actual retained audio and synthetic recovery registries; no GPU claims."""
import base64
import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import unittest
from unittest import mock

from tools.serve_recovery import profile_contract as contract
from tools.serve_recovery import profile_evidence as evidence
from tools.serve_recovery import profile_migration as migration, profile_policy as policy
from tools.serve_recovery import profile_acceptance as driver, service
from tools.serve_recovery import test_asr_profiles as old
from tools.serve_recovery import test_deployment
from tools.serve_service import package

ROOT = Path(__file__).resolve().parents[2]
OBSERVED = ROOT/'docs/evidence/serve-vad-http-20261117'
# These retained v4 records predate the speaker backend. Preserve that scope.
AUXILIARIES = {k:package.ASR_AUXILIARY[k] for k in ('alignment', 'vad')}


class AudioAuxiliaryProfiles(unittest.TestCase):
    setUp = old.AsrProfiles.setUp
    stamp = old.AsrProfiles.stamp
    selected = old.AsrProfiles.selected
    audio = old.AsrProfiles.audio
    asr_extended = old.AsrProfiles.extended

    def selected_auxiliary(self):
        result = self.selected()
        for name, (fixed, defaults, _) in AUXILIARIES.items():
            result[name] = dict(fixed, directory='/optional/'+name,
                                limits=dict(defaults))
        result['vad']['limits']['max_chunks'] = 3
        return result

    def auxiliary_audio(self, identity):
        source = evidence.auxiliary_audio_source((ROOT/'tests/fixtures/asr/asr_zh.wav').read_bytes(), identity)
        config = json.loads((OBSERVED/'final-config.wire').read_text())
        return dict(source_base64=base64.b64encode(source).decode(),
            request=evidence.auxiliary_audio_request(identity),
            response=json.loads((OBSERVED/'python-repeated-segments.wire').read_text()),
            health={k:config['native_'+k+'_health'] for k in ('alignment','vad')},
            limits=dict(alignment=config['native_alignment_limits'],
                        vad=dict(max_chunks=config['native_vad_max_chunks'])))

    def extended(self):
        prior, original = self.asr_extended()
        registry = copy.deepcopy(prior)
        registry['identity']['native_asr'] = contract.asr_binding(self.selected_auxiliary())
        records = {n:copy.deepcopy(row['record']) for n,row in original.items()}
        for record in records.values():
            record['format'] = evidence.AUDIO_AUXILIARY_FORMAT
            caps = record['capabilities']['transcriptions']
            for name, (fixed, _, _) in AUXILIARIES.items():
                caps[name] = dict(fixed, configured=True)
            caps['vad']['max_chunks'] = 3
            record['business']['asr_auxiliary'] = self.auxiliary_audio(registry['identity'])
        return prior, original, registry, old.previous.bind_records(registry, records)

    def pending_for(self, registry):
        initial = policy.initial(registry, self.stamp(0), self.config)
        return policy.prepare(initial, 'light', 'sustained_memory_pressure',
            self.pending['pending']['request_id'], 7, self.stamp(1))

    def test_all_auxiliary_paths_relocate_but_fixed_models_and_limits_remain_bound(self):
        selected = self.selected_auxiliary()
        fixed = contract.asr_binding(selected)
        self.assertNotIn('directory', fixed)
        self.assertTrue(all('directory' not in fixed[k] for k in AUXILIARIES))
        moved = copy.deepcopy(selected);moved['directory'] = '/moved/asr'
        for name in AUXILIARIES: moved[name]['directory'] = '/moved/'+name
        self.assertEqual(contract.asr_binding(moved), fixed)
        self.assertEqual(contract.asr_binding(contract.asr_configuration(fixed)), fixed)
        for name in AUXILIARIES:
            bad = copy.deepcopy(fixed);bad[name]['directory'] = '/must-not-be-qualified'
            with self.assertRaises(ValueError): contract.asr_configuration(bad)
            bad = copy.deepcopy(fixed);bad[name]['weight_sha256'] = '0'*64
            with self.assertRaises((ValueError, RuntimeError)): contract.asr_configuration(bad)

    def test_real_multiclip_witness_rejects_missing_times_tokens_stats_health_and_source(self):
        _, _, registry, _ = self.extended();identity = registry['identity']
        value = self.auxiliary_audio(identity)
        evidence.audio_auxiliary_business(value, identity)
        self.assertEqual(hashlib.sha256(base64.b64decode(value['source_base64'])).hexdigest(), evidence.AUDIO_PAIR_SHA)
        faults = [lambda v: v.update(source_base64=''),
            lambda v: v['response'].update(text='fake'),
            lambda v: v['response']['segments'][1].update(seek=0),
            lambda v: v['response']['segments'][1].update(tokens=[0]),
            lambda v: v['response']['segments'][0].update(compression_ratio=1),
            lambda v: v['response']['segments'][1].update(no_speech_prob=float('nan')),
            lambda v: v['response']['words'][-1].update(end=100),
            lambda v: v['health']['vad'].update(core_readiness_dependency=True),
            lambda v: v['health']['vad'].update(compute_failures=False),
            lambda v: v['limits']['vad'].update(max_chunks=2),
            lambda v: v['health']['vad'].update(device_bytes=1)]
        for mutate in faults:
            bad = copy.deepcopy(value);mutate(bad)
            with self.assertRaises(ValueError): evidence.audio_auxiliary_business(bad, identity)

    def test_qualification_requires_auxiliary_business_and_exact_declared_capacity(self):
        _, _, registry, records = self.extended()
        row = records['primary']['record']
        evidence.validate(row, registry, 'primary', records['primary']['sha256'])
        bad = copy.deepcopy(row);del bad['business']['asr_auxiliary']
        with self.assertRaises(ValueError): evidence.validate(bad, registry, 'primary', records['primary']['sha256'])
        for domain, field, value in [('vad','max_chunks',4), ('vad','configured',False),
                                     ('alignment','revision','other')]:
            bad = copy.deepcopy(row);bad['capabilities']['transcriptions'][domain][field] = value
            with self.assertRaises(ValueError):
                contract.qualification('primary', bad['capacity'], bad['identity'],
                    bad['capabilities'], bad['configuration'], bad['startup_memory'], 'a'*64)
        self.assertEqual(evidence.format_for(row['capacity'], row['identity']), evidence.AUDIO_AUXILIARY_FORMAT)

    def test_explicit_auxiliary_addition_keeps_history_pending_and_all_wait(self):
        prior, original, registry, records = self.extended()
        pending = self.pending_for(prior)
        for options in ({}, dict(automatic=True, allow_capability_extension=True)):
            with self.assertRaises(ValueError):
                migration.permit(prior, registry, original, records, self.config, self.config, **options)
        grant = migration.permit(prior, registry, original, records, self.config, self.config,
                                   allow_capability_extension=True)
        self.assertEqual(grant['format'], migration.AUDIO_EXTENSION_FORMAT)
        migrated = migration.candidate(pending, grant, self.stamp(999999))
        for key in ['history','pending','not_before_ns','selected','profiles','completed_generation','light_since_ns']:
            self.assertEqual(migrated[key], pending[key])
        self.assertEqual(migrated['clock']['logical_ns'], pending['clock']['logical_ns'])
        self.assertEqual(migrated['history_bindings'][pending['pending']['request_id']]['registry_sha256'], contract.digest(prior))
        migration.AuthorizedContract(grant).transition(pending, migrated)
        with self.assertRaises(ValueError): policy.transition(pending, migrated)

    def test_explicit_vad_capacity_change_retains_original_qualification_and_wait(self):
        _, _, registry, records = self.extended()
        pending = self.pending_for(registry)
        following = copy.deepcopy(registry)
        following['identity']['native_asr']['vad']['limits']['max_chunks'] = 4
        values = {n:copy.deepcopy(r['record']) for n,r in records.items()}
        for value in values.values():
            value['capabilities']['transcriptions']['vad']['max_chunks'] = 4
            value['business']['asr_auxiliary']['limits']['vad']['max_chunks'] = 4
        changed = old.previous.bind_records(following, values)
        with self.assertRaises(ValueError):
            migration.permit(registry, following, records, changed, self.config, self.config)
        grant = migration.permit(registry, following, records, changed, self.config, self.config,
                                   allow_capacity_change=True)
        self.assertEqual(grant['format'], migration.BACKEND_CAPACITY_FORMAT)
        migrated = migration.candidate(pending, grant, self.stamp(999999))
        self.assertEqual(migrated['pending'], pending['pending'])
        self.assertEqual(migrated['history'], pending['history'])
        self.assertEqual(migrated['not_before_ns'], pending['not_before_ns'])
        migration.AuthorizedContract(grant).transition(pending, migrated)
        with self.assertRaises(ValueError): migration.validate(dict(grant, automatic=True))

    def test_automatic_same_features_work_and_removal_or_unsealed_addition_fail(self):
        prior, original, registry, records = self.extended()
        following = copy.deepcopy(registry)
        following['identity'].update(binary_sha256='e'*64, protocol_sha256='f'*64)
        changed = old.previous.bind_records(following,
            {n:copy.deepcopy(r['record']) for n,r in records.items()})
        grant = migration.permit(registry, following, records, changed,
                                   self.config, self.config, automatic=True)
        self.assertTrue(grant['automatic'])
        for before, after, first, second in [(registry, prior, records, original),
                                             (prior, registry, original, records)]:
            with self.assertRaises(ValueError):
                migration.permit(before, after, first, second, self.config, self.config,
                                   automatic=True, allow_capability_extension=True)
        with self.assertRaises(ValueError):
            migration.permit(registry, prior, records, original, self.config, self.config,
                               allow_capability_extension=True, allow_capacity_change=True)

    def test_deployment_preserves_accepted_auxiliary_models_and_allows_directory_moves(self):
        target = test_deployment.EmbeddingTargetTests().target
        selected = self.selected_auxiliary()
        moved = copy.deepcopy(selected)
        for name in AUXILIARIES: moved[name]['directory'] = '/moved/'+name
        target(dict(native_asr=selected), dict(native_asr=moved))
        for name in AUXILIARIES:
            for fault in ('remove','identity'):
                bad = copy.deepcopy(selected)
                if fault == 'remove': del bad[name]
                else: bad[name]['revision'] = 'different'
                with self.assertRaisesRegex(ValueError, 'accepted audio auxiliary'):
                    target(dict(native_asr=selected), dict(native_asr=bad))

    def test_native_cli_and_both_profile_commands_keep_all_auxiliary_limits(self):
        arguments = ['prepare']
        for name in ('binary','release','model','guardian','cuda-probe','policy','backend-policy',
                     'native-receipt','output','native-ingress'):
            arguments += ['--'+name, str(self.fixture.directory/name)]
        with mock.patch.object(service.bundle, 'prepare', return_value={}) as prepare, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(service.main([*arguments, '--asr-model-dir','/audio',
                '--aligner-model-dir','/align','--aligner-max-words','256',
                '--vad-model-dir','/vad','--vad-max-chunks','3']), 0)
        selected = prepare.call_args.kwargs['asr_auxiliary']
        self.assertEqual(selected['alignment']['limits']['max_words'], 256)
        self.assertEqual(selected['vad']['limits']['max_chunks'], 3)
        _, _, registry, _ = self.extended()
        native = self.selected_auxiliary()
        base = dict(model_dir='/model', settings=dict(profile=registry['profiles']['primary'],
            native_asr=native, port=10000, restart_limit=0))
        plan = dict(base=base, profiles=registry['profiles'], port=10000,
                    operations_port=10001, binary='/candidate')
        for name in contract.NAMES:
            command = driver.command(plan, name, '/bundle')
            for key, (_, _, prefix) in AUXILIARIES.items():
                for option, value in dict(model_dir=native[key]['directory'], **native[key]['limits']).items():
                    flag = '--'+prefix+'-'+option.replace('_','-')
                    self.assertEqual(command.count(flag), 1)
                    self.assertEqual(command[command.index(flag)+1], str(value))
        profile = registry['profiles']['primary']
        self.assertEqual(driver.limits_for(profile,native)['chats'], driver.limits_for(profile,self.selected())['chats']+2)

    def test_pending_declarations_become_proven_additions_without_losing_features(self):
        _, _, registry, records = self.extended()
        new = copy.deepcopy(records['primary']['record']['capabilities'])
        selected = new['transcriptions']
        selected.update(segments=dict(scope='independently_decoded_audio_clip'),
            chunking_strategy=['auto','server_vad'], remaining_native_work=['diarization'])
        first = copy.deepcopy(new)
        first['transcriptions'] = dict(package.NATIVE_ASR, configured=True,
            remaining_native_work=['alignment','diarization','vad_chunking'])
        before, after = [migration.semantic_capabilities(v) for v in (first,new)]
        self.assertTrue(migration.extends(before,after))
        self.assertFalse(migration.extends(after,before))
        self.assertNotIn('max_chunks', after['transcriptions']['vad'])
        self.assertEqual(registry['identity']['native_asr']['vad']['limits']['max_chunks'],3)
        bad = copy.deepcopy(new);del bad['transcriptions']['segments']
        with self.assertRaises(ValueError): migration.semantic_capabilities(bad)

    def test_actual_retained_primary_light_allow_only_explicit_complete_format_extension(self):
        registries, observations = [], []
        for name in ['serve-asr-profiles-20261117', 'serve-audio-auxiliary-profiles-20261117']:
            folder = ROOT/'docs/evidence'/name/'qualified'
            registries.append(json.loads((folder/'registry.json').read_text()))
            observations.append({key:dict(record=json.loads((folder/(key+'.json')).read_text()),
                sha256=hashlib.sha256((folder/(key+'.json')).read_bytes()).hexdigest()) for key in contract.NAMES})
        old_registry, new_registry = registries
        old_records, new_records = observations
        for options in ({}, dict(automatic=True, allow_capability_extension=True)):
            with self.assertRaises(ValueError):
                migration.permit(old_registry,new_registry,old_records,new_records,
                                 self.config,self.config,**options)
        permit = migration.permit(old_registry,new_registry,old_records,new_records,
            self.config,self.config,allow_capability_extension=True)
        self.assertEqual(permit['format'], migration.AUDIO_EXTENSION_FORMAT)
        pending = self.pending_for(old_registry)
        candidate = migration.candidate(pending, permit, self.stamp(999999))
        self.assertEqual(candidate['pending'], pending['pending'])
        self.assertEqual(candidate['not_before_ns'], pending['not_before_ns'])
        migration.AuthorizedContract(permit).transition(pending,candidate)
        original = new_records['primary']['record']['capabilities']
        for formats in [['json','text','srt'], ['json','text','invented'],
                        ['json','text','srt','verbose_json','vtt','json']]:
            bad = copy.deepcopy(original);bad['transcriptions']['formats'] = formats
            with self.assertRaises(ValueError):migration.semantic_capabilities(bad)
        bad = copy.deepcopy(original);del bad['transcriptions']['segments']
        with self.assertRaises(ValueError):migration.semantic_capabilities(bad)


if __name__ == '__main__':
    unittest.main()
