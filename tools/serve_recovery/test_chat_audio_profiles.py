"""Actual retained Chat answers with synthetic CPU registries, never GPU claims."""
import base64
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

from tools.serve_recovery import profile_contract as contract
from tools.serve_recovery import profile_evidence as evidence
from tools.serve_recovery import profile_acceptance as driver
from tools.serve_recovery import profile_migration as migration, profile_policy as policy
from tools.serve_recovery import test_speaker_profiles as old
from tools.serve_recovery.test_profile_migration import bind_records
from tools.native_asr import service_acceptance as managed
from tools.native_asr.chat_acceptance import requests

ROOT = Path(__file__).resolve().parents[2]
OBSERVED = ROOT/'docs/evidence/serve-chat-audio-http-20261118'


class ChatAudioProfiles(unittest.TestCase):
    setUp = old.SpeakerProfiles.setUp
    stamp = old.SpeakerProfiles.stamp
    selected = old.SpeakerProfiles.selected
    selected_auxiliary = old.SpeakerProfiles.selected_auxiliary
    selected_speaker = old.SpeakerProfiles.selected_speaker
    audio = old.SpeakerProfiles.audio
    auxiliary_audio = old.SpeakerProfiles.auxiliary_audio
    asr_extended = old.SpeakerProfiles.asr_extended
    auxiliary_extended = old.SpeakerProfiles.auxiliary_extended
    pending_for = old.SpeakerProfiles.pending_for
    witness = old.SpeakerProfiles.witness
    speaker_extended = old.SpeakerProfiles.extended

    def source(self):
        return base64.b64encode((ROOT/'tests/fixtures/asr/asr_zh.wav').read_bytes()).decode()

    def chat_witness(self):
        return evidence.chat_audio_witness(self.source(),
            json.loads((OBSERVED/'python-zh.wire').read_text()),
            json.loads((OBSERVED/'final-config.wire').read_text()))

    def extended(self):
        _, _, prior, original = self.speaker_extended()
        following = copy.deepcopy(prior)
        following['identity'].update(binary_sha256='9'*64, protocol_sha256='8'*64)
        rows = {n:copy.deepcopy(v['record']) for n,v in original.items()}
        for row in rows.values():
            row['format'] = evidence.CHAT_AUDIO_FORMAT
            row['capabilities']['transcriptions']['chat_input'] = copy.deepcopy(evidence.CHAT_AUDIO_CAPABILITY)
            row['business']['chat_audio'] = self.chat_witness()
        return prior, original, following, bind_records(following, rows)

    def test_real_chat_source_answer_usage_and_health_rejects_fabricated_success(self):
        fixed = dict(native_asr=contract.asr_binding(self.selected()))
        value = self.chat_witness()
        evidence.chat_audio_business(value, fixed, self.source())
        original = requests()['zh']
        original['messages'][0]['content'][1]['input_audio']['data'] = self.source()
        self.assertEqual(evidence.chat_audio_request(self.source()), original)
        faults = [lambda v:v.update(source_sha256='0'*64),
            lambda v:v.update(request_sha256='0'*64),
            lambda v:v['response']['choices'].pop(),
            lambda v:v['response']['choices'][1]['message'].update(content='天气'),
            lambda v:v['response']['choices'][0]['message'].update(refusal='no'),
            lambda v:v['response']['choices'][0].update(finish_reason='length'),
            lambda v:v['response']['usage'].update(total_tokens=1),
            lambda v:v['response']['usage']['q4t_audio_preparation'].update(audio_tokens=True),
            lambda v:v['response']['usage']['q4t_audio_preparation'].update(input_tokens=999999),
            lambda v:v['response']['usage']['prompt_tokens_details'].update(cached_tokens=999999),
            lambda v:v['health'].update(core_readiness_dependency=True),
            lambda v:v['health'].update(device_bytes=0),
            lambda v:v['limits'].update(max_output_tokens=129)]
        for mutate in faults:
            bad = copy.deepcopy(value);mutate(bad)
            with self.assertRaises(ValueError): evidence.chat_audio_business(bad, fixed, self.source())
        with self.assertRaises(ValueError): evidence.chat_audio_request('YR==')

    def test_new_receipts_require_chat_on_both_profiles_and_preserve_old_format(self):
        prior, original, registry, records = self.extended()
        for name in ('primary', 'light'):
            old_row = original[name]['record']
            evidence.validate(old_row, prior, name, original[name]['sha256'])
            self.assertEqual(evidence.format_for(old_row['capacity'], old_row['identity'],
                old_row['capabilities']), evidence.SPEAKER_FORMAT)
            row = records[name]['record']
            evidence.validate(row, registry, name, records[name]['sha256'])
            self.assertEqual(evidence.format_for(row['capacity'], row['identity'],
                row['capabilities']), evidence.CHAT_AUDIO_FORMAT)
            for key in ('chat_audio', 'speaker', 'asr_auxiliary', 'asr'):
                bad = copy.deepcopy(row);del bad['business'][key]
                with self.assertRaises(ValueError):
                    evidence.business(bad['business'], bad['identity'], bad['capacity'], bad['capabilities'])
            bad = copy.deepcopy(row);bad['format'] = evidence.SPEAKER_FORMAT
            with self.assertRaises(ValueError): evidence.validate(bad, registry, name, records[name]['sha256'])

    def test_malformed_declarations_and_unbound_backend_cannot_be_qualified(self):
        _, _, registry, records = self.extended()
        row = records['primary']['record']
        for key, value in [('preparation','fabricated'), ('max_parts',0),
                           ('output_modalities',['audio']), ('formats',['wav']),
                           ('usage','reservation')]:
            bad = copy.deepcopy(row['capabilities'])
            bad['transcriptions']['chat_input'][key] = value
            with self.assertRaises(ValueError):
                contract.qualification('primary', row['capacity'], row['identity'], bad,
                    row['configuration'], row['startup_memory'], 'a'*64)
            with self.assertRaises(ValueError): migration.semantic_capabilities(bad)
        no_asr = copy.deepcopy(row['identity']);del no_asr['native_asr']
        with self.assertRaises(ValueError):
            contract.qualification('primary', row['capacity'], no_asr, row['capabilities'],
                row['configuration'], row['startup_memory'], 'a'*64)

    def test_explicit_extension_preserves_recovery_state_and_auto_fallback_cannot_drop_chat(self):
        prior, original, registry, records = self.extended()
        for options in ({}, dict(automatic=True, allow_capability_extension=True)):
            with self.assertRaises(ValueError):
                migration.permit(prior, registry, original, records, self.config, self.config, **options)
        grant = migration.permit(prior, registry, original, records, self.config, self.config,
                                   allow_capability_extension=True)
        pending = self.pending_for(prior)
        following = migration.candidate(pending, grant, self.stamp(999999))
        for key in ('history','pending','selected','not_before_ns','light_since_ns'):
            self.assertEqual(following[key], pending[key])
        migration.AuthorizedContract(grant).transition(pending, following)
        for options in ({}, dict(automatic=True), dict(allow_capability_extension=True)):
            with self.assertRaises(ValueError):
                migration.permit(registry, prior, records, original, self.config, self.config, **options)

    def test_profile_driver_freezes_actual_extra_requests_without_omitting_auxiliaries(self):
        _, _, registry, _ = self.extended()
        selected = registry['profiles']['primary'];fixed = registry['identity']['native_asr']
        before = driver.limits_for(selected, fixed)
        after = driver.limits_for(selected, fixed, True)
        self.assertEqual(after, dict(before, chats=before['chats']+2))
        with self.assertRaises(RuntimeError): driver.limits_for(selected, None, True)
        args = ['profile_acceptance.py','prepare','--binary','/engine','--package','/package',
                '--output','/output','--chat-audio']
        with mock.patch.object(sys,'argv',args), mock.patch.object(driver,'prepare') as prepare:
            driver.main();self.assertTrue(prepare.call_args.args[0].chat_audio)

    def test_managed_budgets_and_stream_requests_stay_explicit_and_bounded(self):
        for scenario, extra in [('tool_retained',1),('tool_version',2),('tls',2)]:
            limits = managed.limits_for(scenario, True)
            self.assertEqual(limits['chats'], managed.LIMITS[scenario]['chats']+extra)
            plan = dict(scenario=scenario, chat_audio=True, limits=limits)
            with mock.patch.dict(managed.GLOBALS):
                managed.configure_plan_limits(plan)
                with self.assertRaises(RuntimeError):
                    managed.configure_plan_limits(dict(plan, limits=dict(limits, chats=limits['chats']+1)))
        with self.assertRaises(RuntimeError): managed.limits_for('tls_remaining', True)
        original = evidence.chat_audio_request(self.source())
        stream = evidence.chat_audio_request(self.source(), stream=True)
        self.assertEqual(stream, dict(original, stream=True, stream_options=dict(include_usage=True)))
        value = self.chat_witness()
        with self.assertRaises(ValueError):
            evidence.chat_audio_business(value, dict(native_asr=contract.asr_binding(self.selected())),
                                        self.source(), stream=True)


if __name__ == '__main__': unittest.main()
