"""CPU counterexamples for the real duplex evidence checker; no fake GPU pass."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from tools.serve_realtime import duplex_checks as c
from tools.serve_realtime.duplex_acceptance import accounting, capture_actor, check_plan, CONTRACT, LIMITS, ROOT


def response_fixture():
    value = dict(mode='vad', events=[], responses=[])
    for i,label in enumerate(c.LABELS['vad']):
        response = dict(id='r'+str(i), status='cancelled' if i == 2 else 'completed',
            output_modalities=['text'], usage=dict(input_tokens=3,output_tokens=2,total_tokens=5),
            output=[dict(type='message',content=[dict(type='output_text',text='answer')])])
        value['responses'].append(dict(label=label,response=response))
        value['events'] += [dict(type='response.created',response=dict(id=response['id'])),
            dict(type='response.output_text.delta',response_id=response['id'],delta='answer'),
            dict(type='response.done',response=copy.deepcopy(response))]
    return value


def idle_fixture():
    return dict(responses=[{}, {}, dict(response=dict(id='cancelled'))], events=[
        dict(type='input_audio_buffer.speech_started',item_id='speech'),
        dict(type='input_audio_buffer.committed',item_id='speech'),
        dict(type='input_audio_buffer.timeout_triggered',item_id='empty',audio_start_ms=4000,audio_end_ms=4000),
        dict(type='input_audio_buffer.committed',item_id='empty'),
        dict(type='response.created',response=dict(id='cancelled')),
        dict(type='response.output_text.delta',response_id='cancelled',delta='one'),
        dict(type='input_audio_buffer.speech_started',item_id='interruption'),
        dict(type='input_audio_buffer.committed',item_id='interruption')])


class Checks(unittest.TestCase):
    def test_response_coverage_missing_or_duplicate_terminal(self):
        value = response_fixture()
        self.assertEqual(c.response_rows(value)[1], 15)
        for duplicate in (False,True):
            bad = copy.deepcopy(value)
            if duplicate: bad['events'].append(copy.deepcopy(bad['events'][-1]))
            else: bad['events'].pop()
            with self.assertRaises(RuntimeError): c.response_rows(bad)

    def test_response_identity_stream_and_usage_counterexamples(self):
        original = response_fixture()
        for field in ('identity','stream','usage'):
            value = copy.deepcopy(original)
            if field == 'identity': value['events'][0]['response']['id'] = 'foreign'
            elif field == 'stream': value['events'][1]['delta'] = 'changed'
            else: value['responses'][0]['response']['usage']['total_tokens'] = 9
            with self.assertRaises(RuntimeError): c.response_rows(value)

    def test_asr_index_order_and_duplicate_are_not_success(self):
        events = [dict(type='conversation.item.input_audio_transcription.completed',
            item_id='mixed',content_index=i,transcript='真实。',
            usage=dict(input_tokens=3,output_tokens=2,total_tokens=5)) for i in (1,3)]
        expected = [('mixed',1),('mixed',3)]
        self.assertEqual(c.input_rows(events,expected,'真实。')[0],10)
        for bad in ([*reversed(events)], events+[events[0]], [dict(events[0],content_index=0),events[1]]):
            with self.assertRaises(RuntimeError): c.input_rows(bad,expected,'真实。')

    def test_asr_failure_and_quality_remain_distinct(self):
        event = dict(type='conversation.item.input_audio_transcription.completed',
            item_id='audio',content_index=0,transcript='原始错词',
            usage=dict(input_tokens=3,output_tokens=2,total_tokens=5))
        _,quality = c.input_rows([event],[('audio',0)],'正确词')
        self.assertEqual(quality[0]['status'],'FAIL')
        self.assertEqual(quality[0]['actual'],'原始错词')
        failed = dict(type='conversation.item.input_audio_transcription.failed')
        with self.assertRaises(RuntimeError): c.input_rows([event,failed],[('audio',0)],'正确词')

    def test_temporary_item_cannot_remain_retrievable(self):
        value = dict(events=[dict(type='error',error=dict(event_id='missing_'+name,
            code='realtime_item_not_found')) for name in ('temporary_audio','temporary_roundtrip')])
        c.temporary_cleanup(value)
        value['events'].append(dict(type='conversation.item.retrieved',item=dict(id='temporary_audio')))
        with self.assertRaises(RuntimeError): c.temporary_cleanup(value)

    def test_temporary_ack_must_be_exact_and_all_errors_accounted(self):
        original = dict(events=[dict(type='error',error=dict(event_id='missing_'+name,
            code='realtime_item_not_found')) for name in ('temporary_audio','temporary_roundtrip')])
        for bad in ('code','identity','extra'):
            value = copy.deepcopy(original)
            if bad == 'code': value['events'][0]['error']['code'] = 'not_actually_checked'
            elif bad == 'identity': value['events'][0]['error']['event_id'] = 'foreign'
            else: value['events'].append(dict(type='error',error=dict(code='unexpected')))
            with self.assertRaises(RuntimeError): c.temporary_cleanup(value)

    def test_zero_input_idle_cannot_invent_asr_or_received_samples(self):
        original = idle_fixture()
        self.assertEqual(c.idle_boundary(original),[('speech',0),('interruption',0)])
        for fake in ('asr','duration'):
            value = copy.deepcopy(original)
            if fake == 'asr': value['events'].append(dict(type='conversation.item.input_audio_transcription.completed',item_id='empty'))
            else: value['events'][2]['audio_end_ms'] = 4100
            with self.assertRaises(RuntimeError): c.idle_boundary(value)

    def test_interruption_must_follow_real_model_delta(self):
        value = idle_fixture()
        value['events'] = [e for e in value['events'] if e['type'] != 'response.output_text.delta']
        with self.assertRaises(RuntimeError): c.idle_boundary(value)

    def test_public_private_usage_and_admission_are_honest(self):
        observed = dict(visible_tokens=20,private_asr=0,admitted=5)
        metrics = {'q4t_client_rate_settled_tokens_total{client="alpha"}':20,
            'q4t_client_rate_admitted_total{client="alpha"}':5,
            'q4t_client_rate_reserved_tokens{client="alpha"}':0}
        self.assertEqual(accounting(metrics,'alpha',observed)['actual_settled_tokens'],20)
        for key in metrics:
            bad = dict(metrics); bad[key] += 1
            with self.assertRaises(RuntimeError): accounting(bad,'alpha',observed)
        observed['private_asr'] = 1
        with self.assertRaises(RuntimeError): accounting(metrics,'alpha',observed)
        metrics['q4t_client_rate_settled_tokens_total{client="alpha"}'] = 27
        actual = accounting(metrics,'alpha',observed)
        self.assertEqual(actual['private_asr_counter_residual'],7)
        self.assertIs(actual['private_asr_exact_token_oracle'],False)

    def test_actor_timeout_retains_written_stdout_and_stderr(self):
        root = Path(os.environ.get('Q4T_TEST_OUTPUT_DIR', ROOT/'.q4t-work/realtime-cpu'))
        root.mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix='capture-',dir=root))
        output, errors = directory/'partial.json', directory/'partial.stderr'
        command = [sys.executable,'-u','-c',
            "import time,sys;print('{\"partial\":true}');print('before timeout',file=sys.stderr);time.sleep(10)"]
        with self.assertRaises(subprocess.TimeoutExpired):
            capture_actor(command, {}, output, errors, dict(os.environ), 1)
        self.assertEqual(json.loads(output.read_text()), {'partial':True})
        self.assertIn('before timeout', errors.read_text())

    def test_journal_receipt_must_keep_all_original_events_and_attempts(self):
        event = dict(type='response.done')
        rows = [dict(direction='client',monotonic_ns='1',event=dict(type='response.create')),
                dict(direction='server',monotonic_ns='2',event=event)]
        receipt = dict(events=[event],response_creates=1)
        c.journal_matches(rows, receipt)
        for bad in ([rows[0]], [rows[1]], rows + [rows[1]]):
            with self.assertRaises(RuntimeError): c.journal_matches(bad, receipt)


    def test_frozen_plan_cannot_expand_scope_or_replace_real_source(self):
        plan = dict(schema='q4t-realtime-duplex-business-v1',limits=copy.deepcopy(LIMITS),
            contract=copy.deepcopy(CONTRACT), record=dict(path=str(ROOT/'docs/STATUS.md'),version='rev376'))
        check_plan(plan)
        for kind in ('budget','contract','source'):
            bad = copy.deepcopy(plan)
            if kind == 'budget': bad['limits']['starts'] += 1
            elif kind == 'contract': bad['contract']['automatic_replay'] = True
            else: bad['record']['path'] = 'invented'
            with self.assertRaises(RuntimeError): check_plan(bad)


if __name__ == '__main__': unittest.main()
