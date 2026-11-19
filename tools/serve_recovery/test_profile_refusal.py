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
from tools.serve_recovery.test_profile_conversation import record as conversation_record
from tools.serve_recovery.test_profile_semantic import record as semantic_record


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
    def test_only_actual_consumers_fit_the_qualified_fair_queue(self):
        actor = object.__new__(stage.Stage)
        selected = dict(max_tokens=256, max_queued_requests=3, max_inflight_requests=8)
        actor.plan = dict(base=dict(settings=dict(native_tts_base=dict(limits=dict(max_frames=200)))),
                          profiles=dict(primary=selected, light=selected))
        policy = actor.client_policy()
        self.assertEqual([r['id'] for r in policy['clients']], [stage.encrypted.CLIENT, 'audio'])
        for profile in actor.plan['profiles'].values(): stage.full.capacity.check_clients(profile, policy)
        self.assertTrue(policy['fair_queue'])
        invalid = copy.deepcopy(policy)
        invalid['clients'] += [dict(policy['clients'][0], id=n) for n in ('unused-one', 'unused-two')]
        with self.assertRaisesRegex(RuntimeError, 'client fairness exceeds global capacity'):
            stage.full.capacity.check_clients(selected, invalid)

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
            for source_format in (reuse.FORMAT, reuse.ASSEMBLED_FORMAT):
                value = dict(format='q4t-profile-business-v18',
                    reused_business=dict(format=source_format, record='x' * (3 << 20)))
                profile_record.save(path, value)
                self.assertEqual(profile_record.read(path), value)
                with self.assertRaisesRegex(RuntimeError, 'metadata size limit'): package.read(path)
            value['reused_business']['format'] = 'unreviewed-source'
            with self.assertRaises(ValueError): profile_record.save(path, value)


class CompletedObservation(unittest.TestCase):
    def conversation(self):
        value = conversation_record()
        value['format'] = 'q4t-completed-conversation-observation-v1'
        value['session'].update(status='PROTOCOL_PASS', mode='complete', phase='result',
            partial=None, quality_policy='record_exact_source_answer_separately',
            model_quality=dict(status='PASS', expected=value['source']['version'],
                actual=value['source']['version'], criterion='exact_source_version',
                response_id=value['session']['responses'][1]['id']))
        return value

    def semantic(self):
        value = semantic_record()
        value['format'] = 'q4t-completed-semantic-observation-v1'
        for row in value['turns']:
            row.update(status='OBSERVED_NOT_QUALIFIED',
                observation_policy='record_bounded_semantic_fallback_v1',
                transport='openai-realtime-wss')
        return value

    def test_exact_output_review_never_rewrites_observation_status(self):
        value = self.conversation(); original = copy.deepcopy(value)
        self.assertEqual(profile_evidence.conversation_business(value), 220)
        self.assertEqual(value, original)
        value = self.semantic(); original = copy.deepcopy(value)
        profile_evidence.semantic_business(value, dict(native_asr=dict(
            model='qwen3-asr-0.6b', limits=dict(max_output_tokens=128),
            vad=dict(model='silero-vad-16k', limits=dict(max_seconds=600)))))
        self.assertEqual(value, original)

    def test_wrong_answer_cannot_be_qualified_by_a_fabricated_quality_pass(self):
        value = self.conversation()
        session = value['session']; answer = session['responses'][1]
        answer['output'][0]['content'][0]['text'] = '<version>rev346</version>'
        session['events'][-2]['delta'] = '<version>rev346</version>'
        session['events'][-1]['response'] = copy.deepcopy(answer)
        with self.assertRaises(ValueError): profile_evidence.conversation_business(value)
        for change in (dict(close_code=None), dict(status='FAIL'), dict(partial={}),
                       dict(quality_policy='unknown'), dict(model_quality=dict(status='FAIL'))):
            value = self.conversation(); value['session'].update(change)
            with self.assertRaises(ValueError): profile_evidence.conversation_business(value)

    def test_cold_timeout_and_missing_lifecycle_never_meet_successful_score_gate(self):
        for change in (dict(accepted=0, stale=1, maximum_wait=True, probability_available=False),
                       dict(maximum_wait=True), dict(probability_available=False),
                       dict(included_in_usage=False), dict(input_tokens=0)):
            value = self.semantic()['turns'][0]
            value['terminal']['q4t_semantic_vad'].update(change)
            with self.assertRaises(ValueError): profile_evidence.semantic_observed_success(value, False)
        for change in (dict(close_code=None), dict(connections=True),
                       dict(status='FAIL'), dict(observation_policy='unknown')):
            value = self.semantic()['turns'][0]; value.update(change)
            with self.assertRaises(ValueError): profile_evidence.semantic_observed_success(value, False)

    def assembly(self):
        body = {key: dict(actual=key) for key in reuse.FRESH | {'tts'}}
        value = dict(profile='primary', identity=dict(binary_sha256='1'*64),
            capacity=dict(max_tokens=256), capabilities=dict(implemented=True),
            configuration=dict(mtp=True), startup_memory=dict(status='PASS'), business=body,
            environment=dict(status='PASS', env_abort=False, all_queries_ok=True,
                cleanup_complete=True, nvml_queries=323, lease_result_sha256=reuse.ASSEMBLY_FILE),
            reused_business=dict(voice_prompt=dict(actual='voice'),
                                 legacy_responses_prompt=dict(actual='legacy')))
        selected = dict(fresh={k: ledger.digest(v) for k,v in body.items()},
            voice=ledger.digest(value['reused_business']['voice_prompt']),
            legacy=ledger.digest(value['reused_business']['legacy_responses_prompt']),
            **{k:ledger.digest(value[k]) for k in
               ('capabilities', 'configuration', 'startup_memory')})
        proof = dict(format='q4t-completed-consumer-assembly-v1', identity=value['identity'],
            profiles=dict(primary=value['capacity']), profiles_receipts=dict(primary=selected),
            failed_observations=dict(exact_format=dict(status='FAIL', answer='<version>rev346</version>')),
            stages=dict(original=dict(status='FAILED'), consumers=dict(status='OBSERVATIONS_COMPLETE_NOT_QUALIFICATION')))
        value['reused_business']['assembly'] = copy.deepcopy(proof)
        patch = mock.patch.object(reuse, 'ASSEMBLY_DIGEST', ledger.digest(proof))
        patch.start(); self.addCleanup(patch.stop)
        return value

    def test_review_binds_successful_fragments_and_keeps_original_failures(self):
        value = self.assembly(); original = copy.deepcopy(value)
        reuse.validate_assembly(value); self.assertEqual(value, original)
        for change in (
            lambda v: v['business']['prompt'].update(actual='replayed'),
            lambda v: v['business'].pop('media'),
            lambda v: v['reused_business']['voice_prompt'].update(actual='foreign'),
            lambda v: v['capabilities'].update(implemented=False),
            lambda v: v['configuration'].update(mtp=False),
            lambda v: v['startup_memory'].update(status='FAILED'),
            lambda v: v['environment'].update(nvml_queries=1),
        ):
            wrong = copy.deepcopy(value); change(wrong)
            with self.assertRaises((ValueError, KeyError)): reuse.validate_assembly(wrong)

    def test_unknown_identity_or_discarded_failed_source_cannot_be_reviewed(self):
        value = self.assembly()
        for change in (
            lambda v: v['identity'].update(binary_sha256='0'*64),
            lambda v: v['capacity'].update(max_tokens=128),
            lambda v: v['reused_business']['assembly'].pop('failed_observations'),
            lambda v: v['reused_business']['assembly']['stages']['original'].update(status='PASS'),
        ):
            wrong = copy.deepcopy(value); change(wrong)
            with self.assertRaises(ValueError): reuse.validate_assembly(wrong)


if __name__ == '__main__': unittest.main()
