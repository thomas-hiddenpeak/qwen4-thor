"""Counterexamples for bounded task clarification without rewriting failures."""
import copy
import unittest
from pathlib import Path
from unittest import mock
from tools.serve_recovery import ledger,profile_duplex_followup as f
from tools.serve_recovery import profile_duplex_evidence as d
from tools.serve_realtime import duplex_quality_checks as c


def event(kind, **data):
    return dict(type=kind, **data)


def fixture(mode):
    label='mixed' if mode=='mixed' else 'vad_auto'
    usage=dict(input_tokens=4,output_tokens=3,total_tokens=7)
    response=dict(id='response',status='completed',output_modalities=['text'],usage=usage,
        output=[dict(type='message',id='answer',content=[dict(type='output_text',text=d.PHRASE)])])
    settings=dict(instructions=c.INSTRUCTIONS,audio=dict(input=dict(turn_detection=dict(create_response=True,idle_timeout_ms=None))))
    events=[event('session.updated',session=copy.deepcopy(settings)) for _ in range(2)]
    if mode=='vad_once':
        events += [event('input_audio_buffer.speech_started',item_id='voice'),
                   event('input_audio_buffer.committed',item_id='voice')]
    parts=[('mixed_input',1),('mixed_input',3)] if mode=='mixed' else [('voice',0)]
    events += [event('conversation.item.input_audio_transcription.completed',item_id=i,
        content_index=p,transcript=d.PHRASE,usage=usage) for i,p in parts]
    events += [event('response.created',response=dict(id='response')),
        event('response.output_text.delta',response_id='response',delta=d.PHRASE),
        event('response.done',response=response)]
    for i,e in enumerate(events):e['event_id']='cpu-'+str(i)
    retrievals=[]
    if mode=='mixed':
        retrievals=[dict(label='mixed',item=dict(id='mixed_input',content=[
            dict(type='input_text',text='Repeat the first audio sentence only.'),
            dict(type='input_audio',audio='AAA=',transcript='REFERENCE_ONLY_NEVER_SPOKEN'),
            dict(type='input_text',text='Second audio follows.'),dict(type='input_audio',audio='AAA=')]))]
    return dict(mode=mode,status='PASS',transport='official-openai-wss',connections=1,close_code=1000,
        automatic_replay=False,response_creates=int(mode=='mixed'),instruction_policy=c.POLICY,
        responses=[dict(label=label,response=response)],events=events,retrievals=retrievals,
        quality=[d.duplex_checks.judgment(label,d.PHRASE,d.PHRASE)])


class Observed(unittest.TestCase):
    def test_both_tasks_bind_actual_parts_text_and_usage(self):
        for mode,count in [('mixed',3),('vad_once',2)]:
            result=c.observe(fixture(mode),d.PHRASE)
            self.assertEqual(result['admitted'],count)
            self.assertEqual(result['visible_tokens'],7*count)

    def test_missing_input_wrong_index_replay_and_lost_delta_fail(self):
        for mode in ('mixed','vad_once'):
            original=fixture(mode)
            def missing(v):v['events']=[e for e in v['events'] if not e['type'].endswith('transcription.completed')]
            def wrong(v):next(e for e in v['events'] if e['type'].endswith('transcription.completed'))['content_index']=4
            def replay(v):v['events'].append(copy.deepcopy(v['events'][0]))
            def delta(v):next(e for e in v['events'] if e['type']=='response.output_text.delta')['delta']='unrelated'
            for mutate in (missing,wrong,replay,delta):
                bad=copy.deepcopy(original);mutate(bad)
                with self.assertRaises(RuntimeError):c.observe(bad,d.PHRASE)

    def test_changed_caller_policy_and_transport_do_not_qualify(self):
        for mode in ('mixed','vad_once'):
            for mutate in [lambda v:v.update(instruction_policy='unknown'),
                lambda v:v.update(automatic_replay=True),lambda v:v.update(connections=2),
                lambda v:v['events'][0]['session'].update(instructions=d.PHRASE),
                lambda v:v.update(transport='unverified')]:
                bad=fixture(mode);mutate(bad)
                with self.assertRaises(RuntimeError):c.observe(bad,d.PHRASE)

    def test_quality_is_derived_and_cannot_be_relabelled(self):
        value=fixture('vad_once');value['responses'][0]['response']['output'][0]['content'][0]['text']='translated'
        next(e for e in value['events'] if e['type']=='response.output_text.delta')['delta']='translated'
        with self.assertRaises(RuntimeError):c.observe(value,d.PHRASE)
        value['quality']=[d.duplex_checks.judgment('vad_auto','translated',d.PHRASE)]
        self.assertEqual(c.observe(value,d.PHRASE)['quality'][0]['status'],'FAIL')


class SourceAndDiagnostics(unittest.TestCase):
    def row(self,variant):
        return dict(label='mixed',variant=variant,seed=41,request=dict(copy.deepcopy(f.REQUESTS['mixed'][variant]),seed=41),
            status=200,response=dict(choices=[dict(finish_reason='stop',message=dict(content=d.PHRASE))],
                usage=dict(total_tokens=9)),quality=d.duplex_checks.judgment('mixed',d.PHRASE,d.PHRASE))

    def test_fixed_http_diagnostics_bind_request_sampling_and_seed(self):
        row=self.row('after');f.diagnostic(row,'mixed','after',41)
        for mutate in [lambda v:v['request'].update(temperature=0),lambda v:v.update(seed=True),
            lambda v:v['request']['messages'][1].update(content=d.PHRASE),
            lambda v:v.update(status=500),lambda v:v['response']['choices'][0].update(finish_reason='length')]:
            bad=copy.deepcopy(row);mutate(bad)
            with self.assertRaises(ValueError):f.diagnostic(bad,'mixed','after',41)

    def test_before_fail_is_preserved_after_failure_still_rejects(self):
        for variant in ('before','after'):
            row=self.row(variant);row['response']['choices'][0]['message']['content']='wrong'
            row['quality']=d.duplex_checks.judgment('mixed','wrong',d.PHRASE)
            if variant=='before':f.diagnostic(row,'mixed',variant,41)
            else:
                with self.assertRaises(ValueError):f.diagnostic(row,'mixed',variant,41)

    def test_source_and_original_failure_cannot_be_replaced(self):
        value=dict(format=f.FORMAT,original={},followup={},diagnostics=[],source_review=f.REVIEW)
        with self.assertRaises(ValueError):f.business(value)
        value['source_review']={}
        with self.assertRaises(ValueError):f.business(value)

    def test_clarification_has_no_expected_answer_and_changes_only_instructions(self):
        self.assertNotIn(d.PHRASE,c.INSTRUCTIONS)
        source=(Path(__file__).parents[2]/'tools/serve_realtime/duplex_sdk.mjs').read_text()
        self.assertIn(c.INSTRUCTIONS,source)
        for pair in f.REQUESTS.values():
            before=copy.deepcopy(pair['before']);before['messages'][0]['content']=c.INSTRUCTIONS
            self.assertEqual(before,pair['after']);self.assertEqual(before['temperature'],1)
            self.assertNotIn('seed',before)

    def test_repaired_driver_uses_same_client_policy_with_exact_work_bounds(self):
        from tools.serve_recovery import profile_duplex_followup_targeted as stage
        obj=object.__new__(stage.Stage)
        with mock.patch.object(stage.full.Stage,'client_policy',return_value=dict(clients=[dict(id='first')])):
            self.assertEqual(obj.client_policy()['clients'],[dict(id='first'),dict(id='audio')])
        self.assertEqual(stage.LIMITS['chats'],8+5+21)
        self.assertEqual(stage.LIMITS['websocket_sessions'],2+5)
        self.assertEqual(stage.LIMITS['ingress_starts'],1+3)


if __name__=='__main__':unittest.main()
