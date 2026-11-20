"""Bounded recovery counterexamples; synthetic waveform never qualifies a model."""
import base64
import copy
import hashlib
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

from tools.serve_realtime import managed, speech_checks as c, speech_managed as m
from tools.serve_realtime import speech_service_acceptance as service
from tools.serve_recovery import profile_speech_evidence as evidence
from tools.serve_recovery.test_profile_speech import session


def fixture(mode='complete'):
    source=session('builtin')
    settings=dict(type='realtime',model='qwen3.8-flash-next',output_modalities=['audio'],
        max_output_tokens=3457,audio=dict(output=dict(voice='ryan',speed=1,
        format=dict(type='audio/pcm',rate=24000))))
    events=copy.deepcopy(source['events'])
    events[0]=dict(type='session.updated',session=settings)
    events[1]['item']['id']='user_speech'
    events[1]['item']['content'][0]['text']='Repeat exactly without adding words: '+c.PHRASE
    events.insert(0,dict(type='session.created',session=settings))
    rows=[copy.deepcopy(source['responses'][0]['response'])]
    partial=None
    if mode=='hold_stream':
        partial=dict(response_id='pending',item_id='pending_item',delta=base64.b64encode(b'\x01\x02'*400).decode())
        events.extend([dict(type='response.created',response=dict(id='pending')),
            dict(type='response.output_audio.delta',**partial)])
    for i,event in enumerate(events):event['event_id']='event_'+str(i)
    return dict(phase='result',status='PASS',mode=mode,transport='official-openai-wss',
        connections=1,automatic_replay=False,response_creates=2 if partial else 1,
        close_code=1000 if mode=='complete' else 1006,responses=rows,events=events,
        event_bytes=150000,sent_events=6,retrievals=[source['retrievals'][0]['item']],
        partial=partial,transport_errors=[])


class Contract(unittest.TestCase):
    def test_cold_entry_defers_production_imports_until_sealed_source_pin(self):
        script=Path(service.__file__).resolve()
        code='''import runpy,sys
entry=runpy.run_path(sys.argv[1])
assert entry['GLOBALS']['Stage'] is entry['Stage']
assert not any(n == 'tools.serve_recovery' or n.startswith('tools.serve_recovery.')
               for n in sys.modules), 'production module loaded before sealed source pin'
'''
        result=subprocess.run([sys.executable,'-I','-B','-c',code,str(script)],
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=5)
        self.assertEqual(result.returncode,0,result.stderr.decode())

    def setUp(self):
        raw=c.decode64(fixture()['retrievals'][0]['content'][0]['audio'])
        patch=mock.patch.object(evidence,'PCM',dict(builtin=(len(raw),hashlib.sha256(raw).hexdigest())))
        patch.start();self.addCleanup(patch.stop)

    def test_complete_and_interrupted_audio_keep_exact_bytes_and_positive_usage(self):
        for mode in ('complete','hold_stream','hold_complete'):
            value=fixture(mode);original=copy.deepcopy(value)
            self.assertEqual(m.check_session(value,mode,'official-openai-wss'),1101)
            self.assertEqual(value,original)

    def reject(self,mutate,mode='complete'):
        value=fixture(mode);mutate(value)
        with self.assertRaises((RuntimeError,ValueError,KeyError)):
            m.check_session(value,mode,'official-openai-wss')

    def test_changed_waveform_voice_or_actual_session_cannot_pass(self):
        for change in [lambda v:v['retrievals'][0]['content'][0].update(audio='AAAA'),
            lambda v:v['events'][1]['session']['audio']['output'].update(voice='vivian'),
            lambda v:v['responses'][0]['audio']['output'].update(speed=.75),
            lambda v:v['events'][2]['item'].update(role='assistant')]:self.reject(change)

    def test_completed_or_silent_request_cannot_be_claimed_interrupted(self):
        for change in [lambda v:v.update(partial=None),lambda v:v['partial'].update(delta=''),
            lambda v:v['events'].append(dict(type='response.done',event_id='bad',
                response=dict(id='pending',status='completed')))]:self.reject(change,'hold_stream')

    def test_replay_dropped_terminal_and_transport_fallback_fail(self):
        for change in [lambda v:v.update(automatic_replay=True),lambda v:v.update(connections=2),
            lambda v:v.update(transport='ws-loopback'),lambda v:v['events'].pop(),
            lambda v:v.update(close_code=1006),lambda v:v.update(sent_events=True),
            lambda v:v['events'].append(v['events'][0])]:self.reject(change)

    def test_complete_and_cancelled_accounting_requires_exact_request_ownership(self):
        before=dict.fromkeys(managed.METRICS,0);before['free_slots']=1
        complete=dict(before,admitted=1,total=1,success=1,settled=1101)
        cancelled=dict(before,admitted=2,total=2,success=1,aborted=1,settled=1200)
        self.assertEqual(m.accounting(before,complete,1101),1101)
        self.assertEqual(m.accounting(before,cancelled,1101,True),1200)
        for key,value in [('admitted',1),('success',2),('aborted',0),('errors',1),('settled',1100)]:
            with self.assertRaises(RuntimeError):m.accounting(before,dict(cancelled,**{key:value}),1101,True)
        with self.assertRaises(RuntimeError):m.accounting(before,dict(complete,settled=1102),1101)

    def test_frozen_budget_counts_startup_selfcheck_and_every_speech_attempt(self):
        totals=dict(starts=0,chats=0,probes=0,websocket_sessions=0,seconds=0)
        for scenario,limits in service.LIMITS.items():
            phases=m.phases(dict(scenario=scenario,speech_recovery=dict(format=m.FORMAT)))
            self.assertEqual(limits['websocket_sessions'],len(phases))
            self.assertEqual(limits['chats'],limits['starts']+sum(2 if mode=='hold_stream' else 1 for _,mode in phases))
            for key in totals:totals[key]+=limits[key]
        self.assertEqual(totals,dict(starts=5,chats=12,probes=1,websocket_sessions=6,seconds=1200))
        with self.assertRaises(RuntimeError):m.phases(dict(scenario='tls',speech_recovery=dict(format='unknown')))

    def test_gateway_stage_and_controller_stage_keep_explicit_connections(self):
        self.assertEqual(m.phases(dict(scenario='tls',speech_recovery=dict(format=m.FORMAT))),
            [('tls-before','hold_stream'),('tls-after','complete'),('tls-controller','complete')])
        usage=dict(phase='active',generation=2,source=None,snapshot=None)
        self.assertEqual(service.P['cold_controller_usage'](usage,2,160,161)['mode'],
                         'full_window_after_unsealed_controller_death')
        with self.assertRaises(RuntimeError):service.P['cold_controller_usage'](usage,2,160,159)


if __name__=='__main__':unittest.main()
