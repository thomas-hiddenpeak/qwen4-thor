"""Counterexamples for semantic business evidence; no models or network."""
import base64
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from tools.serve_realtime import semantic_acceptance as s


def evidence(continuation=False):
    terminal=dict(item_id='item', transcript=s.base.EXPECTED['zh'],
        usage=dict(type='tokens',input_tokens=180,output_tokens=15,total_tokens=195,
            input_token_details=dict(audio_tokens=60,text_tokens=120)),
        logprobs=[dict(token='词',logprob=-.2,bytes=list('词'.encode()))],
        q4t_semantic_vad=dict(profile='q4t-native-semantic-turn-v1',attempts=1,
            accepted=1,unavailable=0,invalid=0,stale=0,input_tokens=90,output_tokens=7,
            included_in_usage=True,probability_available=True,maximum_wait=False))
    return dict(status='PASS',connections=1,close_code=1000,automatic_replay=False,
        terminal=terminal,delta=terminal['transcript'],sent_samples=160000,
        events=[dict(type=kind,item_id='item') for kind in
            ('input_audio_buffer.speech_started','input_audio_buffer.speech_stopped',
             'input_audio_buffer.committed','conversation.item.input_audio_transcription.completed')],
        continuation=dict(no_commit=True,original_samples_sent=36000,pause_samples=12000,
            total_original_samples=100895) if continuation else None,
        retrieved=dict(item=dict(id='item',content=[dict(transcript=terminal['transcript'],
            audio=base64.b64encode(b'\0\1'*4000).decode())])))


class Evidence(unittest.TestCase):
    def test_plan_fixes_budget_no_asr_prompt_and_local_model_scope(self):
        plan=dict(schema='q4t-semantic-turn-business-v1',limits=dict(s.LIMITS),
            settings=s.settings(),split_samples=36000,contrast_text=dict(s.CONTRAST),
            contract=dict(actual_classifier=True,usage_includes_private_work=True,
                preserve_unfinished_turn=True,no_auto_replay=True,no_default_changes=True,
                calibrated=False,guardian_recovery=False,master_complete=False))
        s.check_plan(plan)
        for field,value in [('starts',2),('chats',4)]:
            changed=copy.deepcopy(plan);changed['limits'][field]=value
            with self.assertRaises(Exception):s.check_plan(changed)
        changed=copy.deepcopy(plan)
        changed['settings']['audio']['input']['transcription']['prompt']='reference answer'
        with self.assertRaises(Exception):s.check_plan(changed)

    def test_actual_classifier_usage_and_both_turn_modes(self):
        for continuation in (False,True):
            tokens,observed=s.check_turn(evidence(continuation),continuation)
            self.assertEqual(tokens,195);self.assertEqual(observed['retained_bytes'],8000)

    def test_silent_fallback_or_unfinished_classifier_cannot_pass(self):
        for field,value in [('accepted',0),('attempts',9),('maximum_wait',True),
                            ('probability_available',False),('stale',1),('accepted',True)]:
            row=evidence();row['terminal']['q4t_semantic_vad'][field]=value
            with self.assertRaises(Exception):s.check_turn(row,False)

    def test_private_work_must_be_in_actual_parent_usage(self):
        for field,value in [('input_tokens',0),('input_tokens',180),('output_tokens',15),
                            ('output_tokens',129),('included_in_usage',False)]:
            row=evidence();row['terminal']['q4t_semantic_vad'][field]=value
            with self.assertRaises(Exception):s.check_turn(row,False)
        row=evidence();row['terminal']['usage']['total_tokens']+=1
        with self.assertRaises(Exception):s.check_turn(row,False)

    def test_continuation_events_errors_and_retained_identity_are_required(self):
        row=evidence(True);row['continuation']['no_commit']=False
        with self.assertRaises(Exception):s.check_turn(row,True)
        row=evidence();row['events'].append(dict(type='error',error=dict(code='failed')))
        with self.assertRaises(Exception):s.check_turn(row,False)
        row=evidence();row['events'][0]['item_id']='foreign'
        with self.assertRaises(Exception):s.check_turn(row,False)
        row=evidence();row['retrieved']['item']['content'][0]['audio']='AA=='
        with self.assertRaises(Exception):s.check_turn(row,False)

    def test_classifier_requires_finite_probability_and_completed_generation(self):
        def wire(content='{"p":0.9}',finish='stop'):
            return dict(choices=[dict(finish_reason=finish,message=dict(content=content))],
                        usage=dict(prompt_tokens=100,completion_tokens=8,total_tokens=108))
        self.assertEqual(s.check_score(wire()),.9)
        for content in ('{"p":true}','{"p":NaN}','{"p":1.1}','{"p":-1}',
                        '{"p":0.9,"extra":1}','{"p":0.8,"p":0.9}','{}'):
            with self.assertRaises(Exception):s.check_score(wire(content))
        with self.assertRaises(Exception):s.check_score(wire(finish='length'))


class Metadata(unittest.TestCase):
    def call(self, status, payload):
        received=[]
        class Peer(BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def do_GET(self):
                received.append((self.path,self.headers.get('X-Q4T-Client-Id'),
                                 self.headers.get('X-Q4T-Gateway-Token')))
                self.send_response(status);self.send_header('Content-Length',str(len(payload)))
                self.end_headers();self.wfile.write(payload)
        root=Path(os.environ.get('Q4T_TEST_OUTPUT_DIR',s.WORK/'metadata-cpu-fixtures'))
        root.mkdir(parents=True,exist_ok=True)
        out=Path(tempfile.mkdtemp(prefix='metadata-',dir=root))
        server=ThreadingHTTPServer(('127.0.0.1',0),Peer)
        worker=threading.Thread(target=server.serve_forever);worker.start()
        stage=s.Stage.__new__(s.Stage);stage.plan={'port':server.server_port}
        stage.token='cpu-only-token';stage.out=out;charged=[]
        stage.budget=lambda kind:charged.append(kind)
        try:
            if status != 200:
                with self.assertRaisesRegex(RuntimeError,'metadata HTTP status: 401'):
                    stage.metadata('/v1/capabilities','observed')
            elif len(payload)>1<<20:
                with self.assertRaisesRegex(RuntimeError,'metadata response bound'):
                    stage.metadata('/v1/config','observed')
            else:
                self.assertEqual(stage.metadata('/v1/capabilities','observed'),{'configured':True})
        finally:
            server.shutdown();server.server_close();worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(received),1)
        self.assertEqual(received[0][1:],('gamma','cpu-only-token'))
        self.assertEqual(charged,['controls'])
        self.assertEqual((out/'observed.wire').read_bytes(),payload[:(1<<20)+1])
        self.assertEqual(json.loads((out/'observed.http.json').read_text())['status'],status)

    def test_metadata_sends_bound_client_identity(self):
        self.call(200,b'{"configured":true}')

    def test_metadata_preserves_unauthorized_response_without_misdiagnosis(self):
        self.call(401,b'{"error":{"code":"client_authentication_failed"}}')

    def test_metadata_response_has_explicit_byte_limit(self):
        self.call(200,b' ' *((1<<20)+1))


if __name__=='__main__':unittest.main()
