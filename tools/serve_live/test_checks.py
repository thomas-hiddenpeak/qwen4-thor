"""Reject evidence omissions/forged receipts before actual GPU business."""
import base64
import copy
import unittest
from tools.serve_live.checks import observe


def fixture():
    record=dict(path='docs/STATUS.md',sha256='a'*64,version='rev426')
    resource=dict(id='live_test',status='active',store=False,
        audio=dict(format={'type':'audio/pcm','rate':24000}),
        delegation=dict(type='client',acknowledgement_delay_ms=600000))
    events=[]
    def event(kind,**rest):
        value=dict(type=kind,**rest)
        if kind!='session.output_audio.delta':value['event_id']='id_'+str(len(events))
        events.append(value);return value
    event('session.started',session=copy.deepcopy(resource))
    event('session.input_audio.muted',client_event_id='mute')
    event('session.input_audio.unmuted',client_event_id='unmute')
    event('session.updated',session=copy.deepcopy(resource),client_event_id='delay')
    event('session.input_transcript.delta',delta='真实',start_ms=10,end_ms=1000)
    event('session.input_transcript.delta',delta='输入',start_ms=10,end_ms=1000)
    event('session.delegation.created',delegation=dict(type='delegation',id='task1',target='client'))
    event('session.thinking.appended',client_event_id='project_result',start_ms=1000,end_ms=1000)
    event('session.commentary.appended',client_event_id='project_answer',start_ms=1000,end_ms=1000)
    event('session.output_audio.delta',delta=base64.b64encode(bytes([1,2])*2400).decode())
    event('session.output_transcript.delta',delta='rev426',start_ms=1000,end_ms=1100)
    event('session.usage.updated',usage=dict(seconds=1.5))
    event('session.closed',session=copy.deepcopy(resource),reason='close_requested',
          client_event_id='finish',usage=dict(seconds=2.0))
    journal=[dict(direction='server',event=e) for e in events]
    journal.insert(7,dict(direction='client',event=dict(type='session.thinking.append',
        delegation_id='task1',content='rev426 '+record['sha256'])))
    receipt=dict(status='PASS',events=events,connections=1,close_code=1000,
        automatic_replay=False,delegation_id='task1',record=record,expected_input='真实输入')
    before={'q4t_client_rate_admitted_total{client="alpha"}':0,
            'q4t_client_rate_settled_tokens_total{client="alpha"}':0}
    after={'q4t_client_rate_admitted_total{client="alpha"}':3,
           'q4t_client_rate_settled_tokens_total{client="alpha"}':72,
           'q4t_client_rate_reserved_tokens{client="alpha"}':0,
           'q4t_active_chats':0,'q4t_realtime_sessions':0,
           'q4t_realtime_host_reserved_bytes':0,'q4t_seq_slots_free':1}
    return [receipt,journal,before,after,record]


class Checks(unittest.TestCase):
    def test_real_deltas_accounting_and_quality_are_separate(self):
        args=fixture();r=observe(*args)
        self.assertEqual([q['status'] for q in r['quality']],['PASS','PASS'])
        self.assertEqual(r['waveform_bytes'],4800)
        self.assertEqual(r['activity_seconds'],2.0)
        args[0]['events'][10]['delta']='wrong original answer'
        r=observe(*args)
        self.assertEqual(r['protocol'],'PASS')
        self.assertEqual(r['quality'][1]['status'],'FAIL')
        self.assertEqual(r['quality'][1]['actual'],'wrong original answer')

    def rejected(self,mutate):
        args=fixture();mutate(args)
        with self.assertRaises((ValueError,RuntimeError,KeyError,AssertionError)):
            observe(*args)

    def test_record_must_be_actual_and_fed_back(self):
        self.rejected(lambda a:a[1][7]['event'].update(content='invented'))

    def test_delegation_ownership(self):
        self.rejected(lambda a:a[0].update(delegation_id='other'))

    def test_missing_drained_close(self):
        self.rejected(lambda a:a[0].update(close_code=1006))

    def test_audio_receipt_duration(self):
        self.rejected(lambda a:a[0]['events'][10].update(end_ms=1200))

    def test_journal_cannot_drop_events(self):
        self.rejected(lambda a:a[1].pop())

    def test_no_extra_model_or_reservation(self):
        for key in ('q4t_client_rate_admitted_total{client="alpha"}',
                    'q4t_client_rate_reserved_tokens{client="alpha"}',
                    'q4t_realtime_host_reserved_bytes'):
            with self.subTest(key=key):self.rejected(lambda a:a[3].update({key:9}))

    def test_no_duplicate_or_synthetic_audio_metadata(self):
        self.rejected(lambda a:a[0]['events'][9].update(event_id='fake'))
        self.rejected(lambda a:a[0]['events'][8].update(event_id='id_7'))

    def test_live_activity_must_be_monotonic(self):
        self.rejected(lambda a:a[0]['events'][-1].update(usage=dict(seconds=1)))

    def test_silent_bytes_are_not_real_speech(self):
        self.rejected(lambda a:a[0]['events'][9].update(delta=base64.b64encode(bytes(4800)).decode()))


if __name__=='__main__':unittest.main()
