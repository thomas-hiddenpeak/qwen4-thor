"""Actual fixed SDK and native ingress against a CPU peer, never a model oracle."""
import base64
import copy
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

from tools.serve_realtime import duplex_managed as m
from tools.serve_realtime import test_conversation_recovery_sdk as prior
from tools.serve_realtime.test_duplex_service import RAW,fixture,patch_input
from tools.serve_realtime.test_transcription_sdk import frame,read_frame,free_port
from tools.serve_realtime.ingress_fixture import NativeIngress,certificate

ROOT=Path(__file__).resolve().parents[2]


class DuplexSdk(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root=Path(tempfile.mkdtemp(prefix='duplex-sdk-',dir=os.environ['Q4T_TEST_OUTPUT_DIR']))
        cls.tls=certificate(cls.root/'credentials')

    def setUp(self):
        patch_input(self)
        self.out=Path(tempfile.mkdtemp(prefix='case-',dir=self.root))
        self.clip=self.out/'cpu.pcm';self.clip.write_bytes(RAW)
        self.commands=[];self.errors=[];owner=self
        class Peer(BaseHTTPRequestHandler):
            protocol_version='HTTP/1.1'
            def log_message(self,*_):pass
            def do_GET(self):
                self.close_connection=True;self.connection.settimeout(5)
                accept=base64.b64encode(hashlib.sha1((self.headers['Sec-WebSocket-Key']+
                    '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
                self.wfile.write(('HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\n'
                    'Upgrade: websocket\r\nSec-WebSocket-Accept: '+accept+'\r\n\r\n').encode())
                number=0;settings={};audio=bytearray();partial=False;data,_=fixture()
                def emit(event):
                    nonlocal number
                    number+=1;event=dict(event,event_id='cpu-'+str(number))
                    raw=json.dumps(event).encode()
                    wire=frame(raw) if len(raw)<65536 else bytes([0x81,127])+struct.pack('!Q',len(raw))+raw
                    self.wfile.write(wire);self.wfile.flush()
                emit(dict(type='session.created'))
                emit(dict(type='conversation.created',conversation=dict(id='cpu-conversation')))
                try:
                    while True:
                        row=read_frame(self.rfile)
                        if row is None:return
                        opcode,payload=row
                        if opcode==8:
                            self.wfile.write(frame(payload,8));self.wfile.flush();return
                        event=json.loads(payload);owner.commands.append(event['type'])
                        if event['type']=='session.update':
                            if settings:
                                assert event['session']==dict(type='realtime',audio=dict(input=dict(
                                    transcription=dict(model='qwen3-asr-0.6b',delay='low'))))
                                settings['audio']['input']['transcription']['delay']='low'
                            else:settings=copy.deepcopy(data['events'][1]['session'])
                            emit(dict(type='session.updated',session=settings))
                        elif event['type']=='input_audio_buffer.append':
                            audio.extend(base64.b64decode(event['audio'],validate=True))
                            if settings['audio']['input']['transcription'].get('delay')=='low' and len(audio)>=144000 and not partial:
                                partial=True;emit(dict(type='conversation.item.input_audio_transcription.delta',
                                                      item_id='partial',content_index=0,delta='甚至'))
                        elif event['type']=='input_audio_buffer.commit':
                            assert bytes(audio)==RAW;audio.clear()
                            emit(data['events'][2]);emit(data['events'][3])
                        elif event['type']=='conversation.item.retrieve':emit(data['events'][4])
                        elif event['type']=='response.create':
                            assert event['response']['conversation']=='auto' and 'input' not in event['response']
                            for message in data['events'][5:]:emit(message)
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

    def start(self,mode,plain=False):
        self.journal=self.out/(mode+'-'+str(len(self.commands))+'.events.jsonl')
        args=dict(base_url=f"{'http' if plain else 'https'}://127.0.0.1:{self.front.port}/v1",
            api_key='A'*40,ca_file=None if plain else self.tls['certificate'],mode=mode,
            clip=str(self.clip),clip_sha256=hashlib.sha256(RAW).hexdigest(),journal_file=str(self.journal))
        env={k:v for k,v in os.environ.items() if k not in ('NODE_OPTIONS','NODE_PATH','LD_PRELOAD')}
        process=subprocess.Popen(['node',str(ROOT/'tools/serve_realtime/duplex_recovery_sdk.mjs')],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env)
        def cleanup():
            if process.poll() is None:process.kill()
            process.communicate(timeout=3)
        self.addCleanup(cleanup)
        process.stdin.write(json.dumps(args).encode());process.stdin.close();process.stdin=None
        return process

    def check(self,value,mode,transport='official-openai-wss'):
        rows=[json.loads(line) for line in self.journal.read_text().splitlines()]
        return m.check_session(value,mode,transport,rows)

    def test_actual_official_tls_client_commits_input_and_uses_same_conversation(self):
        value=self.finish(self.start('complete'));self.check(value,'complete')
        self.assertEqual(value['connections'],1)
        self.assertEqual(self.commands.count('input_audio_buffer.commit'),1)
        self.assertEqual(self.commands.count('response.create'),1)

    def test_gateway_kill_keeps_original_partial_asr_journal_without_replay(self):
        process=self.start('hold_stream');ready=self.ready(process)
        self.assertEqual(ready['partial'],dict(item_id='partial',delta='甚至'))
        self.front.process.kill();self.front.process.wait(5)
        value=self.finish(process);self.check(value,'hold_stream')
        self.assertEqual(value['close_code'],1006)
        self.assertEqual(self.commands.count('input_audio_buffer.commit'),1)
        self.assertEqual(self.commands.count('response.create'),1)

    def test_plain_explicit_new_connection_survives_whole_transport_loss(self):
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


if __name__=='__main__':unittest.main()
