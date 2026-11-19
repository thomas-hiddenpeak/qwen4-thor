"""Pinned official SDK/native TLS against an explicit CPU semantic peer.

These cases validate the acceptance client, not speech or semantic model quality.
"""
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

from tools.serve_realtime.ingress_fixture import NativeIngress, certificate
from tools.serve_realtime.test_transcription_sdk import free_port, frame, read_frame

ROOT = Path(__file__).resolve().parents[2]


class SemanticSdk(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.output = Path(os.environ['Q4T_TEST_OUTPUT_DIR']).resolve()
        cls.output.mkdir(parents=True, exist_ok=True)
        cls.root = Path(tempfile.mkdtemp(prefix='semantic-sdk-', dir=cls.output))
        cls.tls = certificate(cls.root/'credentials')
        cls.ingress = Path(os.environ['Q4T_INGRESS']).resolve()

    def call(self, mode='complete'):
        directory = Path(tempfile.mkdtemp(prefix=mode+'-', dir=self.root))
        clip = directory/'fixture.pcm'
        clip.write_bytes(struct.pack('<h', 1000) * 14400)
        split = 4800 if mode in ('continuation', 'premature') else 0
        rows, errors = [], []
        expected = (12000 + 14400 + (12000 if split else 0) + 72000) * 2
        end_speech = (12000 + 14400 + (12000 if split else 0)) * 2
        class Peer(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def log_message(self, *_):
                pass
            def do_GET(self):
                rows.append(dict(path=self.path, client=self.headers.get('X-Q4T-Client-Id')))
                self.close_connection = True
                self.connection.settimeout(5)
                accept = base64.b64encode(hashlib.sha1((
                    self.headers['Sec-WebSocket-Key'] +
                    '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
                self.wfile.write(('HTTP/1.1 101 Switching Protocols\r\n'
                    'Connection: Upgrade\r\nUpgrade: websocket\r\n'
                    'Sec-WebSocket-Accept: ' + accept + '\r\n\r\n').encode())
                count = received = 0
                started = completed = False
                def emit(kind, **fields):
                    nonlocal count
                    count += 1
                    self.wfile.write(frame(json.dumps(dict(type=kind,
                        event_id='cpu-'+str(count), **fields)).encode()))
                    self.wfile.flush()
                emit('session.created', session=dict(id='cpu', type='transcription'))
                try:
                    while True:
                        row = read_frame(self.rfile)
                        if row is None:
                            return
                        opcode, payload = row
                        if opcode == 8:
                            self.wfile.write(frame(payload, 8)); self.wfile.flush()
                            return
                        if opcode != 1:
                            raise ValueError('fixture opcode')
                        event = json.loads(payload)
                        if event['type'] == 'session.update':
                            emit('session.updated', session=event['session'])
                        elif event['type'] == 'input_audio_buffer.append':
                            received += len(base64.b64decode(event['audio'], validate=True))
                            if received > 24000 and not started:
                                started = True
                                emit('input_audio_buffer.speech_started', item_id='cpu-item')
                            limit = (12000+4800)*2 if mode == 'premature' else end_speech+14400
                            if received >= limit and not completed:
                                completed = True
                                emit('input_audio_buffer.speech_stopped', item_id='cpu-item')
                                emit('input_audio_buffer.committed', item_id='cpu-item')
                                emit('conversation.item.input_audio_transcription.delta',
                                     item_id='cpu-item', delta='CPU fixture only')
                                if mode == 'late_error':
                                    emit('error', error=dict(code='fixture_failure',
                                         type='server_error', message='CPU error'))
                                emit('conversation.item.input_audio_transcription.completed',
                                     item_id='cpu-item', transcript='CPU fixture only',
                                     q4t_semantic_vad=dict(profile='q4t-native-semantic-turn-v1',
                                         accepted=0 if mode == 'missing_score' else 1,
                                         input_tokens=90, output_tokens=7, included_in_usage=True))
                        elif event['type'] == 'conversation.item.retrieve':
                            if received != expected:
                                raise ValueError('fixture waveform count')
                            emit('conversation.item.retrieved', item=dict(id='cpu-item',
                                content=[dict(transcript='CPU fixture only', audio='AAAA')]))
                        else:
                            raise ValueError('unexpected client event')
                except (BrokenPipeError, ConnectionResetError):
                    if mode in ('complete', 'continuation'):
                        errors.append('unexpected disconnect')
                except Exception as error:
                    errors.append(str(error))
        server = ThreadingHTTPServer(('127.0.0.1', 0), Peer)
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        try:
            ingress = NativeIngress(directory/'ingress', self.ingress, free_port(),
                server.server_port, free_port(), 'e'*64, {'alpha': 'A'*40},
                self.tls, lambda: worker.is_alive())
            try:
                args = dict(base_url=f'https://127.0.0.1:{ingress.port}/v1',
                    api_key='A'*40, ca_file=self.tls['certificate'], file=str(clip),
                    split_samples=split, settings=dict(type='transcription',
                        audio=dict(input=dict(turn_detection=dict(
                            type='semantic_vad', eagerness='high')))))
                result = subprocess.run(['node', str(ROOT/'tools/serve_realtime/semantic_sdk.mjs')],
                    input=json.dumps(args), text=True, capture_output=True,
                    timeout=15, env=os.environ)
                (directory/'stdout.json').write_text(result.stdout)
                (directory/'stderr.log').write_text(result.stderr)
                self.assertTrue(result.stdout, result.stderr)
                value = json.loads(result.stdout)
            finally:
                ingress.close()
        finally:
            server.shutdown(); server.server_close(); worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(rows, [dict(path='/v1/realtime?intent=transcription', client='alpha')])
        self.assertEqual(value['connections'], 1)
        self.assertFalse(value['automatic_replay'])
        return result.returncode, value

    def test_complete_turn_requires_classifier_and_orderly_close(self):
        code, value = self.call()
        self.assertEqual(code, 0, value)
        self.assertEqual(value['status'], 'PASS')
        self.assertEqual(value['close_code'], 1000)

    def test_unfinished_prefix_preserves_one_connection_and_actual_suffix(self):
        code, value = self.call('continuation')
        self.assertEqual(code, 0, value)
        self.assertTrue(value['continuation']['no_commit'])
        self.assertEqual(value['continuation']['original_samples_sent'], 4800)

    def test_premature_commit_rejected_without_replay(self):
        code, value = self.call('premature')
        self.assertNotEqual(code, 0)
        self.assertIn('committed before continuation', value['error'])

    def test_transcript_without_actual_classifier_is_not_acceptance(self):
        code, value = self.call('missing_score')
        self.assertNotEqual(code, 0)
        self.assertIn('real classifier', value['error'])

    def test_error_alongside_completed_event_is_not_hidden(self):
        code, value = self.call('late_error')
        self.assertNotEqual(code, 0)
        self.assertIn('fixture_failure', value['error'])


if __name__ == '__main__':
    unittest.main()
