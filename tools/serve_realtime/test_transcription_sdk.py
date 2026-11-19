"""Actual official WSS client/native TLS ingress with an explicit CPU peer."""
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
import threading
import unittest

from tools.serve_realtime.ingress_fixture import NativeIngress, certificate

ROOT = Path(__file__).resolve().parents[2]


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));return sock.getsockname()[1]


def frame(raw, opcode=1):
    size = len(raw)
    return (bytes([0x80|opcode, size]) if size < 126 else
            bytes([0x80|opcode,126])+struct.pack('!H',size)) + raw


def read_frame(stream):
    head = stream.read(2)
    if not head: return None
    if len(head) != 2 or not head[0]&0x80 or not head[1]&0x80:
        raise ValueError('fixture expects masked complete frames')
    size = head[1]&127
    if size == 126: size = struct.unpack('!H',stream.read(2))[0]
    if size == 127: size = struct.unpack('!Q',stream.read(8))[0]
    if size > 1048576: raise ValueError('fixture frame budget')
    mask, raw = stream.read(4),stream.read(size)
    if len(raw)!=size or len(mask)!=4: raise EOFError('fixture frame')
    return head[0]&15,bytes(b^mask[i%4] for i,b in enumerate(raw))


class SdkTls(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.output = Path(os.environ['Q4T_TEST_OUTPUT_DIR']).resolve()
        cls.output.mkdir(parents=True,exist_ok=True)
        cls.sdk = Path(os.environ['Q4T_NODE_SDK_DIR']).resolve()
        cls.ingress = Path(os.environ['Q4T_INGRESS']).resolve()
        cls.root = Path(tempfile.mkdtemp(prefix='sdk-tls-',dir=cls.output))
        cls.tls = certificate(cls.root/'credentials')
        cls.untrusted = certificate(cls.root/'untrusted')

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix='case-',dir=self.root))
        self.rows,self.audio,self.errors = [],[],[]
        owner = self
        class Peer(BaseHTTPRequestHandler):
            protocol_version='HTTP/1.1'
            def log_message(self,*_args): pass
            def do_GET(self):
                owner.rows.append(dict(path=self.path,client=self.headers.get('X-Q4T-Client-Id'),
                    token=self.headers.get('X-Q4T-Gateway-Token'),
                    authorization=self.headers.get('Authorization')))
                self.close_connection=True
                self.connection.settimeout(3)
                accept=base64.b64encode(hashlib.sha1((self.headers['Sec-WebSocket-Key']+
                    '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
                self.wfile.write(('HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\n'
                    'Upgrade: websocket\r\nSec-WebSocket-Accept: '+accept+'\r\n\r\n').encode())
                number=0; audio=bytearray()
                def emit(kind,**fields):
                    nonlocal number
                    number+=1
                    self.wfile.write(frame(json.dumps(dict(type=kind,event_id='e'+str(number),**fields)).encode()))
                    self.wfile.flush()
                emit('session.created',session=dict(id='cpu-session',type='transcription'))
                try:
                    while True:
                        row=read_frame(self.rfile)
                        if row is None:return
                        opcode,payload=row
                        if opcode==8:
                            self.wfile.write(frame(payload,8));self.wfile.flush();return
                        if opcode!=1:raise ValueError('fixture opcode')
                        event=json.loads(payload);kind=event['type']
                        if kind=='session.update':emit('session.updated',session=event['session'])
                        elif kind=='input_audio_buffer.append':
                            raw=base64.b64decode(event['audio'],validate=True)
                            if base64.b64encode(raw).decode()!=event['audio']:
                                emit('error',error=dict(code='bad_audio',message='CPU invalid base64',
                                    type='invalid_request_error',event_id=event.get('event_id')))
                            else:audio.extend(raw)
                        elif kind=='input_audio_buffer.clear':
                            audio.clear();emit('input_audio_buffer.cleared')
                        elif kind=='input_audio_buffer.commit':
                            if not audio:
                                emit('error',error=dict(code='empty',message='CPU empty audio',
                                    type='invalid_request_error',event_id=event.get('event_id')))
                                continue
                            owner.audio.append(bytes(audio));audio.clear()
                            emit('input_audio_buffer.committed',item_id='cpu-item',previous_item_id=None)
                            emit('conversation.item.input_audio_transcription.delta',item_id='cpu-item',
                                 content_index=0,delta='CPU fixture only')
                            emit('conversation.item.input_audio_transcription.completed',item_id='cpu-item',
                                 content_index=0,transcript='CPU fixture only',
                                 usage=dict(type='tokens',input_tokens=12,output_tokens=4,total_tokens=16))
                        else:raise ValueError('fixture event')
                except BaseException as error:owner.errors.append(type(error).__name__)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Peer)
        self.server.daemon_threads=True
        self.thread=threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.front=None
        self.addCleanup(self.close)
        self.front=NativeIngress(self.directory/'ingress',self.ingress,free_port(),
            self.server.server_port,free_port(),'e'*64,{'alpha':'A'*40},
            self.tls,lambda:True)
        self.clip=self.directory/'source.pcm';self.clip.write_bytes(bytes(range(256))*128)

    def close(self):
        if self.front is not None:self.front.close()
        self.server.shutdown();self.server.server_close();self.thread.join(2)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(self.errors,[])

    def call(self,**changes):
        args=dict(base_url=f'https://127.0.0.1:{self.front.port}/v1',api_key='A'*40,
            ca_file=self.tls['certificate'],clips=[dict(file=str(self.clip))],probes=True)
        args.update(changes)
        env={k:v for k,v in os.environ.items() if k not in ('NODE_OPTIONS','NODE_PATH','LD_PRELOAD')}
        run=subprocess.run(['node',str(ROOT/'tools/serve_realtime/transcription_sdk.mjs')],
            input=json.dumps(args),text=True,capture_output=True,timeout=15,env=env)
        (self.directory/'sdk.json').write_text(run.stdout)
        (self.directory/'sdk.stderr').write_text(run.stderr)
        return run.returncode,json.loads(run.stdout)

    def test_actual_sdk_wss_native_auth_errors_audio_and_close(self):
        code,result=self.call()
        self.assertEqual(code,0,result)
        self.assertEqual(result['status'],'PASS');self.assertEqual(result['connections'],1)
        self.assertEqual(result['close_code'],1000);self.assertFalse(result['automatic_replay'])
        self.assertEqual(self.audio,[self.clip.read_bytes()])
        self.assertEqual(len(self.rows),1)
        self.assertEqual(self.rows[0],dict(path='/v1/realtime?intent=transcription',client='alpha',
            token='e'*64,authorization=None))
        self.assertEqual(len(result['errors']),2)

    def test_untrusted_tls_never_reaches_backend(self):
        code,result=self.call(ca_file=self.untrusted['certificate'])
        self.assertNotEqual(code,0);self.assertEqual(result['connections'],0)
        self.assertEqual(self.rows,[])

    def test_invalid_api_key_never_reaches_backend(self):
        code,result=self.call(api_key='B'*40)
        self.assertNotEqual(code,0);self.assertEqual(result['connections'],0)
        self.assertEqual(self.rows,[])


if __name__=='__main__':unittest.main()
