"""Synthetic CPU wire tests: capture must continue after a logical SSE error."""
import io
import json
import unittest
from tools.serve_device_memory import correction as c


class Capture(unittest.TestCase):
    def raw(self):
        role=dict(id='r',object='chat.completion.chunk',created=1,model='m',
            choices=[dict(index=0,delta=dict(role='assistant',content=''),
                          finish_reason=None,logprobs=None)],usage=None)
        error=dict(error=dict(message='failed',type='service_unavailable_error',
                             code='gpu_unhealthy',param=None))
        return (''.join('data: '+json.dumps(x)+'\r\n\r\n' for x in (role,error))+
                'data: [DONE]\r\n\r\n').encode()

    def test_full_capture_precedes_fail_fast_parser(self):
        raw=self.raw();parts=[]
        captured=c.capture_response(io.BytesIO(raw),parts.append)
        self.assertEqual(captured,b''.join(parts));self.assertEqual(captured,raw)
        c.v.error_terminal(captured,True,'gpu_unhealthy')
        fast=[]
        with self.assertRaises(c.RemoteError):c.read_sse(io.BytesIO(captured),sink=fast.append)
        self.assertNotIn(b'[DONE]',b''.join(fast))
        self.assertIn(b'[DONE]',captured)

    def test_fragmented_read_and_byte_limit(self):
        class Fragment(io.BytesIO):
            def read1(self,size):return self.read(min(3,size))
        raw=self.raw()
        self.assertEqual(c.capture_response(Fragment(raw),lambda _:None),raw)
        parts=[]
        with self.assertRaises(RuntimeError):c.capture_response(Fragment(raw),parts.append,maximum=16)
        self.assertEqual(b''.join(parts),raw[:17])

    def test_partial_exception_never_becomes_eof(self):
        class Broken:
            calls=0
            def read1(self,size):
                self.calls+=1
                if self.calls==1:return b'data: partial'
                raise TimeoutError('wire stalled')
        parts=[]
        with self.assertRaises(TimeoutError):c.capture_response(Broken(),parts.append)
        self.assertEqual(parts,[b'data: partial'])

    def test_targeted_scope_does_not_replay_accepted_business(self):
        names=[r[0] for r in c.v.SCENARIOS[2:]]
        self.assertEqual(len(names),7)
        self.assertNotIn('model',names);self.assertNotIn('optional-oom',names)
        self.assertEqual({k:c.SPENT[k]+c.LIMITS[k] for k in c.SPENT},c.CUMULATIVE)
        self.assertEqual(c.original.LIMITS['starts'],9)
        self.assertEqual(c.CUMULATIVE['starts'],10)


if __name__=='__main__':unittest.main()
