"""Actual SDK/native TLS CPU peers for audio recovery; no model or GPU."""
import base64
import copy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest

from tools.serve_realtime import speech_managed
from tools.serve_realtime import test_conversation_recovery_sdk as prior
from tools.serve_realtime.test_speech_service import fixture
from tools.serve_realtime.test_transcription_sdk import frame, read_frame, free_port
from tools.serve_realtime.ingress_fixture import NativeIngress, certificate

ROOT=Path(__file__).resolve().parents[2]


class SpeechSdk(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root=Path(tempfile.mkdtemp(prefix='speech-sdk-',dir=os.environ['Q4T_TEST_OUTPUT_DIR']))
        cls.tls=certificate(cls.root/'credentials');cls.untrusted=certificate(cls.root/'untrusted')

    def setUp(self):
        self.out=Path(tempfile.mkdtemp(prefix='case-',dir=self.root))
        self.commands=[];self.errors=[];self.corrupt_retrieval=False;owner=self
        class Peer(BaseHTTPRequestHandler):
            protocol_version='HTTP/1.1'
            def log_message(self,*_):pass
            def do_GET(self):
                self.close_connection=True;self.connection.settimeout(5)
                accept=base64.b64encode(hashlib.sha1((self.headers['Sec-WebSocket-Key']+
                    '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
                self.wfile.write(('HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\n'
                    'Upgrade: websocket\r\nSec-WebSocket-Accept: '+accept+'\r\n\r\n').encode())
                number=0;turn=0;data=fixture('hold_stream')
                def emit(event):
                    nonlocal number
                    number+=1;event=dict(event,event_id='cpu-'+str(number))
                    self.wfile.write(frame(json.dumps(event).encode()));self.wfile.flush()
                emit(data['events'][0]);emit(dict(type='conversation.created',conversation=dict(id='cpu-conversation')))
                try:
                    while True:
                        row=read_frame(self.rfile)
                        if row is None:return
                        opcode,payload=row
                        if opcode==8:
                            self.wfile.write(frame(payload,8));self.wfile.flush();return
                        event=json.loads(payload);owner.commands.append(event['type'])
                        if event['type']=='session.update':
                            emit(dict(type='session.updated',session=event['session']))
                        elif event['type']=='conversation.item.create':
                            emit(dict(type='conversation.item.done',item=event['item']))
                        elif event['type']=='response.create':
                            turn+=1
                            for message in (data['events'][3:-2] if turn==1 else data['events'][-2:]):emit(message)
                        elif event['type']=='conversation.item.retrieve':
                            item=copy.deepcopy(data['retrievals'][0])
                            if owner.corrupt_retrieval:item['content'][0]['audio']='AAAA'
                            emit(dict(type='conversation.item.retrieved',item=item))
                        else:raise ValueError('unplanned CPU event')
                except (BrokenPipeError,ConnectionResetError):pass
                except BaseException as error:owner.errors.append(repr(error))
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Peer);self.server.daemon_threads=True
        self.thread=threading.Thread(target=self.server.serve_forever);self.thread.start()
        self.front=None;self.addCleanup(self.close)
        self.front=NativeIngress(self.out/'ingress',Path(os.environ['Q4T_INGRESS']),free_port(),
            self.server.server_port,free_port(),'e'*64,{'alpha':'A'*40},self.tls,lambda:True)

    close=prior.RecoverySdk.close
    ready=prior.RecoverySdk.ready
    finish=prior.RecoverySdk.finish

    def start(self,mode,*,plain=False,wrong_ca=False):
        args=dict(base_url=f"{'http' if plain else 'https'}://127.0.0.1:{self.front.port}/v1",
            api_key='A'*40,ca_file=None if plain else
                (self.untrusted if wrong_ca else self.tls)['certificate'],mode=mode)
        env={k:v for k,v in os.environ.items() if k not in ('NODE_OPTIONS','NODE_PATH','LD_PRELOAD')}
        process=subprocess.Popen(['node',str(ROOT/'tools/serve_realtime/speech_recovery_sdk.mjs')],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env)
        def cleanup():
            if process.poll() is None:process.kill()
            process.communicate(timeout=3)
        self.addCleanup(cleanup)
        process.stdin.write(json.dumps(args).encode());process.stdin.close();process.stdin=None
        return process

    def check(self,value,mode,transport='official-openai-wss'):
        from unittest import mock
        from tools.serve_recovery import profile_speech_evidence
        raw=base64.b64decode(fixture()['retrievals'][0]['content'][0]['audio'])
        with mock.patch.object(profile_speech_evidence,'PCM',
                dict(builtin=(len(raw),hashlib.sha256(raw).hexdigest()))):
            return speech_managed.check_session(value,mode,transport)

    def test_complete_official_tls_keeps_audio_bytes_history_and_normal_close(self):
        value=self.finish(self.start('complete'));self.check(value,'complete')
        self.assertEqual(value['connections'],1);self.assertEqual(value['close_code'],1000)
        self.assertEqual(self.commands.count('response.create'),1)

    def test_gateway_loss_interrupts_actual_audio_without_reconnect(self):
        process=self.start('hold_stream');ready=self.ready(process)
        self.assertTrue(base64.b64decode(ready['partial']['delta']))
        self.front.process.kill();self.front.process.wait(5)
        value=self.finish(process);self.check(value,'hold_stream')
        self.assertEqual(value['close_code'],1006);self.assertEqual(value['connections'],1)
        self.assertEqual(self.commands.count('response.create'),2)

    def test_explicit_plain_connection_recovers_after_whole_transport_loss(self):
        old=self.front;old.close()
        self.front=NativeIngress(self.out/'plain',Path(os.environ['Q4T_INGRESS']),old.port,
            self.server.server_port,free_port(),'e'*64,{'alpha':'A'*40},None,lambda:True)
        process=self.start('hold_complete',plain=True);self.ready(process)
        self.front.process.kill();self.front.process.wait(5)
        self.check(self.finish(process),'hold_complete','ws-loopback')
        old=self.front;old.close()
        self.front=NativeIngress(self.out/'recovered',Path(os.environ['Q4T_INGRESS']),old.port,
            self.server.server_port,free_port(),'e'*64,{'alpha':'A'*40},None,lambda:True)
        self.check(self.finish(self.start('complete',plain=True)),'complete','ws-loopback')
        self.assertEqual(self.commands.count('response.create'),2)

    def test_wrong_certificate_cannot_fall_back_to_plaintext(self):
        value=self.finish(self.start('complete',wrong_ca=True),False)
        self.assertEqual(value['connections'],0);self.assertEqual(self.commands,[])

    def test_actual_mismatched_retrieval_cannot_pass_offline_verification(self):
        self.corrupt_retrieval=True;value=self.finish(self.start('complete'))
        with self.assertRaisesRegex(RuntimeError,'retrieved waveform differs'):self.check(value,'complete')


if __name__=='__main__':unittest.main()
