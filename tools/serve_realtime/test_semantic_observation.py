"""CPU counterexamples for cold fallback vs a real completed classifier."""
import copy
import unittest

from tools.serve_realtime import semantic_observation as observed
from tools.serve_recovery import profile_evidence
from tools.serve_recovery.test_profile_semantic import record


def fixture(fallback=False, continuation=False):
    value = record()['turns'][int(continuation)]
    value.update(status=observed.STATUS, observation_policy=observed.POLICY,
                 transport='openai-realtime-wss')
    value['terminal']['type'] = 'conversation.item.input_audio_transcription.completed'
    value['retrieved']['type'] = 'conversation.item.retrieved'
    if fallback:
        value['terminal']['q4t_semantic_vad'].update(
            accepted=0, stale=1, maximum_wait=True, probability_available=False)
    value['events'][-1] = copy.deepcopy(value['terminal'])
    value['events'].insert(1, dict(type='conversation.item.input_audio_transcription.delta',
                                  item_id='item', delta=value['delta']))
    value['events'].append(copy.deepcopy(value['retrieved']))
    return value


class Observation(unittest.TestCase):
    def test_scored_and_cold_fallback_remain_distinct_without_mutation(self):
        for fallback in (False, True):
            for continuation in (False, True):
                value = fixture(fallback, continuation); before = copy.deepcopy(value)
                result = observed.observe(value, continuation)
                self.assertEqual(result['kind'],
                    'unknown_maximum_wait' if fallback else 'scored_early_end')
                self.assertEqual(result['total_tokens'], 195)
                self.assertFalse(result['profile_qualified'])
                self.assertEqual(value, before)

    def test_observation_cannot_pass_existing_strict_qualification(self):
        for fallback in (False, True):
            value = fixture(fallback)
            with self.assertRaises(ValueError): profile_evidence.semantic_turn(value, False)
        value = fixture(True); value['status'] = 'PASS'
        with self.assertRaises(ValueError): profile_evidence.semantic_turn(value, False)

    def test_unknown_cannot_finish_early_or_invent_a_probability(self):
        for update in (dict(maximum_wait=False), dict(probability_available=True),
                       dict(stale=0), dict(accepted=True), dict(attempts=9)):
            value = fixture(True); value['terminal']['q4t_semantic_vad'].update(update)
            with self.assertRaises(ValueError): observed.observe(value, False)

    def test_classifier_usage_must_be_finite_bounded_and_accounted(self):
        for update in (dict(input_tokens=0), dict(input_tokens=180),
                       dict(output_tokens=15), dict(output_tokens=129),
                       dict(included_in_usage=False)):
            value = fixture(True); value['terminal']['q4t_semantic_vad'].update(update)
            with self.assertRaises(ValueError): observed.observe(value, False)
        value = fixture(True); value['terminal']['usage']['total_tokens'] += 1
        with self.assertRaises(ValueError): observed.observe(value, False)

    def test_protocol_errors_missing_terminal_and_rewritten_stream_reject(self):
        mutations = (
            lambda v: v['events'].append(dict(type='error')),
            lambda v: v['events'].pop(-2),
            lambda v: v['events'].append(copy.deepcopy(v['terminal'])),
            lambda v: v['events'][1].update(delta='invented'),
            lambda v: v['events'][0].update(item_id='foreign'),
            lambda v: v['events'].reverse(),
            lambda v: v.update(close_code=None),
            lambda v: v.update(automatic_replay=True),
            lambda v: v.update(observation_policy='unknown'),
        )
        for change in mutations:
            value = fixture(True); change(value)
            with self.assertRaises(ValueError): observed.observe(value, False)

    def test_retained_audio_continuation_and_logprobs_cannot_disappear(self):
        for change in (
            lambda v: v['continuation'].update(no_commit=False),
            lambda v: v['retrieved']['item']['content'][0].update(audio='AA=='),
            lambda v: v['retrieved']['item'].update(id='foreign'),
            lambda v: v['terminal']['logprobs'][0].update(logprob=float('nan')),
        ):
            value = fixture(True, True); change(value)
            with self.assertRaises(ValueError): observed.observe(value, True)


if __name__ == '__main__':
    unittest.main()
