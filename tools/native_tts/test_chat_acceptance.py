import base64
import copy
import json
import unittest
import tempfile
import threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from tools.native_tts.chat_acceptance import Stage, HEADERS, check_usage, parse_stream


def usage():
    return dict(prompt_tokens=100,completion_tokens=100,total_tokens=200,
        q4t_audio_synthesis=dict(input_tokens=12,output_tokens=65),
        completion_tokens_details=dict(audio_tokens=64))


def events():
    def event(delta, **extra):
        return dict(object='chat.completion.chunk',choices=[dict(index=0,delta=delta,**extra)])
    return [event(dict(role='assistant'),finish_reason=None),
        event(dict(audio=dict(id='audio_1')),finish_reason=None),
        event(dict(audio=dict(transcript='actual utterance')),finish_reason=None),
        event(dict(audio=dict(data=base64.b64encode(b'actual').decode())),finish_reason=None),
        event(dict(audio=dict(expires_at=12345))),
        event({},finish_reason='stop'),
        dict(object='chat.completion.chunk',choices=[],usage=usage())]


def wire(value):
    return ''.join('data: '+json.dumps(row)+'\r\n\r\n' for row in value).encode()+b'data: [DONE]\r\n\r\n'


class Contract(unittest.TestCase):
    def test_actual_audio_bytes_usage_and_expiry_survive_reassembly(self):
        choices,actual,parsed=parse_stream(wire(events()),1)
        self.assertEqual(base64.b64decode(choices[0]['data']),b'actual')
        self.assertEqual(parsed,events())
        check_usage(actual,1)

    def test_expiry_finish_order_and_partial_stream_cannot_pass(self):
        original=events()
        for mutate in (
            lambda rows: rows[4]['choices'][0].update(finish_reason=None),
            lambda rows: rows.insert(5,copy.deepcopy(rows[3])),
            lambda rows: rows[5]['choices'][0].update(finish_reason='length'),
            lambda rows: rows[3]['choices'][0].update(index=1)):
            rows=copy.deepcopy(original);mutate(rows)
            with self.assertRaises((AssertionError,RuntimeError,ValueError)): parse_stream(wire(rows),1)
        with self.assertRaises((AssertionError,RuntimeError,ValueError)):
            parse_stream(wire(original).replace(b'data: [DONE]\r\n\r\n',b''),1)

    def test_intermediate_base64_padding_cannot_hide_later_audio(self):
        rows=events()
        rows[3]['choices'][0]['delta']['audio']['data']='eA==eQ=='
        with self.assertRaises((AssertionError,RuntimeError,ValueError)):
            parse_stream(wire(rows),1)

    def test_usage_must_contain_real_codes_eos_and_primary_work(self):
        for change in (
            lambda row: row.update(total_tokens=201),
            lambda row: row['q4t_audio_synthesis'].update(output_tokens=64),
            lambda row: row['completion_tokens_details'].update(audio_tokens=65),
            lambda row: row['q4t_audio_synthesis'].update(input_tokens=100)):
            value=usage();change(value)
            with self.assertRaises((AssertionError,RuntimeError,ValueError)): check_usage(value,1)


class ObservationRouting(unittest.TestCase):
    def test_local_operations_and_client_business_are_separate(self):
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                identity = self.headers.get('X-Q4T-Client-Id')
                token = self.headers.get('X-Q4T-Gateway-Token')
                local = self.path in ('/readyz', '/metrics', '/v1/config')
                allowed = ((self.server.operations and local and not identity and not token)
                    or (not self.server.operations and not local and identity == 'other'
                        and token == HEADERS['X-Q4T-Gateway-Token']))
                calls.append((self.server.operations, self.path, identity))
                self.send_response(200 if allowed else 403)
                self.end_headers(); self.wfile.write(b'{}')
            def log_message(self, *_args): pass
        servers = [ThreadingHTTPServer(('127.0.0.1', 0), Handler) for _ in range(2)]
        threads = []
        stage = Stage.__new__(Stage)
        stage.plan = dict(port=servers[0].server_port, operations_port=servers[1].server_port)
        stage.budget = lambda kind: self.assertEqual(kind, 'controls')
        try:
            for index, server in enumerate(servers):
                server.operations = bool(index)
                thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
                thread.start(); threads.append(thread)
            for path in ('/readyz', '/metrics', '/v1/config', '/v1/capabilities',
                         '/v1/chat/completions/chatcmpl_existing'):
                self.assertEqual(stage.http(path, client='other')[0], 200)
            self.assertEqual([row[0] for row in calls], [True, True, True, False, False])
        finally:
            for server in servers: server.shutdown(); server.server_close()
            for thread in threads: thread.join(timeout=2); self.assertFalse(thread.is_alive())

    def test_readiness_denial_is_preserved_and_never_retried(self):
        with tempfile.TemporaryDirectory(dir='.q4t-work') as directory:
            stage = Stage.__new__(Stage); stage.out = Path(directory)
            stage.same_model = lambda: None
            with patch.object(stage, 'http', side_effect=[(503, b'loading'),
                     (403, b'{"error":{"code":"client_route_forbidden"}}')]) as http, \
                 patch('tools.native_tts.chat_acceptance.time.sleep') as sleep:
                with self.assertRaisesRegex(RuntimeError, 'unexpected readiness status: 403'):
                    stage.wait_ready()
                self.assertEqual(http.call_count, 2)
                self.assertEqual(sleep.call_count, 1)
            receipt = json.loads((stage.out/'readiness-failure.json').read_text())
            self.assertEqual(receipt['status'], 403)
            self.assertIn('client_route_forbidden', receipt['body'])


if __name__=='__main__': unittest.main()
