"""Pinned SDK and native TLS with an explicit CPU protocol peer, not an LLM."""
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import select
import subprocess
import tempfile
import threading
import unittest

from tools.serve_realtime.ingress_fixture import NativeIngress, certificate
from tools.serve_realtime.test_transcription_sdk import frame, read_frame, free_port

ROOT = Path(__file__).resolve().parents[2]


class RecoverySdk(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root=Path(tempfile.mkdtemp(prefix='conversation-sdk-',dir=os.environ['Q4T_TEST_OUTPUT_DIR']))
        cls.tls=certificate(cls.root/'credentials')
        cls.untrusted=certificate(cls.root/'untrusted')

    def setUp(self):
        self.out=Path(tempfile.mkdtemp(prefix='case-',dir=self.root))
        self.record=self.out/'record.txt';self.record.write_text('CPU fixture: 默认 rev346\n')
        self.commands=[];self.errors=[];self.outputs=[];owner=self
        class Peer(BaseHTTPRequestHandler):
            protocol_version='HTTP/1.1'
            def log_message(self,*_):pass
            def do_GET(self):
                self.close_connection=True;self.connection.settimeout(5)
                accept=base64.b64encode(hashlib.sha1((self.headers['Sec-WebSocket-Key']+
                    '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
                self.wfile.write(('HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\n'
                    'Upgrade: websocket\r\nSec-WebSocket-Accept: '+accept+'\r\n\r\n').encode())
                number=0;turn=0
                def emit(kind,**fields):
                    nonlocal number
                    number+=1;self.wfile.write(frame(json.dumps(dict(type=kind,event_id='cpu-'+str(number),**fields)).encode()));self.wfile.flush()
                emit('session.created',session=dict(type='realtime',model='qwen3.8-flash-next'))
                emit('conversation.created',conversation=dict(id='cpu-conversation'))
                try:
                    while True:
                        row=read_frame(self.rfile)
                        if row is None:return
                        opcode,payload=row
                        if opcode==8:
                            self.wfile.write(frame(payload,8));self.wfile.flush();return
                        event=json.loads(payload);owner.commands.append(event['type'])
                        if event['type']=='session.update':emit('session.updated',session=event['session'])
                        elif event['type']=='conversation.item.create':
                            item=event['item']
                            if item['type']=='function_call_output':owner.outputs.append(json.loads(item['output']))
                            emit('conversation.item.done',item=item)
                        elif event['type']=='response.create':
                            turn+=1;identifier='cpu-response-'+str(turn);item_id='cpu-item-'+str(turn)
                            emit('response.created',response=dict(id=identifier,conversation_id=None if turn==3 else 'cpu-conversation'))
                            if turn==1:
                                item=dict(id=item_id,type='function_call',status='completed',call_id='cpu-call',
                                          name='lookup_project_record',arguments='{"record_key":"release"}')
                                emit('response.function_call_arguments.delta',response_id=identifier,item_id=item_id,delta=item['arguments'])
                            else:
                                value='rev346' if turn==2 else '1'
                                item=dict(id=item_id,type='message',role='assistant',status='completed',
                                          content=[dict(type='output_text',text=value)])
                                emit('response.output_text.delta',response_id=identifier,item_id=item_id,delta=value)
                            if turn!=3:
                                emit('response.done',response=dict(id=identifier,status='completed',
                                    output=[item],conversation_id='cpu-conversation',output_modalities=['text'],
                                    usage=dict(input_tokens=100,output_tokens=10,total_tokens=110)))
                        else:raise ValueError('unplanned CPU event')
                except (BrokenPipeError,ConnectionResetError):pass
                except BaseException as error:owner.errors.append(repr(error))
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Peer);self.server.daemon_threads=True
        self.thread=threading.Thread(target=self.server.serve_forever);self.thread.start()
        self.front=None;self.addCleanup(self.close)
        self.front=NativeIngress(self.out/'ingress',Path(os.environ['Q4T_INGRESS']),free_port(),
            self.server.server_port,free_port(),'e'*64,{'alpha':'A'*40},self.tls,lambda:True)

    def close(self):
        if self.front:self.front.close()
        self.server.shutdown();self.server.server_close();self.thread.join(2)
        self.assertFalse(self.thread.is_alive());self.assertEqual(self.errors,[])

    def start(self,mode,*,plain=False,wrong_ca=False):
        args=dict(base_url=f"{'http' if plain else 'https'}://127.0.0.1:{self.front.port}/v1",
            api_key='A'*40,ca_file=None if plain else
                (self.untrusted if wrong_ca else self.tls)['certificate'],mode=mode,
            record_file=str(self.record),record_sha256=hashlib.sha256(self.record.read_bytes()).hexdigest(),
            expected_version='rev346')
        env={k:v for k,v in os.environ.items() if k not in ('NODE_OPTIONS','NODE_PATH','LD_PRELOAD')}
        process=subprocess.Popen(['node',str(ROOT/'tools/serve_realtime/conversation_recovery_sdk.mjs')],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env)
        def cleanup():
            if process.poll() is None:process.kill()
            process.communicate(timeout=3)
        self.addCleanup(cleanup)
        process.stdin.write(json.dumps(args).encode());process.stdin.close();process.stdin=None
        return process

    def finish(self,process,success=True):
        stdout,stderr=process.communicate(timeout=10)
        (self.out/('stdout-'+str(process.pid)+'.json')).write_bytes(stdout)
        (self.out/('stderr-'+str(process.pid)+'.log')).write_bytes(stderr)
        value=json.loads(stdout);self.assertEqual(process.returncode==0,success,stderr.decode()+stdout.decode())
        self.assertEqual(value['status'],'PASS' if success else 'FAIL')
        self.assertFalse(value['automatic_replay'])
        return value

    def ready(self,process):
        self.assertTrue(select.select([process.stdout],[],[],10)[0], 'no bounded ready receipt')
        value=json.loads(process.stdout.readline());self.assertEqual(value['phase'],'ready',value)
        (self.out/'ready.json').write_text(json.dumps(value))
        return value

    def test_complete_official_tls_tools_read_actual_fixture_and_close(self):
        value=self.finish(self.start('complete'))
        self.assertEqual(value['connections'],1);self.assertEqual(value['close_code'],1000)
        self.assertEqual(value['transport'],'official-openai-wss')
        self.assertEqual(len(self.outputs),1);self.assertEqual(value['tool_record'],self.outputs[0])
        self.assertEqual(self.outputs[0]['source_sha256'],hashlib.sha256(self.record.read_bytes()).hexdigest())
        self.assertEqual(self.commands.count('response.create'),2)

    def test_gateway_loss_interrupts_actual_pending_text_without_reconnect(self):
        process=self.start('hold_stream');ready=self.ready(process)
        self.assertEqual(ready['partial']['delta'],'1');self.assertEqual(ready['response_creates'],3)
        self.front.process.kill();self.front.process.wait(5)
        value=self.finish(process);self.assertEqual(value['close_code'],1006)
        self.assertEqual(value['connections'],1);self.assertEqual(len(value['responses']),2)
        self.assertEqual(self.commands.count('response.create'),3);self.assertEqual(len(self.outputs),1)

    def test_plaintext_completed_tool_needs_explicit_new_connection_after_loss(self):
        old=self.front;old.close()
        self.front=NativeIngress(self.out/'plain',Path(os.environ['Q4T_INGRESS']),old.port,
            self.server.server_port,free_port(),'e'*64,{'alpha':'A'*40},None,lambda:True)
        process=self.start('hold_complete',plain=True);self.ready(process)
        self.front.process.kill();self.front.process.wait(5)
        value=self.finish(process);self.assertEqual(value['transport'],'ws-loopback')
        self.assertEqual(value['close_code'],1006);self.assertEqual(self.commands.count('response.create'),2)
        old=self.front;old.close()
        self.front=NativeIngress(self.out/'recovered',Path(os.environ['Q4T_INGRESS']),old.port,
            self.server.server_port,free_port(),'e'*64,{'alpha':'A'*40},None,lambda:True)
        after=self.finish(self.start('complete',plain=True));self.assertEqual(after['close_code'],1000)
        self.assertEqual(self.commands.count('response.create'),4);self.assertEqual(len(self.outputs),2)

    def test_wrong_certificate_cannot_silently_fall_back_to_plaintext(self):
        value=self.finish(self.start('complete',wrong_ca=True),False)
        self.assertEqual(value['connections'],0);self.assertEqual(self.commands,[])
        self.assertEqual(value['transport'],'official-openai-wss')


if __name__ == '__main__':unittest.main()
