"""Negative controls for evidence that can otherwise falsely certify history."""
import base64
import copy
import hashlib
import unittest

from tools.serve_realtime import history_acceptance as h


def evidence():
    raw = b'actual processed waveform'
    text = h.base.EXPECTED['zh']
    turns = []
    for i, count in enumerate((70, 86, 70)):
        item = 'item_' + str(i)
        transcript = dict(type='conversation.item.input_audio_transcription.completed',
            item_id=item, transcript=text,
            usage=dict(type='tokens', input_tokens=count, output_tokens=12, total_tokens=count+12,
                input_token_details=dict(audio_tokens=55,text_tokens=count-55)),
            logprobs=[dict(token='中',logprob=-.5,bytes=[228,184,173])])
        turns.append(dict(item_id=item, terminal=transcript, delta=text,
            committed=dict(previous_item_id=None if i != 1 else 'item_0'),
            retrieved=dict(item=dict(id=item, object='realtime.item', type='message',
                role='user', status='completed', content=[dict(type='input_audio',
                    transcript=text, audio=base64.b64encode(raw).decode())]))))
    result = dict(status='PASS', connections=1, automatic_replay=False, turns=turns,
        checks=[dict(type=kind, item_id='item_'+str(i))
                for i in (0, 1) for kind in ('deleted', 'absent')])
    reference = dict(prior_transcript=text, base_input_tokens=70, history_tokens=16)
    wave = dict(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    return result, reference, wave


class Evidence(unittest.TestCase):
    def test_frozen_limits_and_no_prompt_hint_cannot_drift(self):
        plan=dict(schema='q4t-realtime-history-business-v1', limits=dict(h.LIMITS),
            settings={mode:h.settings(mode) for mode in ('manual','near_stream')},
            reference=dict(history_tokens=16,base_input_tokens=70,token_ids=list(range(16))),
            contract=dict(actual_waveform_bytes=True,actual_history_input_tokens=True,
                delete_before_third_item=True,uncommitted_hidden=True,foreign_session_hidden=True,
                actual_usage_settled=True,guardian_recovery=False,default_unchanged=True,master_complete=False))
        h.check_plan(plan)
        altered=copy.deepcopy(plan);altered['limits']['starts']+=1
        with self.assertRaises(Exception):h.check_plan(altered)
        altered=copy.deepcopy(plan)
        altered['settings']['manual']['audio']['input']['transcription']['prompt']='expected transcript'
        with self.assertRaises(Exception):h.check_plan(altered)

    def test_complete_actual_evidence_and_exact_reference(self):
        result, reference, wave = evidence()
        self.assertEqual(h.check_history(result, reference), 262)
        self.assertEqual(h.check_retrieved(result['turns'][0], wave), b'actual processed waveform')

    def test_history_usage_cannot_be_unchanged_or_only_claimed_deleted(self):
        for index, value in ((1, 70), (2, 86)):
            result, reference, _ = evidence()
            result['turns'][index]['terminal']['usage']['input_tokens'] = value
            with self.assertRaises(Exception): h.check_history(result, reference)
        result, reference, _ = evidence()
        result['checks'].pop()
        with self.assertRaises(Exception): h.check_history(result, reference)

    def test_prior_transcript_and_predecessor_must_be_actual(self):
        result, reference, _ = evidence()
        reference['prior_transcript'] += 'Invented reference'
        with self.assertRaises(Exception): h.check_history(result, reference)
        result, reference, _ = evidence()
        result['turns'][2]['committed']['previous_item_id'] = 'deleted_item'
        with self.assertRaises(Exception): h.check_history(result, reference)

    def test_original_or_wrong_waveform_does_not_pass_retrieval(self):
        result, _, wave = evidence()
        for raw in (b'original unprocessed input', b'actual processed waveforM', b''):
            turn = copy.deepcopy(result['turns'][0])
            turn['retrieved']['item']['content'][0]['audio'] = base64.b64encode(raw).decode()
            with self.assertRaises(Exception): h.check_retrieved(turn, wave)

    def test_foreign_identity_and_fabricated_transcript_do_not_pass(self):
        result, _, wave = evidence()
        turn = copy.deepcopy(result['turns'][0])
        turn['retrieved']['item']['id'] = 'foreign'
        with self.assertRaises(Exception): h.check_retrieved(turn, wave)
        turn = copy.deepcopy(result['turns'][0])
        turn['retrieved']['item']['content'][0]['transcript'] = 'fabricated'
        with self.assertRaises(Exception): h.check_retrieved(turn, wave)

    def test_noncanonical_base64_and_malformed_usage_reject(self):
        result, _, wave = evidence()
        for value in ('AB==', '???', result['turns'][0]['retrieved']['item']['content'][0]['audio']+'\n'):
            turn = copy.deepcopy(result['turns'][0]);turn['retrieved']['item']['content'][0]['audio'] = value
            with self.assertRaises(Exception): h.check_retrieved(turn, wave)
        turn = copy.deepcopy(result['turns'][0]);turn['terminal']['usage']['total_tokens'] -= 1
        with self.assertRaises(Exception): h.check_retrieved(turn, wave)

    def test_live_settlement_must_include_all_work_and_release_reservation(self):
        metrics = {n+'{client="alpha"}':v for n,v in (
            ('q4t_client_rate_admitted_total',3),
            ('q4t_client_rate_settled_tokens_total',262),
            ('q4t_client_rate_reserved_tokens',0))}
        h.settlement(metrics, 'alpha', 262, 3)
        for name, value in [('q4t_client_rate_admitted_total',2),
                            ('q4t_client_rate_settled_tokens_total',250),
                            ('q4t_client_rate_reserved_tokens',1)]:
            changed = dict(metrics);changed[name+'{client="alpha"}'] = value
            with self.assertRaises(Exception): h.settlement(changed, 'alpha', 262, 3)


if __name__ == '__main__': unittest.main()
