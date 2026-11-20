"""Synthetic fault receipts cannot replace actual native input qualification."""
import base64
import copy
import hashlib
import json
import unittest
from unittest import mock
from tools.serve_realtime import duplex_managed as m, managed
from tools.serve_realtime import duplex_service_acceptance as service

RAW = b'\x01\x02' * 100895


def fixture(mode='complete'):
    settings = dict(type='realtime', instructions=m.duplex_task_checks.INSTRUCTIONS,
        output_modalities=['text'], max_output_tokens=128,
        audio=dict(input=dict(format=dict(type='audio/pcm', rate=24000),
        transcription=dict(model='qwen3-asr-0.6b', language='', prompt='',
                           languages=[], keywords=[]), turn_detection=None, noise_reduction=None)))
    usage = dict(input_tokens=10, output_tokens=5, total_tokens=15)
    item = dict(id='input', role='user', type='message', content=[dict(type='input_audio',
        transcript=m.PHRASE, audio=base64.b64encode(RAW[:-2]).decode())])
    response = dict(id='response', status='completed', output_modalities=['text'], usage=usage,
        output=[dict(type='message', content=[dict(type='output_text', text=m.PHRASE)])])
    events = [dict(type='session.created'), dict(type='session.updated', session=settings),
        dict(type='input_audio_buffer.committed', item_id='input', previous_item_id=None),
        dict(type='conversation.item.input_audio_transcription.completed',
             item_id='input', content_index=0, transcript=m.PHRASE, usage=usage),
        dict(type='conversation.item.retrieved', item=item),
        dict(type='response.created', response=dict(id='response')),
        dict(type='response.output_text.delta', response_id='response', delta=m.PHRASE),
        dict(type='response.done', response=response)]
    client = [dict(type='input_audio_buffer.append', audio=base64.b64encode(RAW).decode()),
        dict(type='input_audio_buffer.commit'),dict(type='response.create', response=dict(
        conversation='auto', instructions=m.duplex_task_checks.INSTRUCTIONS))]
    partial = None
    if mode == 'hold_stream':
        slow = copy.deepcopy(settings)
        slow['audio']['input']['transcription']['delay'] = 'low'
        events.extend([dict(type='session.updated', session=slow),
            dict(type='conversation.item.input_audio_transcription.delta', item_id='partial', delta='甚至')])
        client.append(dict(type='input_audio_buffer.append', audio=base64.b64encode(RAW[:144000]).decode()))
        partial = dict(item_id='partial', delta='甚至')
    for i, e in enumerate(events):e['event_id'] = 'cpu-'+str(i)
    value = dict(phase='result', status='PASS', mode=mode, transport='official-openai-wss',
        connections=1, automatic_replay=False, response_creates=1,
        input_sha256=hashlib.sha256(RAW).hexdigest(), complete_audio_bytes=len(RAW),
        close_code=1000 if mode=='complete' else 1006, events=events,
        responses=[response], retrievals=[item], partial=partial,
        partial_audio_bytes=144000 if partial else 0, sent_events=len(client),
        event_bytes=sum(len(json.dumps(e).encode()) for e in events), transport_errors=[])
    journal = [dict(direction=direction, event=e, monotonic_ns=str(i))
        for direction, rows in [('client',client),('server',events)] for i,e in enumerate(rows)]
    return value, journal


def patch_input(test):
    for key, data in [('REALTIME_INPUT_SHA', RAW), ('REALTIME_WAVEFORM_SHA', RAW[:-2])]:
        patch = mock.patch.object(m.profile_evidence, key, hashlib.sha256(data).hexdigest())
        patch.start()
        test.addCleanup(patch.stop)


class Contracts(unittest.TestCase):
    def setUp(self):patch_input(self)

    def check(self, pair, mode='complete'):
        return m.check_session(pair[0], mode, 'official-openai-wss', pair[1])

    def test_complete_and_held_connection_require_both_actual_work_units(self):
        for mode in ('complete','hold_complete'):
            result=self.check(fixture(mode),mode)
            self.assertEqual(result['tokens'],30)
            self.assertEqual(len(result['ids']),2)

    def test_continuous_prefix_is_distinct_and_cannot_complete_or_publish(self):
        value,journal=fixture('hold_stream');self.check((value,journal),'hold_stream')
        event=dict(type='conversation.item.done',event_id='leak',item=dict(id='partial'))
        value['events'].append(event);journal.append(dict(direction='server',event=event,monotonic_ns='10'))
        with self.assertRaisesRegex(RuntimeError,'public history'):
            self.check((value,journal),'hold_stream')

    def test_wrong_model_answer_and_hidden_stream_mismatch_are_failures(self):
        for stream in (False,True):
            value,journal=fixture();value['responses'][0]['output'][0]['content'][0]['text']='wrong'
            if stream:next(e for e in value['events'] if e['type']=='response.output_text.delta')['delta']='wrong'
            with self.assertRaises(RuntimeError):self.check((value,journal))

    def test_changed_sent_audio_and_missing_terminal_cannot_pass(self):
        for mutate in ('sent','terminal'):
            value,journal=fixture()
            if mutate=='sent':journal[0]['event']['audio']=base64.b64encode(b'\0'*len(RAW)).decode()
            else:value['events'].pop()
            with self.assertRaises(RuntimeError):self.check((value,journal))

    def test_side_response_or_synthetic_user_input_is_rejected(self):
        for extra in (dict(conversation='none'),dict(input=[])):
            value,journal=fixture();journal[2]['event']['response'].update(extra)
            with self.assertRaisesRegex(RuntimeError,'native input history'):self.check((value,journal))

    def test_hinted_asr_or_replayed_connection_is_rejected(self):
        for key in ('hint','replay'):
            value,journal=fixture()
            if key=='hint':value['events'][1]['session']['audio']['input']['transcription']['prompt']=m.PHRASE
            else:value['connections']=2
            with self.assertRaises(RuntimeError):self.check((value,journal))

    def test_accounting_requires_real_interrupted_work_and_exact_success(self):
        before={k:0 for k in managed.METRICS};before['free_slots']=1
        after=dict(before,admitted=2,total=2,success=2,settled=30)
        self.assertEqual(m.accounting(before,after,30),30)
        interrupted=dict(after,admitted=3,total=3,aborted=1,settled=35)
        self.assertEqual(m.accounting(before,interrupted,30,True),35)
        for change in (dict(settled=30),dict(aborted=0),dict(errors=1),dict(admitted=4)):
            with self.assertRaises(RuntimeError):m.accounting(before,dict(interrupted,**change),30,True)

    def test_fixed_budget_counts_startup_and_every_native_input(self):
        self.assertEqual(sum(v['starts'] for v in service.LIMITS.values()),5)
        self.assertEqual(sum(v['chats'] for v in service.LIMITS.values()),18)
        self.assertEqual(sum(v['websocket_sessions'] for v in service.LIMITS.values()),6)
        self.assertEqual(sum(v['seconds'] for v in service.LIMITS.values()),1200)
        plan=dict(scenario='tls',duplex_recovery=dict(format=m.FORMAT))
        self.assertEqual(m.phases(plan),[('tls-before','hold_stream'),('tls-after','complete'),('tls-controller','complete')])
        with self.assertRaises(RuntimeError):m.phases(dict(plan,duplex_recovery={"format":"unknown"}))

    def test_old_known_error_or_unknown_usage_is_not_accepted_as_zero(self):
        for key in ('status','usage'):
            value,journal=fixture()
            if key=='status':value['status']='FAIL'
            else:value['responses'][0]['usage']['total_tokens']=0
            with self.assertRaises(RuntimeError):self.check((value,journal))


if __name__=='__main__':unittest.main()
