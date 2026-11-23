"""Actual pinned WSS SDK/native TLS with a labeled CPU protocol peer."""
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest

from tools.serve_realtime.history_acceptance import settings
from tools.serve_realtime.ingress_fixture import NativeIngress, certificate
from tools.serve_realtime.test_transcription_sdk import frame, read_frame, free_port

ROOT = Path(__file__).resolve().parents[2]


class HistorySdk(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix='history-sdk-',dir=os.environ['Q4T_TEST_OUTPUT_DIR']))
        cls.tls = certificate(cls.root/'credentials')

    def setUp(self):
        self.out = Path(tempfile.mkdtemp(prefix='case-',dir=self.root))
        self.audio = self.out/'source.pcm';self.audio.write_bytes(bytes(range(256))*128)
        self.wrong_deleted = self.expect_abort = False
        self.errors,self.received,self.commands = [],[],[]
        owner = self
        class Peer(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def log_message(self,*args): pass
            def do_GET(self):
                self.close_connection = True
                self.connection.settimeout(5)
                accept = base64.b64encode(hashlib.sha1((self.headers['Sec-WebSocket-Key']+
                    '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
                self.wfile.write(('HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\n'
                    'Upgrade: websocket\r\nSec-WebSocket-Accept: '+accept+'\r\n\r\n').encode())
                number=0;sequence=1;pending=bytearray();records={};continuous=False;partial=False
                def emit(kind, **fields):
                    nonlocal number
                    number += 1
                    self.wfile.write(frame(json.dumps(dict(type=kind,event_id='e'+str(number),**fields)).encode()))
                    self.wfile.flush()
                emit('session.created',session=dict(id='cpu-session',type='transcription'))
                try:
                    while True:
                        row = read_frame(self.rfile)
                        if row is None:return
                        opcode, payload = row
                        if opcode == 8:
                            self.wfile.write(frame(payload,8));self.wfile.flush();return
                        value = json.loads(payload);owner.commands.append(value['type'])
                        kind = value['type'];item = 'cpu-item-'+str(sequence)
                        if kind == 'session.update':
                            session=value['session'];cfg=session['audio']['input']
                            cfg['transcription']={**dict(language='',prompt='',languages=[],keywords=[]),**cfg['transcription']}
                            continuous='delay' in cfg['transcription']
                            emit('session.updated',session=session)
                        elif kind == 'input_audio_buffer.append':
                            pending.extend(base64.b64decode(value['audio'],validate=True))
                            if continuous and not partial and len(pending)>=24000:
                                partial=True
                                emit('conversation.item.input_audio_transcription.delta',item_id=item,
                                     content_index=0,delta='CPU fixture')
                        elif kind == 'input_audio_buffer.commit':
                            owner.received.append(bytes(pending))
                            records[item]=dict(id=item,object='realtime.item',type='message',role='user',
                                status='completed',content=[dict(type='input_audio',transcript='CPU fixture only',
                                    audio=base64.b64encode(pending).decode())])
                            predecessor=list(records)[-2] if len(records)>1 else None
                            emit('input_audio_buffer.committed',item_id=item,previous_item_id=predecessor)
                            emit('conversation.item.input_audio_transcription.delta',item_id=item,
                                 content_index=0,delta=' only' if partial else 'CPU fixture only')
                            emit('conversation.item.input_audio_transcription.completed',item_id=item,
                                 content_index=0,transcript='CPU fixture only',
                                 usage=dict(type='tokens',input_tokens=12,output_tokens=4,total_tokens=16))
                            sequence+=1;pending.clear();partial=False
                        elif kind == 'conversation.item.retrieve':
                            if value['item_id'] in records:
                                emit('conversation.item.retrieved',item=records[value['item_id']])
                            else:
                                emit('error',error=dict(code='item_not_found',type='invalid_request_error',
                                     message='CPU absent',event_id=value['event_id']))
                        elif kind == 'conversation.item.delete':
                            del records[value['item_id']]
                            emit('conversation.item.deleted',item_id='wrong-item' if owner.wrong_deleted else value['item_id'])
                        else:raise ValueError('unplanned CPU event')
                except (BrokenPipeError,ConnectionResetError):
                    if not owner.expect_abort:owner.errors.append('unexpected socket abort')
                except BaseException as error:owner.errors.append(repr(error))
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Peer);self.server.daemon_threads=True
        self.thread=threading.Thread(target=self.server.serve_forever);self.thread.start()
        self.front=None;self.addCleanup(self.close)
        self.front=NativeIngress(self.out/'ingress',Path(os.environ['Q4T_INGRESS']),free_port(),
            self.server.server_port,None,'e'*64,{'alpha':'A'*40},self.tls,lambda:True)

    def close(self):
        if self.front:self.front.close()
        self.server.shutdown();self.server.server_close();self.thread.join(2)
        self.assertFalse(self.thread.is_alive());self.assertEqual(self.errors,[])

    def call(self,mode):
        args=dict(base_url=f'https://127.0.0.1:{self.front.port}/v1',api_key='A'*40,
            ca_file=self.tls['certificate'],file=str(self.audio),mode=mode,
            settings=settings(mode),foreign_id='foreign-item')
        env={k:v for k,v in os.environ.items() if k not in ('NODE_OPTIONS','NODE_PATH','LD_PRELOAD')}
        run=subprocess.run(['node',str(ROOT/'tools/serve_realtime/history_sdk.mjs')],
            input=json.dumps(args),text=True,capture_output=True,timeout=15,env=env)
        (self.out/'sdk.json').write_text(run.stdout);(self.out/'sdk.stderr').write_text(run.stderr)
        return run.returncode,json.loads(run.stdout)

    def test_manual_history_delete_and_same_connection_with_actual_sdk(self):
        code,result=self.call('manual');self.assertEqual(code,0,result)
        self.assertEqual(self.received,[self.audio.read_bytes()]*3)
        self.assertEqual(len(result['turns']),3);self.assertEqual(result['connections'],1)
        self.assertEqual(len(result['checks']),4)
        self.assertIsNone(result['turns'][-1]['committed']['previous_item_id'])

    def test_continuous_pending_is_hidden_until_actual_commit(self):
        code,result=self.call('near_stream');self.assertEqual(code,0,result)
        self.assertEqual(self.received,[self.audio.read_bytes()]);self.assertEqual(len(result['checks']),2)
        self.assertEqual(result['turns'][0]['before_commit']['sent_bytes'],24000)
        self.assertEqual(result['turns'][0]['delta'],'CPU fixture only')

    def test_wrong_deleted_identity_is_not_certified(self):
        self.wrong_deleted=self.expect_abort=True
        code,result=self.call('manual');self.assertNotEqual(code,0)
        self.assertEqual(result['error'],'deleted identity');self.assertEqual(len(self.received),2)


if __name__ == '__main__':unittest.main()
