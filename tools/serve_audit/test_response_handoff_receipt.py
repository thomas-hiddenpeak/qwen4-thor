"""Exercise cross-client Responses receipt checks independently of the GPU."""
import copy
import unittest

from .response_handoff_receipt import verify, verify_text_lifecycle


class ResponseHandoffTest(unittest.TestCase):
    def setUp(self):
        self.source = b'{"publication_revision":580,"version_revision":219}\n'
        self.request = dict(metadata={'writer':'python'}, input=[
            dict(role='developer',content=[dict(text='Use the receipt')]),
            dict(role='user',content=[dict(text=self.source.decode())])])
        self.followup = dict(metadata={'reader':'node'})
        part = dict(type='output_text', text='580', annotations=[], logprobs=[])
        original = dict(id='resp_a',status='completed',metadata=self.request['metadata'],previous=None,
                        text='580',messages=['msg_answer'],message_parts={'0': [part.copy()]})
        follow = dict(id='resp_b',status='completed',metadata=self.followup['metadata'],previous='resp_a',text='219')
        location = dict(item_id='msg_answer', output_index=0)
        events = [dict(seq=0,type='response.created',response_id='resp_a'),
                  dict(location,seq=1,type='response.output_item.added',item_type='message',content=[]),
                  dict(location,seq=2,type='response.content_part.added',content_index=0,part=dict(part, text='')),
                  dict(location,seq=3,type='response.output_text.delta',content_index=0,delta='580'),
                  dict(location,seq=4,type='response.output_text.done',content_index=0,text='580'),
                  dict(location,seq=5,type='response.content_part.done',content_index=0,part=part.copy()),
                  dict(location,seq=6,type='response.output_item.done',item_type='message',content=[part.copy()]),
                  dict(seq=7,type='response.completed',response_id='resp_a',response=original)]
        root = '/v1/responses'
        target = root+'/resp_a'
        api = [('POST',root+'/input_tokens',''),('POST',root,'')]
        later = [('GET',target,''),('GET',target,''),('GET',target+'/input_items','limit=1&order=asc'),
                 ('GET',target+'/input_items','limit=1&after=msg_a&order=asc'),('POST',root,''),
                 ('GET',target,'stream=true'),('DELETE',target,''),('GET',target,'')]
        def wire(spec, index, beta):
            method,path,query=spec
            return dict(method=method,path=path,query='&'.join(filter(None,[query,'beta=true' if beta else ''])),
                        status=404 if index in [3,9] else 200,trace='req-'+str(index))
        self.writer=dict(sdk='python-2.8.1',beta=False,count=98,created=original,events=events,
                         requests=[wire(s,i,False) for i,s in enumerate(api)])
        self.reader=dict(sdk='node-7.47.0',beta=True,before=copy.deepcopy(original),
                         events=copy.deepcopy(events),follow=follow,gone=dict(status=404,code='not_found'),
                         foreign=dict(status=404,code='not_found'),
                         pages=[dict(id='msg_a',role='developer',text=['Use the receipt'],more=True),
                                dict(id='msg_b',role='user',text=[self.source.decode()],more=False)],
                         requests=[wire(s,i,True) for i,s in enumerate(later,2)])
    def verify(self):
        return verify(self.writer,self.reader,self.request,self.followup,self.source)

    def test_cross_selector_handoff(self):
        self.assertEqual(len(self.verify()),10)

    def test_selector_must_appear_on_actual_wire(self):
        self.reader['requests'][0]['query']=''
        with self.assertRaisesRegex(ValueError,'selector'):
            self.verify()

    def test_stream_must_replay_original_deltas(self):
        self.reader['events'][3]['delta']='581'
        with self.assertRaisesRegex(ValueError,'replay'):
            self.verify()

    def test_continuation_needs_distinct_answer_and_original_parent(self):
        for key,value in [('previous','resp_z'),('text','580')]:
            with self.subTest(key=key), self.assertRaisesRegex(ValueError,'continuation'):
                follow=self.reader['follow'].copy()
                self.reader['follow'][key]=value
                try:
                    self.verify()
                finally:
                    self.reader['follow']=follow

    def test_cross_tenant_denial_needs_its_own_request(self):
        self.reader['requests'][1]['trace']=self.reader['requests'][0]['trace']
        with self.assertRaisesRegex(ValueError,'trace'):
            self.verify()

    def test_matching_bad_stream_item_identity_is_rejected(self):
        for actor in (self.reader,self.writer):
            actor['events'][3]['item_id'] = 'msg_other'
        with self.assertRaisesRegex(ValueError,'wrong message'):
            self.verify()

    def test_matching_bad_done_text_is_rejected(self):
        for actor in (self.reader,self.writer):
            actor['events'][4]['text'] = '581'
        with self.assertRaisesRegex(ValueError,'done text'):
            self.verify()

    def test_matching_bad_content_part_events_are_rejected(self):
        for pos, field, value, reason in (
                (0, 'response_id', 'resp_other', 'response identity'),
                (2, 'content_index', 1, 'new text part'),
                (5, 'item_id', 'msg_other', 'wrong message'),
                (5, 'content_index', 1, 'part is missing'),
                (5, 'part', dict(type='output_text', text='581'), 'differs from done text'),
                (6, 'content', [dict(type='output_text', text='580')], 'message parts')):
            with self.subTest(pos=pos, field=field), self.assertRaisesRegex(ValueError, reason):
                writer, reader = copy.deepcopy((self.writer, self.reader))
                for actor in (writer, reader):
                    actor['events'][pos][field] = value
                verify(writer, reader, self.request, self.followup, self.source)

    def test_message_cannot_close_before_its_part(self):
        events = self.writer['events'][:5] + self.writer['events'][6:]
        with self.assertRaisesRegex(ValueError, 'completed message parts'):
            verify_text_lifecycle(events, self.writer['created'])

    def test_two_parts_keep_their_indexes_and_terminal_metadata(self):
        events = copy.deepcopy(self.writer['events'])
        second = copy.deepcopy(events[2:6])
        for event in second:
            event['content_index'] = 1
        second[1]['delta'] = second[2]['text'] = second[3]['part']['text'] = '!'
        events[6]['content'].append(second[3]['part'])
        final = copy.deepcopy(self.writer['created'])
        final['message_parts']['0'].append(second[3]['part'].copy())
        final['text'] += '!'
        events[6:6] = second
        verify_text_lifecycle(events, final)
        final['message_parts']['0'].reverse()
        with self.assertRaisesRegex(ValueError, 'terminal message'):
            verify_text_lifecycle(events, final)
