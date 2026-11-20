"""Pinned SDK/native TLS CPU peer tests; no generated-model claims."""
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import threading
import unittest

from tools.serve_realtime import mcp_managed as m
from tools.serve_realtime import test_conversation_recovery_sdk as prior
from tools.serve_realtime.test_mcp_service import fixture
from tools.serve_realtime.test_transcription_sdk import frame, read_frame, free_port
from tools.serve_realtime.ingress_fixture import NativeIngress, certificate

ROOT = Path(__file__).resolve().parents[2]


class McpSdk(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root=Path(tempfile.mkdtemp(prefix='mcp-sdk-',dir=os.environ['Q4T_TEST_OUTPUT_DIR']))
        cls.tls=certificate(cls.root/'credentials')
        cls.untrusted=certificate(cls.root/'untrusted')

    def setUp(self):
        self.out=Path(tempfile.mkdtemp(prefix='case-',dir=self.root))
        self.commands=[];self.errors=[];self.dispatched=0;self.sequence=0
        self.mode='complete';self.leak=False;self.bad_arguments=False
        owner=self
        class Peer(BaseHTTPRequestHandler):
            protocol_version='HTTP/1.1'
            def log_message(self,*_):pass
            def do_GET(self):
                self.close_connection=True;self.connection.settimeout(5)
                accept=base64.b64encode(hashlib.sha1((self.headers['Sec-WebSocket-Key']+
                    '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
                self.wfile.write(('HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\n'
                    'Upgrade: websocket\r\nSec-WebSocket-Accept: '+accept+'\r\n\r\n').encode())
                number=0;response_index=0
                value,_,_=fixture(owner.mode)
                raw=json.dumps(value)
                ids=[r['id'] for r in value['responses']]
                ids += [i['id'] for r in value['responses'] for i in r['output']]
                if value['approval']:ids.append(value['approval']['id'])
                for identifier in ids:raw=raw.replace(identifier,'connection'+str(owner.sequence)+'_'+identifier)
                value=json.loads(raw)
                def emit(event):
                    nonlocal number
                    number+=1;event=dict(event,event_id='wire-'+str(number))
                    raw=json.dumps(event).encode()
                    wire=frame(raw) if len(raw)<65536 else bytes([0x81,127])+struct.pack('!Q',len(raw))+raw
                    self.wfile.write(wire);self.wfile.flush()
                emit(dict(type='session.created'));emit(dict(type='conversation.created'))
                try:
                    while True:
                        row=read_frame(self.rfile)
                        if row is None:return
                        opcode,payload=row
                        if opcode==8:
                            self.wfile.write(frame(payload,8));self.wfile.flush();return
                        event=json.loads(payload);owner.commands.append(event['type'])
                        if event['type']=='session.update':
                            definition=event['session']['tools'][0]
                            assert definition['authorization']=='B'*40
                            clean=dict(definition);clean.pop('authorization')
                            if owner.leak:clean['unexpected']='B'*40
                            emit(dict(type='session.updated',session=dict(type='realtime',tools=[clean])))
                            emit(dict(type='mcp_list_tools.failed' if owner.mode in
                                ('policy','hold_policy') else 'mcp_list_tools.completed'))
                        elif event['type']=='conversation.item.create':
                            item=event['item']
                            emit(dict(type='conversation.item.done',item=item))
                            if item['type']=='mcp_approval_response':
                                assert item['approve'] is True
                                assert item['approval_request_id']==value['approval']['id']
                                owner.dispatched+=1
                                for message in value['events']:
                                    if message['type'] in ('response.mcp_call.in_progress','response.output_item.done') or (
                                        message['type']=='conversation.item.done' and message.get('item') in value['tools']):
                                        emit(message)
                        elif event['type']=='response.create':
                            response=value['responses'][response_index];response_index+=1
                            call=next((i for i in response['output'] if i['type']=='mcp_call'),None)
                            if call and owner.bad_arguments:call['arguments']='{}'
                            for message in value['events']:
                                if (message.get('response_id')==response['id'] and message['type'] in
                                    ('response.output_text.delta','response.mcp_call_arguments.delta')) or (
                                    message['type'] in ('response.created','response.done') and
                                    message['response']['id']==response['id']):
                                    if message['type']=='response.done':
                                        message=dict(message,response=response)
                                    emit(message)
                            if call:emit(dict(type='conversation.item.done',item=value['approval']))
                        else:raise ValueError('unplanned CPU event')
                except (BrokenPipeError,ConnectionResetError):pass
                except BaseException as error:owner.errors.append(repr(error))
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Peer);self.server.daemon_threads=True
        self.thread=threading.Thread(target=self.server.serve_forever);self.thread.start()
        self.front=None;self.addCleanup(self.close)
        self.replace_front('ingress')

    close=prior.RecoverySdk.close
    ready=prior.RecoverySdk.ready
    finish=prior.RecoverySdk.finish

    def replace_front(self,name,plain=False):
        if self.front:self.front.close()
        self.front=NativeIngress(self.out/name,Path(os.environ['Q4T_INGRESS']),free_port(),
            self.server.server_port,free_port(),'e'*64,{'alpha':'A'*40},
            None if plain else self.tls,lambda:True)

    def start(self,mode,*,plain=False,history=None,wrong_ca=False):
        self.mode=mode;self.sequence+=1;self.history=history or []
        self.journal=self.out/(str(self.sequence)+'.events.jsonl')
        args=dict(base_url=f"{'http' if plain else 'https'}://127.0.0.1:{self.front.port}/v1",
            api_key='A'*40,ca_file=None if plain else
            (self.untrusted if wrong_ca else self.tls)['certificate'],mode=mode,
            history=self.history,mcp_url='https://localhost:1234/mcp',mcp_token='B'*40,
            arguments=m.mcp_checks.ARGUMENTS,journal_file=str(self.journal))
        env={k:v for k,v in os.environ.items() if k not in ('NODE_OPTIONS','NODE_PATH','LD_PRELOAD')}
        process=subprocess.Popen(['node',str(ROOT/'tools/serve_realtime/mcp_recovery_sdk.mjs')],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env)
        def cleanup():
            if process.poll() is None:process.kill()
            process.communicate(timeout=3)
        self.addCleanup(cleanup)
        process.stdin.write(json.dumps(args).encode());process.stdin.close();process.stdin=None
        return process

    def check(self,value,mode,plain=False):
        rows=[json.loads(line) for line in self.journal.read_text().splitlines()]
        observed=m.c.check_session(value,mode,'ws-loopback' if plain else
            'official-openai-wss',rows,fixture()[2],self.history)
        self.assertNotIn('B'*40,self.journal.read_text())
        return observed

    def test_complete_official_sdk_and_redacted_journal(self):
        value=self.finish(self.start('complete'))
        self.check(value,'complete');self.assertEqual(self.dispatched,1)

    def test_gateway_loss_while_approval_pending_never_dispatches(self):
        process=self.start('hold_approval');self.ready(process)
        self.assertEqual(self.dispatched,0)
        self.front.process.kill();self.front.process.wait(5)
        value=self.finish(process);self.check(value,'hold_approval')
        self.assertEqual(self.dispatched,0);self.assertEqual(value['connections'],1)

    def test_explicit_reconnection_authenticates_completed_history_without_replay(self):
        self.replace_front('plain',True)
        process=self.start('hold_complete',plain=True);self.ready(process)
        self.front.process.kill();self.front.process.wait(5)
        value=self.finish(process);self.check(value,'hold_complete',True)
        self.replace_front('recovered',True)
        after=self.finish(self.start('complete',plain=True,history=value['tools']))
        observed=self.check(after,'complete',True)
        self.assertEqual(observed['history_authenticated'],1);self.assertEqual(self.dispatched,2)

    def test_default_private_route_failure_preserves_ordinary_work(self):
        value=self.finish(self.start('policy'))
        observed=self.check(value,'policy');self.assertEqual(observed['model_quality_status'],'PASS')
        self.assertEqual(self.dispatched,0)

    def test_default_policy_owned_connection_closes_on_payload_loss(self):
        self.replace_front('plain',True)
        process=self.start('hold_policy',plain=True);self.ready(process)
        self.front.process.kill();self.front.process.wait(5)
        self.check(self.finish(process),'hold_policy',True)
        self.assertEqual(self.dispatched,0)

    def test_wrong_certificate_cannot_fall_back_to_plaintext(self):
        value=self.finish(self.start('complete',wrong_ca=True),False)
        self.assertEqual(value['connections'],0);self.assertEqual(self.commands,[])

    def test_server_credential_leak_is_not_written_to_public_journal(self):
        self.leak=True
        value=self.finish(self.start('complete'),False)
        self.assertIn('credential',value['error'])
        self.assertNotIn('B'*40,self.journal.read_text()+json.dumps(value))

    def test_invalid_generated_arguments_cannot_be_approved(self):
        self.bad_arguments=True
        value=self.finish(self.start('complete'),False)
        self.assertIn('actual generated arguments',value['error']);self.assertEqual(self.dispatched,0)


if __name__=='__main__':unittest.main()
