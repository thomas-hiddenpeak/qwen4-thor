"""Counterexamples for speech, exact-source reuse, and recovery preservation."""
import copy
import hashlib
import unittest
from unittest import mock

from tools.serve_recovery import ledger, profile_evidence as e, profile_migration as m
from tools.serve_recovery import profile_record, profile_speech_evidence as s
from tools.serve_recovery import profile_speech_reuse as reuse
from tools.serve_recovery import profile_speech_targeted as stage
from tools.serve_recovery.test_profile_prompt import declaration as prompt
from tools.serve_realtime import speech_checks
from tools.serve_realtime.test_speech_acceptance import fixture


def declaration():
    value = prompt()
    value.update(speech_output=copy.deepcopy(s.DECLARATION), output_modalities=['text','audio'],
                 pending_execution=['audio_input','mcp','tracing'])
    return value


def session(mode):
    value,_ = fixture('quota'); response = value['responses'][0]['response']
    value['responses'] = value['responses'][:1]
    value['events'] = [event for event in value['events'] if event.get('response_id') == response['id'] or
                      event.get('response',{}).get('id') == response['id']]
    value.update(mode='profile_'+mode, response_creates=1, checks=[], tool_record=None)
    value['responses'][0]['label'] = value['retrievals'][0]['label'] = 'profile_'+mode
    voice = 'ryan' if mode=='builtin' else dict(id=s.VOICE)
    response['audio']['output'].update(voice=voice,speed=1)
    response['usage'].update(input_tokens=1000,output_tokens=101,total_tokens=1101)
    target = s.PHRASES.get(mode,s.PHRASES['custom'])
    response['output'][0]['content'][0]['transcript'] = target
    value['retrievals'][0]['item']['content'][0]['transcript'] = target
    for event in value['events']:
        if event['type']=='response.output_audio_transcript.delta': event['delta']=target
        if event['type']=='response.output_audio_transcript.done': event['transcript']=target
        if event['type']=='response.done': event['response']=copy.deepcopy(response)
    value['quality']=[dict(label='profile_'+mode,actual=target,expected=target,status='PASS')]
    value['events'][:0]=[
        dict(type='session.updated',session=dict(audio=dict(output=dict(voice=voice,speed=1)))),
        dict(type='conversation.item.done',item=dict(id='user_profile',role='user',
             content=[dict(type='input_text',text=s.task(mode))])),
        dict(type='response.created',response=dict(id=response['id']))]
    if mode=='foreign':
        failed=dict(response,id='failed',status='failed',output=[],
                    usage=dict(input_tokens=0,output_tokens=0,total_tokens=0),
                    status_details=dict(error=dict(code='object_not_found')))
        healthy=dict(id='healthy',status='completed',output_modalities=['text'],
                     output=[dict(type='message',content=[dict(type='output_text',text='42')])],
                     usage=dict(input_tokens=30,output_tokens=3,total_tokens=33))
        value.update(response_creates=2,retrievals=[],responses=[dict(label='profile_foreign',response=failed),
            dict(label='profile_after_foreign',response=healthy)],quality=[dict(label='profile_after_foreign',
                actual='42',expected='42',status='PASS')])
        value['events']=value['events'][:2]+[
            dict(type='response.created',response=dict(id='failed')),
            dict(type='response.done',response=copy.deepcopy(failed)),
            dict(type='response.created',response=dict(id='healthy')),
            dict(type='response.output_text.delta',response_id='healthy',delta='42'),
            dict(type='response.done',response=copy.deepcopy(healthy))]
    for i,event in enumerate(value['events']): event['event_id']='event_'+str(i)
    return value


class SpeechEvidence(unittest.TestCase):
    def setUp(self):
        value=session('builtin')
        raw=speech_checks.decode64(value['retrievals'][0]['item']['content'][0]['audio'])
        patch=mock.patch.object(s,'PCM',{mode:(len(raw),hashlib.sha256(raw).hexdigest())
                                       for mode in ('builtin','custom')})
        patch.start();self.addCleanup(patch.stop)

    def test_complete_voice_outputs_and_foreign_failure_have_actual_usage(self):
        for mode,total in [('builtin',1101),('custom',1101),('foreign',33)]:
            self.assertEqual(s.session(session(mode),mode),total)

    def reject(self,mode,mutate):
        value=session(mode);mutate(value)
        with self.assertRaises(Exception):s.session(value,mode)

    def test_content_receipt_cannot_hide_wrong_voice_or_waveform(self):
        for mutate in [lambda v:v['retrievals'][0]['item']['content'][0].update(audio='AAAA'),
                       lambda v:v['responses'][0]['response']['audio']['output'].update(voice='vivian'),
                       lambda v:v['responses'][0]['response']['audio']['output'].update(speed=.75)]:
            self.reject('builtin',mutate)

    def test_old_or_missing_user_turn_cannot_qualify(self):
        for mutate in [lambda v:v['events'].pop(1),
                       lambda v:v['events'][1]['item'].update(role='assistant'),
                       lambda v:v['events'].append(v['events'].pop(1))]:
            self.reject('custom',mutate)

    def test_lost_replayed_and_relabelled_terminals_fail(self):
        for mutate in [lambda v:v['events'].append(v['events'][-1]),
                       lambda v:v['events'].pop(),lambda v:v.update(automatic_replay=True),
                       lambda v:v['quality'][0].update(actual='wrong'),lambda v:v.update(close_code=1006)]:
            self.reject('builtin',mutate)

    def test_foreign_voice_leak_or_hidden_work_fails(self):
        for mutate in [lambda v:v['responses'][0]['response']['usage'].update(total_tokens=7),
                       lambda v:v['responses'][0]['response']['status_details']['error'].update(code='tts_error'),
                       lambda v:v['retrievals'].append(dict(audio='secret')),
                       lambda v:v['responses'][1]['response']['output'][0]['content'][0].update(text='41')]:
            self.reject('foreign',mutate)

    def test_all_three_sessions_require_exact_single_admission_and_settlement(self):
        state=dict(admitted=0,settled=0,reserved=0,active=0,sessions=0,host_bytes=0,free_slots=1)
        value=dict(format=s.FORMAT,snapshot_sha256=s.profile_refusal_reuse.VOICE_SNAPSHOT,sessions={})
        for mode in s.CASES:
            item=session(mode);total=s.session(item,mode)
            value['sessions'][mode]=dict(session=item,before=dict(state),
                after=dict(state,admitted=2 if mode=='foreign' else 1,settled=total))
        s.business(value)
        for key,new in [('admitted',2),('settled',0),('reserved',1),('host_bytes',1024)]:
            wrong=copy.deepcopy(value);wrong['sessions']['builtin']['after'][key]=new
            with self.assertRaises(Exception):s.business(wrong)


class SpeechCapabilities(unittest.TestCase):
    def test_v19_requires_complete_audio_and_keeps_legacy_text_v18(self):
        current=dict(realtime=dict(conversation=declaration()))
        old=dict(realtime=dict(conversation=prompt()))
        self.assertTrue(e.speech_enabled(current));self.assertFalse(e.speech_enabled(old))
        self.assertEqual(e.format_for({}, {},current),e.SPEECH_FORMAT)
        self.assertEqual(e.format_for({}, {},old),e.PROMPT_FORMAT)

    def test_unknown_partial_or_false_declared_speech_is_rejected(self):
        for mutate in [lambda v:v.pop('speech_output'),lambda v:v.update(output_modalities=['text']),
                       lambda v:v['speech_output'].update(rpm='twice'),
                       lambda v:v['speech_output'].update(truncate=1),
                       lambda v:v.update(pending_execution=['mcp','tracing'])]:
            value=declaration();mutate(value)
            with self.assertRaises(ValueError):e.speech_enabled(dict(realtime=dict(conversation=value)))

    def test_extension_preserves_speech_in_automatic_equivalence(self):
        old=m.semantic_capabilities(dict(realtime=dict(conversation=prompt())))
        new=m.semantic_capabilities(dict(realtime=dict(conversation=declaration())))
        self.assertTrue(m.extends(old,new));self.assertFalse(m.extends(new,old))
        self.assertNotEqual(old,new)
        self.assertIn('speech_output',new['realtime']['conversation'])

    def test_eight_mib_bound_only_for_explicit_reviewed_v19(self):
        value=dict(format=e.SPEECH_FORMAT,reused_business=dict(format=reuse.FORMAT))
        profile_record.shape(value,3<<20)
        for changed,size in [(value,(8<<20)+1),
            (dict(value,format=e.PROMPT_FORMAT),3<<20),
            (dict(value,reused_business={}),3<<20)]:
            with self.assertRaises(ValueError):profile_record.shape(changed,size)


class SourceReuse(unittest.TestCase):
    def setUp(self):
        self.review=dict(reuse.REVIEW,previous_binary='a'*64,previous_protocol='b'*64,
                         candidate_binary='c'*64,candidate_protocol='d'*64)
        old=dict(identity=dict(binary_sha256='a'*64,protocol_sha256='b'*64,model='fixed'),
                 capacity=dict(max_tokens=256),capabilities=dict(protocol_source_sha256='b'*64,
                 realtime=dict(conversation=prompt())),business=dict(schema=dict(answer=42),
                 embedding=[1,2,3],tts=dict(speech='oldbytes',chat_response={},health={})))
        source=dict(identity=ledger.digest(old['identity']),capacity=ledger.digest(old['capacity']),
            capabilities=ledger.digest(old['capabilities']),
            business={k:ledger.digest(v) for k,v in old['business'].items()},
            tts=dict(speech=ledger.digest('oldbytes')),record_sha256='e'*64,
            prior_witnesses=dict(voice_prompt='f'*64))
        self.value=copy.deepcopy(old);self.value.update(format=e.SPEECH_FORMAT,profile='primary')
        self.value['identity'].update(binary_sha256='c'*64,protocol_sha256='d'*64)
        self.value['capabilities'].update(protocol_source_sha256='d'*64)
        self.value['capabilities']['realtime']['conversation']=declaration()
        self.value['business']['realtime_speech']={}
        for key,value in [('REVIEW',self.review),('SOURCES',dict(primary=source))]:
            patch=mock.patch.object(reuse,key,value);patch.start();self.addCleanup(patch.stop)

    def test_exact_compact_source_retains_witness_hashes_without_nested_record(self):
        value=reuse.attach(self.value);reuse.validate(value)
        self.assertNotIn('record',value['reused_business'])
        self.assertEqual(value['reused_business']['source']['prior_witnesses'],dict(voice_prompt='f'*64))

    def reject(self,mutate):
        value=reuse.attach(self.value);mutate(value)
        with self.assertRaises(Exception):reuse.validate(value)

    def test_unknown_origin_or_modified_legacy_witness_cannot_reuse(self):
        for mutate in [lambda v:v['reused_business']['review'].update(source_review='0'*64),
                       lambda v:v['reused_business']['source'].update(record_sha256='0'*64),
                       lambda v:v['reused_business']['source']['prior_witnesses'].update(voice_prompt='0'*64)]:
            self.reject(mutate)

    def test_other_binary_asset_capacity_and_capability_cannot_reuse(self):
        for mutate in [lambda v:v['identity'].update(binary_sha256='0'*64),
                       lambda v:v['identity'].update(model='other'),
                       lambda v:v['capacity'].update(max_tokens=1),
                       lambda v:v['capabilities'].update(extra=True)]:self.reject(mutate)

    def test_reused_business_must_match_each_exact_digest(self):
        for key in ('schema','embedding'):
            self.reject(lambda v:v['business'].update({key:'changed'}))
        self.reject(lambda v:v['business']['tts'].update(speech='changed'))

    def test_fresh_scope_is_mandatory_and_cannot_cover_other_changes(self):
        for mutate in [lambda v:v['business'].pop('realtime_speech'),
                       lambda v:v['business'].update(unreviewed={}),
                       lambda v:v['business']['tts'].pop('health'),
                       lambda v:v['reused_business'].update(keys=[])]:self.reject(mutate)


class DriverBudget(unittest.TestCase):
    def test_result_requires_both_capacities_all_connections_and_exact_requests(self):
        value=dict(status=stage.SUCCESS,binary_sha256=stage.reuse.REVIEW['candidate_binary'],seconds=60,
                   counts={k:v for k,v in stage.LIMITS.items() if k!='seconds'})
        stage.check_result(value)
        for key in ('starts','chats','websocket_sessions','ingress_starts'):
            wrong=copy.deepcopy(value);wrong['counts'][key]-=1
            with self.assertRaises(RuntimeError):stage.check_result(wrong)
        for mutate in [lambda v:v.update(status='FAIL'),lambda v:v.update(seconds=1201),
                       lambda v:v.update(binary_sha256='0'*64)]:
            wrong=copy.deepcopy(value);mutate(wrong)
            with self.assertRaises(RuntimeError):stage.check_result(wrong)


if __name__=='__main__':unittest.main()
