"""Exercise cross-client Responses receipt checks independently of the GPU."""
import copy
import unittest

from .response_handoff_receipt import verify


class ResponseHandoffTest(unittest.TestCase):
    def setUp(self):
        self.source = b'{"publication_revision":580,"version_revision":219}\n'
        self.request = dict(metadata={'writer':'python'}, input=[
            dict(role='developer',content=[dict(text='Use the receipt')]),
            dict(role='user',content=[dict(text=self.source.decode())])])
        self.followup = dict(metadata={'reader':'node'})
        original = dict(id='resp_a',status='completed',metadata=self.request['metadata'],previous=None,text='580')
        follow = dict(id='resp_b',status='completed',metadata=self.followup['metadata'],previous='resp_a',text='219')
        events = [dict(seq=0,type='response.created'), dict(seq=1,type='response.output_text.delta',delta='580'),
                  dict(seq=2,type='response.completed',response=original)]
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
        self.reader['events'][1]['delta']='581'
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
