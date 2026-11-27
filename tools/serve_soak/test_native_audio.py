"""Test scheduling, real loopback transport and independent round-trip replay."""
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from tools.serve_soak import native_audio as audio, native_contract as contract


class AudioSoak(unittest.TestCase):
    def test_day_includes_warmup_hour_checkpoints_and_final_hour(self):
        config = contract.configuration('day', audio=True)
        self.assertEqual(config['audio_cycles'], [1, *range(60, 1441, 60)])
        self.assertEqual(contract.business_count(config, 60), 304)
        self.assertEqual(contract.checkpoint('day', 60, 3600, audio=True)['business_requests'], 304)
        self.assertEqual(config['limits']['chats'], 7251)
        self.assertEqual(contract.configuration('day')['limits']['chats'], 7201)

    def test_short_plan_rejects_changed_cycles_and_text_only_downgrade(self):
        plan = dict(contract.configuration('short', audio=True), version=2,
                    scenario='native_soak_audio_short', audio_roundtrip={},
                    automatic_request_replay=False, day_run_owner='user')
        self.assertEqual(contract.validate_plan(plan)['limits']['chats'], 20)
        for mutation in [dict(audio_cycles=[1]), dict(scenario='native_soak_short'),
                         dict(version=1), dict(limits=contract.configuration('short')['limits'])]:
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                contract.validate_plan(dict(plan, **mutation))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.audio = b'audio fixture\x00\xff\x01'
        self.reference = dict(speech=dict(input='Read this test.'), bytes=len(self.audio),
                              sha256=audio.digest(self.audio), asr_model='fixture-asr')

    def run_http(self, *, fail=False):
        requests = []
        clip = self.audio
        class Server(BaseHTTPRequestHandler):
            def log_message(self, *unused):
                pass

            def do_POST(self):
                data = self.rfile.read(int(self.headers['Content-Length']))
                requests.append((self.path, self.headers['Authorization']))
                if self.path == '/v1/audio/speech':
                    body, mime = clip, 'audio/mpeg'
                else:
                    envelope = ('Content-Type: '+self.headers['Content-Type']+'\r\n\r\n').encode() + data
                    form = {part.get_param('name', header='content-disposition'): part
                            for part in BytesParser(policy=default).parsebytes(envelope).iter_parts()}
                    if (not fail and form['file'].get_payload(decode=True) == clip and
                            form['file'].get_content_type() == 'audio/mpeg' and
                            form['model'].get_content().strip() == 'fixture-asr' and
                            form['response_format'].get_content().strip() == 'json' and len(form) == 3):
                        text = 'Read this test!'
                    else:
                        text = 'different words'
                    body, mime = json.dumps(dict(text=text)).encode(), 'application/json'
                self.send_response(200)
                self.send_header('Content-Type', mime)
                self.end_headers()
                self.wfile.write(body)
        with HTTPServer(('127.0.0.1', 0), Server) as server:
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            stage = SimpleNamespace(budget=Mock(), output=self.directory, port=server.server_port,
                                    keys=dict(alpha='cpu-key'))
            try:
                audio.exercise(stage, 'cycle-0001', self.reference)
            finally:
                server.shutdown()
                thread.join(5)
        self.assertEqual(requests, [(p, 'Bearer cpu-key') for p in
                                     ('/v1/audio/speech', '/v1/audio/transcriptions')])
        self.assertEqual(stage.budget.reserve.call_count, 2)

    def test_asr_receives_the_complete_speech_and_wire_can_be_reverified(self):
        self.run_http()
        prefix = self.directory/'cycle-0001'
        audio.verify(self.reference, prefix)
        for name in ('speech.mp3', 'transcription-request.bin', 'transcription.json'):
            path = self.directory/('cycle-0001-' + name)
            old = path.read_bytes()
            path.write_bytes(b'{}' if name.endswith('json') else old[:-1])
            with self.subTest(name=name), self.assertRaises((ValueError, KeyError)):
                audio.verify(self.reference, prefix)
            path.write_bytes(old)

    def test_a_different_transcript_fails_with_original_speech_and_reply_retained(self):
        with self.assertRaisesRegex(ValueError, 'spoken sentence'):
            self.run_http(fail=True)
        self.assertEqual((self.directory/'cycle-0001-speech.mp3').read_bytes(), self.audio)
        self.assertEqual(json.loads((self.directory/'cycle-0001-transcription.json').read_bytes())['text'],
                         'different words')


if __name__ == '__main__':
    unittest.main()
