"""Targeted CPU counterexamples, never a model-quality acceptance."""
import copy
import unittest
from tools.serve_realtime import idle_checks as c
from tools.serve_realtime.idle_acceptance import check_plan, LIMITS, CONTRACT
from tools.serve_realtime.test_duplex_acceptance import response_fixture


def prompt_fixture():
    before=dict(request=dict(temperature=1,messages=[dict(role='developer',content='policy'),
        dict(role='user',content=[dict(type='text',text=c.OLD_CUE)])]),
        snapshot=[dict(content=[dict(type='input_text',text=c.OLD_CUE)])],prompt='policy\n'+c.OLD_CUE)
    after=copy.deepcopy(before)
    after['request']['messages'][-1]['content'][0]['text']=c.NEW_CUE
    after['snapshot'][-1]['content'][0]['text']=c.NEW_CUE
    after['prompt']='policy\n'+c.NEW_CUE
    return before,after


def idle_fixture():
    v=response_fixture();v['mode']='idle';v['responses']=v['responses'][:2];v['events']=v['events'][:6]
    v['events'] = [dict(type='input_audio_buffer.speech_started',item_id='speech'),
        dict(type='input_audio_buffer.committed',item_id='speech'),
        dict(type='conversation.item.input_audio_transcription.completed',item_id='speech',content_index=0,
             transcript='answer',usage=dict(type='tokens',input_tokens=2,output_tokens=1,total_tokens=3))] + v['events'][:3] + [
        dict(type='input_audio_buffer.timeout_triggered',item_id='empty',audio_start_ms=5,audio_end_ms=5),
        dict(type='input_audio_buffer.committed',item_id='empty')] + v['events'][3:]
    for i,event in enumerate(v['events']):event['event_id']='e'+str(i)
    v.update(status='PASS',transport='official-openai-wss',connections=1,close_code=1000,
        automatic_replay=False,response_creates=0,quality=[c.c.judgment('vad_auto','answer','answer'),
        c.c.judgment('idle','answer','READY')])
    return v


class Checks(unittest.TestCase):
    def test_reconstruction_rejects_policy_sampling_and_history_changes(self):
        before,after=prompt_fixture();c.prompt_pair(before,after)
        for name in ('policy','sampling','history','render'):
            bad=copy.deepcopy(after)
            if name=='policy':bad['request']['messages'][0]['content']='changed'
            elif name=='sampling':bad['request']['temperature']=0
            elif name=='history':bad['snapshot'][0]['content'][0]['text']='changed'
            else:bad['prompt']='changed'
            with self.assertRaises(RuntimeError):c.prompt_pair(before,bad)

    def test_original_quality_failure_is_kept_separate_from_protocol(self):
        v=idle_fixture();result=c.observe(v,'answer')
        self.assertEqual(result['visible_tokens'],13)
        self.assertEqual(result['admitted'],3)
        self.assertEqual(result['quality'][1]['status'],'FAIL')
        bad=copy.deepcopy(v);bad['quality'][1]['actual']='READY'
        with self.assertRaises(RuntimeError):c.observe(bad,'answer')

    def test_idle_requires_original_native_input_and_response_order(self):
        for kind in ('samples','fake_asr','order','manual_response','extra_response'):
            v=idle_fixture()
            if kind=='samples':v['events'][6]['audio_end_ms']=6
            elif kind=='fake_asr':v['events'].append(dict(type='conversation.item.input_audio_transcription.completed',event_id='fake',item_id='empty'))
            elif kind=='order':v['events'][6],v['events'][5]=v['events'][5],v['events'][6]
            elif kind=='manual_response':v['response_creates']=1
            else:v['events'].append(dict(type='response.created',event_id='extra',response=dict(id='other')))
            with self.assertRaises(RuntimeError):c.observe(v,'answer')

    def test_frozen_limits_and_inputs_cannot_expand(self):
        before,after=prompt_fixture()
        plan=dict(schema='q4t-realtime-idle-followup-v1',limits=copy.deepcopy(LIMITS),
                  contract=copy.deepcopy(CONTRACT),prompts=dict(before=before,after=after))
        check_plan(plan)
        for key in ('limits','contract'):
            bad=copy.deepcopy(plan);bad[key]['new']='not frozen'
            with self.assertRaises(RuntimeError):check_plan(bad)
