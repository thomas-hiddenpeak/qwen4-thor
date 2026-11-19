"""Reject provenance drift and skipped main-model consumers; zero GPU work."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools.serve_recovery import ledger, profile_evidence, profile_record
from tools.serve_recovery import profile_refusal_reuse as reuse
from tools.serve_recovery import profile_refusal_targeted as stage
from tools.serve_recovery import test_profile_prompt_reuse as previous
from tools.serve_service import package


class RefusalSource(unittest.TestCase):
    stamp = previous.SourceReuse.stamp

    def setUp(self):
        previous.SourceReuse.setUp(self)
        review = dict(reuse.REVIEW,
            previous_binary=self.original['identity']['binary_sha256'],
            candidate_binary='e'*64, candidate_protocol='f'*64,
            previous_registry=ledger.digest(self.source_registry))
        patch = mock.patch.object(reuse, 'REVIEW', review)
        patch.start(); self.addCleanup(patch.stop)

    def attached(self):
        return reuse.attach(self.value, self.source_registry, self.original,
                            previous.legacy(), None)

    def test_original_observations_remain_distinct_and_unmodified(self):
        original = copy.deepcopy(self.original)
        value = self.attached()
        self.assertEqual(self.original, original)
        self.assertEqual(value['reused_business']['record'], original)
        self.assertEqual(value['reused_business']['keys'], ['embedding'])
        self.assertEqual(value['reused_business']['partial_keys'], [])
        self.assertIsNot(value['business']['prompt'], self.value['business']['prompt'])
        reuse.validate(value)

    def test_unknown_source_engine_capacity_and_capability_are_rejected(self):
        value = self.attached()
        for mutate in [lambda v: v['identity'].update(binary_sha256='1'*64),
                       lambda v: v['identity'].update(protocol_sha256='1'*64),
                       lambda v: v['identity'].update(native_embedding={}),
                       lambda v: v['capacity'].update(max_tokens=1),
                       lambda v: v['capabilities']['chat'].update(invented=True),
                       lambda v: v['reused_business']['review'].update(source_review='0'*64)]:
            changed = copy.deepcopy(value); mutate(changed)
            with self.assertRaises(ValueError): reuse.validate(changed)

    def test_skipped_consumer_recursive_source_and_rewritten_backend_fail(self):
        value = self.attached()
        for mutate in [lambda v: v['business'].pop('prompt'),
                       lambda v: v['business'].pop('conversation'),
                       lambda v: v['business'].update(embedding={}),
                       lambda v: v['reused_business']['keys'].append('media'),
                       lambda v: v['reused_business'].update(partial_keys=['voice']),
                       lambda v: v['reused_business']['record'].update(reused_business={}),
                       lambda v: v['reused_business'].update(voice_prompt={})]:
            changed = copy.deepcopy(value); mutate(changed)
            with self.assertRaises(ValueError): reuse.validate(changed)


class ConsumerBoundary(unittest.TestCase):
    def fixture(self):
        # Isolate the new source/owner/ID projection. Audio and native reference
        # validators are existing separately tested contracts, spied on below.
        data = {k: [1, 2] for k in reuse.CONDITION_FIELDS}
        records = dict(consent=dict(owner=reuse.VOICE_OWNER),
                       voice=dict(owner=reuse.VOICE_OWNER, data=data))
        old = dict(conditioning=copy.deepcopy(records), speech='original-speech')
        request = profile_evidence.voice_requests(reuse.VOICE_CONSENT, reuse.VOICE_ID)['chat']
        chat = dict(id='actual-chat', choices=['actual-answer'],
                    usage=dict(q4t_audio_synthesis=dict(input_tokens=918)))
        witness = dict(snapshot_sha256=reuse.VOICE_SNAPSHOT, records=records,
            request=request, chat=chat, stored_chat=copy.deepcopy(chat),
            chat_transcript=dict(text='actual-new-asr'), health=dict(state='ready', model='base',
                core_readiness_dependency=False, reason='none', compute_failures=0,
                asset_cleanup_pending=False))
        return witness, old, dict(native_tts_base=dict(model='base'))

    def test_saved_voice_checks_fresh_output_against_real_original_condition(self):
        witness, old, identity = self.fixture(); snapshot = copy.deepcopy(old)
        with mock.patch.object(profile_evidence, 'voice_reference') as reference, \
             mock.patch.object(profile_evidence, 'voice_synthesis') as synthesis:
            reuse.voice_business(witness, old, identity)
            reference.assert_called_once_with(witness['records'], reuse.VOICE_CONSENT, reuse.VOICE_ID)
            self.assertEqual(synthesis.call_args.args[0]['chat'], witness['chat'])
            self.assertEqual(synthesis.call_args.args[0]['chat_transcript'], witness['chat_transcript'])
            self.assertEqual(synthesis.call_args.args[0]['speech'], old['speech'])
        self.assertEqual(old, snapshot)

    def test_saved_voice_cannot_hide_owner_reference_request_storage_or_usage_drift(self):
        value, old, identity = self.fixture()
        for mutate in [lambda v: v.update(snapshot_sha256='0'*64),
                       lambda v: v['records']['voice'].update(owner='0'*64),
                       lambda v: v['records']['voice']['data'].update(reference=[3]),
                       lambda v: v['request']['audio'].update(voice=dict(id='foreign')),
                       lambda v: v['stored_chat'].update(id='other'),
                       lambda v: v['chat']['usage']['q4t_audio_synthesis'].update(input_tokens=0),
                       lambda v: v['health'].update(compute_failures=1)]:
            changed = copy.deepcopy(value); mutate(changed)
            with mock.patch.object(profile_evidence, 'voice_reference'), \
                 mock.patch.object(profile_evidence, 'voice_synthesis'), self.assertRaises(ValueError):
                reuse.voice_business(changed, old, identity)

    def test_actual_work_must_match_frozen_bounded_scope(self):
        value = dict(status=stage.SUCCESS, binary_sha256=reuse.REVIEW['candidate_binary'],
            seconds=100, counts={k: v for k, v in stage.LIMITS.items() if k != 'seconds'})
        value['counts']['controls'] = 50; stage.check_result(value)
        for key in value['counts']:
            changed = copy.deepcopy(value); changed['counts'][key] = stage.LIMITS[key] + 1
            with self.assertRaises(RuntimeError): stage.check_result(changed)
            changed['counts'][key] = False
            with self.assertRaises(RuntimeError): stage.check_result(changed)
        for changes in (dict(seconds=900), dict(status='FAILED'), dict(binary_sha256='0'*64)):
            with self.assertRaises(RuntimeError): stage.check_result(dict(value, **changes))

    def test_unreviewed_plan_is_rejected_before_lease_or_model_initialization(self):
        with mock.patch.object(stage, 'check_plan', side_effect=ValueError('unknown review')), \
             mock.patch.object(stage.encrypted.Stage, '__init__') as initialize, \
             mock.patch.object(stage.subprocess, 'Popen') as spawn:
            with self.assertRaisesRegex(ValueError, 'unknown review'): stage.Stage({})
            initialize.assert_not_called(); spawn.assert_not_called()

    def test_new_large_provenance_stays_separate_from_general_metadata(self):
        parent = Path(__file__).resolve().parents[2] / 'build/refusal-profile-tests'
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as directory:
            path = Path(directory) / 'record.json'
            value = dict(format='q4t-profile-business-v18',
                reused_business=dict(format=reuse.FORMAT, record='x' * (3 << 20)))
            profile_record.save(path, value); self.assertEqual(profile_record.read(path), value)
            with self.assertRaisesRegex(RuntimeError, 'metadata size limit'): package.read(path)
            value['reused_business']['format'] = 'unreviewed-source'
            with self.assertRaises(ValueError): profile_record.save(path, value)


if __name__ == '__main__': unittest.main()
