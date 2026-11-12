"""SDK consumption of actual C++ CPU output, not live HTTP/model acceptance."""
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest


def compare(actual, expected):
    if isinstance(expected, dict):
        assert isinstance(actual, dict)
        for key, value in expected.items():
            assert key in actual, key
            compare(actual[key], value)
    elif isinstance(expected, list):
        assert isinstance(actual, list) and len(actual) == len(expected)
        for first, second in zip(actual, expected):
            compare(first, second)
    else:
        assert actual == expected, (actual, expected)


def python_sdk(base, directory, report):
    import openai
    assert openai.__version__ == '3.42.1'
    directory = Path(directory)
    consumed = []
    with openai.OpenAI(base_url=base, api_key='cpu-captured-wire',
                       max_retries=0, timeout=3) as sdk:
        for name in ('included', 'hidden'):
            expected = json.loads((directory/(name+'.json')).read_text())
            request = json.loads((directory/(name+'.request.json')).read_text())
            request.pop('stream')
            request.pop('stream_options')
            # The Python stream helper requires model even though the native
            # planner and wire request schema permit the configured default.
            request['model'] = expected['model']
            response = sdk.responses.create(**request)
            compare(response.model_dump(mode='json'), expected)
            assert response.output_text == expected['output'][1]['content'][0]['text']
            with sdk.responses.stream(**request,
                    stream_options={'include_obfuscation': False}) as stream:
                events = [event.model_dump(mode='json') for event in stream]
                compare(stream.get_final_response().model_dump(mode='json'), expected)
            assert [event['sequence_number'] for event in events] == list(range(len(events)))
            consumed.append(dict(name=name, response=response.model_dump(mode='json'),
                                 events=events))
    Path(report).write_text(json.dumps(dict(status='PASS', version=openai.__version__,
                                           consumed=consumed), indent=2)+'\n')


@contextmanager
def peer(directory):
    requests, failures = [], []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            try:
                assert self.path == '/v1/responses'
                length = int(self.headers['Content-Length'])
                assert 0 < length < 65536
                request = json.loads(self.rfile.read(length))
                name = ('included' if 'file_search_call.results' in
                        request.get('include', []) else 'hidden')
                expected = json.loads((directory/(name+'.request.json')).read_text())
                if 'model' in request:
                    expected['model'] = json.loads(
                        (directory/(name+'.json')).read_text())['model']
                streaming = request.get('stream', False)
                if not streaming:
                    expected.pop('stream')
                    expected.pop('stream_options')
                compare(request, expected)
                requests.append(dict(name=name, stream=streaming, request=request))
                assert len(requests) <= 4
                data = (directory/(name+('.wire' if streaming else '.json'))).read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream' if streaming
                                 else 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                # Deliberately split multibyte UTF-8 and SSE records.
                for offset in range(0, len(data), 7):
                    self.wfile.write(data[offset:offset+7])
                self.wfile.flush()
            except Exception as error:
                failures.append(repr(error))
                self.close_connection = True
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/v1', requests, failures
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)
        assert not thread.is_alive()


class FileSearchTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures = Path(os.environ['Q4T_RESPONSES_FILE_SEARCH_FIXTURES']).resolve()
        cls.output = Path(os.environ['Q4T_TEST_OUTPUT_DIR']).resolve()
        cls.output.mkdir(parents=True, exist_ok=True)

    def sdk(self, python):
        prefix = self.output/self._testMethodName
        with peer(self.fixtures) as (base, requests, failures):
            command = ([os.environ['Q4T_PYTHON_SDK'], __file__, '--sdk-child']
                       if python else ['node', 'tools/serve_responses/file_search_sdk.mjs'])
            command += [base, str(self.fixtures), str(prefix)+'.json']
            result = subprocess.run(command, capture_output=True, timeout=20)
            Path(str(prefix)+'.stdout').write_bytes(result.stdout)
            Path(str(prefix)+'.stderr').write_bytes(result.stderr)
            Path(str(prefix)+'.requests.json').write_text(json.dumps(requests, indent=2)+'\n')
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertFalse(failures)
            self.assertEqual([(r['name'], r['stream']) for r in requests],
                             [(n, s) for n in ('included', 'hidden') for s in (False, True)])
            report = json.loads(Path(str(prefix)+'.json').read_text())
            self.assertEqual(report['status'], 'PASS')
            for result in report['consumed']:
                expected = [json.loads(line[6:]) for line in
                            (self.fixtures/(result['name']+'.wire')).read_text().splitlines()
                            if line.startswith('data: {')]
                compare(result['events'], expected)

    def test_javascript_native_file_search_json_and_sse(self):
        self.sdk(False)

    def test_python_native_file_search_json_and_sse(self):
        self.sdk(True)

    def test_native_file_search_unified_schema_lifecycle_and_projection(self):
        from tools.serve_responses.test_contract import errors
        sources, checked = {}, 0
        for name in ('included', 'hidden'):
            for suffix in ('request.json', 'json', 'wire'):
                path = self.fixtures/(name+'.'+suffix)
                sources[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
            request = json.loads((self.fixtures/(name+'.request.json')).read_text())
            response = json.loads((self.fixtures/(name+'.json')).read_text())
            events = [json.loads(line[6:]) for line in
                      (self.fixtures/(name+'.wire')).read_text().splitlines()
                      if line.startswith('data: {')]
            for kind, value in [('response_create', request), ('response', response)] + [
                    ('response_event', event) for event in events]:
                self.assertFalse(errors(value, kind), (name, kind, errors(value, kind)))
                checked += 1
            self.assertEqual([e['sequence_number'] for e in events], list(range(len(events))))
            self.assertEqual(events[-1]['type'], 'response.completed')
            self.assertEqual(events[-1]['response'], response)
            call, message = response['output']
            stages = [e['type'] for e in events if e['type'].startswith('response.file_search_call.')]
            self.assertEqual(stages, ['response.file_search_call.'+s for s in
                                     ('in_progress', 'searching', 'completed')])
            self.assertEqual(len([e for e in events if e['type'] == 'response.output_item.done'
                                  and e['item']['id'] == call['id']]), 1)
            text = message['content'][0]
            citation, = text['annotations']
            self.assertEqual(citation['type'], 'file_citation')
            self.assertEqual(citation['index'], len(text['text']))
            self.assertIn('[file:'+citation['file_id']+']', text['text'])
            annotations = [e for e in events if e['type'] == 'response.output_text.annotation.added']
            self.assertEqual(len(annotations), 1)
            self.assertEqual(annotations[0]['annotation'], citation)
            if name == 'included':
                hit, = call['results']
                self.assertEqual(hit['file_id'], citation['file_id'])
                self.assertEqual(hit['filename'], citation['filename'])
                self.assertEqual(hit['text'], 'PLE streams immutable tables from SSD.')
            else:
                self.assertIsNone(call['results'])
                self.assertNotIn('PLE streams immutable tables from SSD.', json.dumps(events))
        (self.output/'file-search-wire-schema.json').write_text(json.dumps(
            dict(status='PASS', checked=checked, sources_sha256=sources), indent=2)+'\n')


if __name__ == '__main__':
    if sys.argv[1:2] == ['--sdk-child']:
        python_sdk(*sys.argv[2:])
    else:
        unittest.main()
