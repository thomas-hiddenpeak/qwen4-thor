"""Voice qualification validators. Synthesized CPU records are not GPU evidence."""
import base64
import copy
import io
import math
import struct
import unittest
import wave

from tools.serve_recovery import profile_contract as contract
from tools.serve_recovery import profile_evidence as evidence
from tools.serve_recovery import profile_migration as migration
from tools.serve_recovery.test_tts_profiles import TtsProfiles
from tools.serve_recovery.test_speaker_profiles import SpeakerProfiles
from tools.serve_recovery import test_profile_migration as previous
from tools.serve_recovery import profile_policy as policy
from tools.serve_service import package
from tools.serve_service.test_voice_package import selected, registration_asr, capabilities


def cpu_record():
    # Deterministic non-speech WAV exercises receipt shape and corruption gates.
    # Transcript strings below are synthetic assertions, never content evidence.
    buffer = io.BytesIO();frames = 4
    with wave.open(buffer, 'wb') as out:
        out.setnchannels(1);out.setsampwidth(2);out.setframerate(24000)
        out.writeframes(struct.pack('<'+'h'*(frames*1920), *[
            round(4000*math.sin(2*math.pi*300*i/24000)) for i in range(frames*1920)]))
    audio = base64.b64encode(buffer.getvalue()).decode()
    consent = dict(id='cons_cpu', object='audio.voice_consent', created_at=1,
                   name='local-consent', language='en')
    voice = dict(id='voice_cpu', object='audio.voice', created_at=2,
                 name='local-sample', type='audio_sample')
    def rejected(status, code='object_not_found'):
        return dict(status=status, body=dict(error=dict(code=code, type='invalid_request_error')))
    health = dict(state='ready', model=package.NATIVE_TTS_BASE['model'],
        reason='none', core_readiness_dependency=False, compute_failures=0)
    return dict(requests=evidence.voice_requests(consent['id'], voice['id']),
        consent=consent, voice=voice, limits=selected()['limits'],
        speech=dict(audio_base64=audio, usage=dict(input_tokens=10,
            output_tokens=65, total_tokens=75)),
        speech_transcript=dict(text=evidence.VOICE_TARGETS[0]),
        chat=dict(id='chatcmpl-cpu', choices=[dict(index=0, finish_reason='stop',
            message=dict(role='assistant', content=evidence.VOICE_TARGETS[1],
                audio=dict(id='audio_cpu', expires_at=99, transcript=evidence.VOICE_TARGETS[1], data=audio)))],
            usage=dict(prompt_tokens=30, completion_tokens=70, total_tokens=100,
                completion_tokens_details=dict(audio_tokens=64),
                q4t_audio_synthesis=dict(input_tokens=10, output_tokens=65))),
        chat_transcript=dict(text=evidence.VOICE_TARGETS[1]),
        negative=dict(negated_consent=rejected(400, 'voice_consent_mismatch'),
            different_speaker=rejected(400, 'voice_speaker_mismatch'),
            cross_consent=rejected(404), cross_voice=rejected(404),
            cross_speech=rejected(404), quota=rejected(429, 'client_tpm_exhausted')),
        revocation=dict(consent_deleted=dict(id=consent['id'], object=consent['object'], deleted=True),
            voice_deleted=dict(id=voice['id'], object=voice['object'], deleted=True),
            speech=rejected(404), consent=rejected(404)),
        health=dict(base=health, condition=dict(health, model=health['model']+'-speaker')))


class VoiceProfiles(unittest.TestCase):
    setUp = TtsProfiles.setUp
    stamp = TtsProfiles.stamp
    selected = TtsProfiles.selected
    extended = TtsProfiles.extended

    def test_identity_keeps_base_and_minimal_registration_paths_out_of_hash(self):
        fixed = copy.deepcopy(self.old['identity'])
        fixed['native_tts_base'] = contract.tts_binding(selected(), base=True)
        fixed['native_asr'] = contract.asr_binding(registration_asr(), voice_registration=True)
        self.assertEqual(contract.identity(fixed), fixed)
        native = dict(fixed['native_embedding'], directory='/embedding') if fixed['native_embedding'] else None
        relocated = dict(selected(), directory='/moved')
        actual = contract.binding(fixed['binary_sha256'], fixed['protocol_sha256'],
            fixed['model_files'], fixed['model_assets'], native,
            native_asr=registration_asr(), native_tts_base=relocated)
        self.assertEqual(actual, fixed)
        for change in [None, {}, dict(fixed['native_tts_base'], weight_sha256='0'*64)]:
            with self.assertRaises((ValueError, RuntimeError)):
                contract.identity(dict(fixed, native_tts_base=change))
        both = dict(fixed, native_tts=contract.tts_binding(self.selected()))
        both['native_tts']['limits']['max_frames'] -= 1
        with self.assertRaisesRegex(ValueError, 'share effective'):
            contract.identity(both)

    def test_capability_binding_requires_correct_families_and_registration_context(self):
        base = contract.tts_binding(selected(), base=True)
        asr = contract.asr_binding(registration_asr(), voice_registration=True)
        caps = capabilities(registration=True)
        contract.tts_capability_binding(None, caps, base, asr)
        for wrong in [None, {}]:
            with self.assertRaises(ValueError):contract.tts_capability_binding(None, caps, base, wrong)
        with self.assertRaises(ValueError):contract.tts_capability_binding(None, caps)
        mixed = capabilities(builtin=True)
        contract.tts_capability_binding(package.NATIVE_TTS, mixed, base)
        with self.assertRaises(ValueError):contract.tts_capability_binding(None, mixed, base)
        migration.tts_capabilities(capabilities())
        migration.tts_capabilities(capabilities(builtin=True))

    def test_old_builtin_evidence_cannot_qualify_registered_voices(self):
        registry, rows = self.extended()
        identity = dict(registry['identity'],
            native_tts_base=contract.tts_binding(selected(), base=True))
        body = copy.deepcopy(rows['primary']['record']['business'])
        with self.assertRaises(ValueError):evidence.business(body, identity)
        self.assertEqual(evidence.format_for({}, identity), evidence.VOICE_FORMAT)
        self.assertFalse(evidence.has_speaker(dict(native_asr=registration_asr())))
        with_alignment = dict(registration_asr(), alignment={})
        self.assertTrue(evidence.has_speaker(dict(native_asr=with_alignment)))

    def test_voice_receipt_requires_ids_audio_asr_usage_negatives_and_revocation(self):
        identity = dict(native_tts_base=contract.tts_binding(selected(), base=True),
            native_asr=contract.asr_binding(registration_asr(), voice_registration=True))
        value = cpu_record();evidence.voice_business(value, identity)
        with self.assertRaisesRegex(ValueError, 'lacks qualified'):
            evidence.voice_business(value, dict(native_tts_base=identity['native_tts_base']))
        changes = [
            lambda d:d['requests']['speech'].update(voice='ryan'),
            lambda d:d['requests']['transcription'].update(prompt=evidence.VOICE_TARGETS[0]),
            lambda d:d['requests']['voice'].update(consent='cons_other'),
            lambda d:d['consent'].update(identity=[0]*192),
            lambda d:d['speech_transcript'].update(text='The train arrives.'),
            lambda d:d['chat']['choices'][0]['message']['audio'].update(transcript='wrong'),
            lambda d:d['speech']['usage'].update(output_tokens=64),
            lambda d:d['chat']['usage']['q4t_audio_synthesis'].update(output_tokens=64),
            lambda d:d['chat'].update(usage=None),
            lambda d:d['chat']['usage'].update(completion_tokens_details=[]),
            lambda d:d['negative']['different_speaker'].update(status=200),
            lambda d:d['negative']['quota']['body']['error'].update(code='other'),
            lambda d:d['revocation']['consent_deleted'].update(id='cons_other'),
            lambda d:d['revocation']['speech'].update(status=200),
            lambda d:d['health']['condition'].update(core_readiness_dependency=True),
            lambda d:d['limits'].update(max_frames=159),
        ]
        for index, change in enumerate(changes):
            bad = copy.deepcopy(value);change(bad)
            with self.subTest(index=index), self.assertRaises(ValueError):
                evidence.voice_business(bad, identity)
        raw = bytearray(base64.b64decode(value['speech']['audio_base64']))
        for broken in [raw[:-10], raw[:44]+bytes(len(raw)-44)]:
            bad = copy.deepcopy(value);bad['speech']['audio_base64'] = base64.b64encode(broken).decode()
            with self.assertRaises(ValueError):evidence.voice_business(bad, identity)

    def voice_registry(self, builtin=False):
        fixture = SpeakerProfiles()
        # Share the same synthetic immutable model inventory across versions;
        # a separately created temporary model has different inode identities.
        for key in ('old', 'new', 'first', 'second'):
            setattr(fixture, key, copy.deepcopy(getattr(self, key)))
        _, _, registry, source = fixture.extended()
        base = selected()
        if builtin:
            native = self.selected()
            base['limits'] = copy.deepcopy(native['limits'])
            registry['identity']['native_tts'] = contract.tts_binding(native)
        registry['identity']['native_tts_base'] = contract.tts_binding(base, base=True)
        records = {n:copy.deepcopy(row['record']) for n,row in source.items()}
        for row in records.values():
            row['format'] = evidence.VOICE_FORMAT
            row['capabilities'].update(capabilities(builtin, registration=True))
            row['business']['voice'] = cpu_record()
            row['business']['voice']['limits'] = copy.deepcopy(base['limits'])
            if builtin:
                from tools.serve_recovery.test_tts_profiles import retained
                row['business']['tts'] = retained()[0]
        return registry, previous.bind_records(registry, records)

    def pending_for(self, registry):
        initial = policy.initial(registry, self.stamp(0), self.config)
        return policy.prepare(initial, 'light', 'sustained_memory_pressure',
            self.pending['pending']['request_id'], 7, self.stamp(1))

    def test_minimal_registration_context_is_bound_before_and_after_migration(self):
        before = contract.asr_binding(dict(package.NATIVE_ASR,
            directory='/asr', limits=dict(package.ASR_LIMITS)))
        after = contract.asr_binding(registration_asr(), voice_registration=True)
        self.assertEqual(migration.audio_change(before, after, after_voice=True),
                         (True, False))
        with self.assertRaises(RuntimeError):migration.audio_change(before, after)
        self.assertEqual(migration.audio_change(after, after,
            before_voice=True, after_voice=True), (False, False))
        with self.assertRaises(RuntimeError):
            migration.audio_change(after, after, after_voice=True)
        caps = capabilities(registration=True)
        caps['transcriptions'] = dict(package.NATIVE_ASR, configured=True)
        for name in ('vad', 'speaker'):
            caps['transcriptions'][name] = dict(package.ASR_AUXILIARY[name][0], configured=True)
        caps['transcriptions']['vad']['max_chunks'] = package.VAD_LIMITS['max_chunks']
        normalized = migration.semantic_capabilities(caps)
        self.assertEqual(normalized['voice_registration']['enabled'], dict(enabled=True))
        self.assertTrue(caps['voice_registration']['enabled'])
        inactive = capabilities()
        inactive['speech'].update(enabled=False, registered_custom_voices=False,
            builtin_enabled=False, **package.NATIVE_TTS)
        inactive['chat_audio_output']['enabled'] = False
        self.assertTrue(migration.extends(migration.semantic_capabilities(inactive),
                                         migration.semantic_capabilities(capabilities())))

    def test_base_capacity_change_keeps_consumed_budget_pending_and_original_identity(self):
        registry, rows = self.voice_registry()
        for n in contract.NAMES:
            evidence.validate(rows[n]['record'], registry, n, rows[n]['sha256'])
        pending = self.pending_for(registry)
        newer = copy.deepcopy(registry)
        newer['identity']['native_tts_base']['limits']['max_frames'] = 161
        records = {n:copy.deepcopy(row['record']) for n,row in rows.items()}
        for row in records.values():row['business']['voice']['limits']['max_frames'] = 161
        following = previous.bind_records(newer, records)
        for options in ({}, dict(automatic=True, allow_capacity_change=True)):
            with self.assertRaises(ValueError):
                migration.permit(registry, newer, rows, following, self.config, self.config, **options)
        grant = migration.permit(registry, newer, rows, following, self.config, self.config,
                                 allow_capacity_change=True)
        self.assertEqual(grant['format'], migration.VOICE_FORMAT)
        value = migration.candidate(pending, grant, self.stamp(999999))
        for key in ('history', 'pending', 'not_before_ns', 'selected', 'profiles',
                    'completed_generation', 'light_since_ns'):
            self.assertEqual(value[key], pending[key])
        self.assertEqual(value['clock']['logical_ns'], pending['clock']['logical_ns'])
        self.assertEqual(value['history_bindings'][pending['pending']['request_id']]
                         ['registry_sha256'], contract.digest(registry))
        migration.AuthorizedContract(grant).transition(pending, value)
        for mutate in (lambda d:d.update(automatic=True),
                       lambda d:d.update(to_native_tts_base=None),
                       lambda d:d['to_native_tts_base'].update(weight_sha256='0'*64),
                       lambda d:d.update(to_native_asr=None),
                       lambda d:d.update(to_native_tts_base=copy.deepcopy(d['from_native_tts_base']))):
            bad=copy.deepcopy(grant);mutate(bad)
            with self.assertRaises((ValueError, RuntimeError)):migration.validate(bad)

    def test_both_families_migrate_shared_limits_and_automatic_recovery_keeps_features(self):
        registry, rows = self.voice_registry(builtin=True)
        newer = copy.deepcopy(registry)
        newer['identity'].update(binary_sha256='e'*64, protocol_sha256='f'*64)
        records = {n:copy.deepcopy(row['record']) for n,row in rows.items()}
        following = previous.bind_records(newer, records)
        grant = migration.permit(registry, newer, rows, following,
                                 self.config, self.config, automatic=True)
        self.assertTrue(grant['automatic'])
        self.assertEqual(grant['format'], migration.FORMAT)
        for family in ('native_tts', 'native_tts_base'):
            newer['identity'][family]['limits']['max_frames'] = 161
        for row in records.values():
            for family in ('tts', 'voice'):row['business'][family]['limits']['max_frames'] = 161
        following = previous.bind_records(newer, records)
        grant = migration.permit(registry, newer, rows, following,
                                 self.config, self.config, allow_capacity_change=True)
        self.assertEqual(grant['format'], migration.VOICE_FORMAT)
        bad=copy.deepcopy(grant);bad['to_native_tts']['limits']['max_frames'] = 162
        with self.assertRaisesRegex(ValueError, 'share effective'):migration.validate(bad)
        bad=copy.deepcopy(grant);bad['to_native_tts']=None
        with self.assertRaisesRegex(ValueError, 'accepted TTS'):migration.validate(bad)

    def test_explicit_base_addition_cannot_remove_or_reuse_builtin_only_business(self):
        registry, rows = self.voice_registry()
        grant = migration.permit(self.old, registry, self.first, rows,
            self.config, self.config, allow_capability_extension=True)
        self.assertEqual(grant['format'], migration.VOICE_FORMAT)
        for options in ({}, dict(automatic=True, allow_capability_extension=True)):
            with self.assertRaises(ValueError):
                migration.permit(self.old, registry, self.first, rows,
                    self.config, self.config, **options)
        with self.assertRaises(ValueError):
            migration.permit(registry, self.old, rows, self.first,
                self.config, self.config, allow_capability_extension=True)
        bad=copy.deepcopy(rows)
        del bad['primary']['record']['business']['voice']
        with self.assertRaises(ValueError):
            migration.permit(self.old, registry, self.first, bad,
                self.config, self.config, allow_capability_extension=True)


if __name__ == '__main__':unittest.main()
