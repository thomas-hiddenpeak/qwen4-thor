"""CPU replay of archived native bytes, never a live TLS/model acceptance."""
import copy
import json
from pathlib import Path
import subprocess
import unittest
import warnings

from tools.serve_responses import sdk_capture as capture

ROOT = Path(__file__).resolve().parents[2]
WIRE = ROOT / 'docs/evidence/serve-file-search-business-20261112/hidden.wire'


def record():
    wire = WIRE.read_bytes().decode('utf-8')
    events = capture.wire_events(wire)
    response = events[-1]['response']
    text = ''.join(part['text'] for item in response['output']
                   if item['type'] == 'message' for part in item['content']
                   if part['type'] == 'output_text')
    return dict(version='3.42.1', requests=1, wire=wire, events=events,
                response=response, sdk_final=dict(id=response['id'],
                    status=response['status'], output_text=text,
                    output_types=[item['type'] for item in response['output']]),
                sdk_events=[dict(type=e['type'], sequence_number=e['sequence_number'])
                            for e in events])


def replay():
    import httpx2 as httpx
    raw = WIRE.read_bytes()
    calls = []

    class Body(httpx.SyncByteStream):
        def __iter__(self):
            for offset in range(0, len(raw), 7):
                yield raw[offset:offset+7]

    class Replay(httpx.BaseTransport):
        def handle_request(self, request):
            calls.append((request.method, request.url.path))
            return httpx.Response(200, headers={'Content-Type': 'text/event-stream'},
                                  stream=Body())

    config = dict(base_url='https://127.0.0.1:19108/v1',
                  ca='/etc/ssl/certs/ca-certificates.crt', key='CPU_REPLAY_ONLY',
                  request=dict(model='qwen3.8-flash-next', input='CPU replay', stream=True))
    with warnings.catch_warnings(record=True) as emitted:
        warnings.simplefilter('always')
        actual = capture.stream_response(config, transport=Replay())
    capture.require(not emitted, 'SDK replay emitted warnings')
    capture.require(calls == [('POST', '/v1/responses')], 'SDK replay dispatch differs')
    return actual


class SdkCaptureTests(unittest.TestCase):
    def test_official_high_level_helper_and_split_native_wire_stay_separate(self):
        sdk = ROOT / '.q4t-work/application-service-20261102/sdk-env/bin/python'
        code = ('import sys,json;sys.path.insert(0,'+repr(str(ROOT))+');'
                'from tools.serve_responses.test_sdk_capture import replay;'
                'print(json.dumps(replay()))')
        child = subprocess.run([str(sdk), '-I', '-B', '-c', code],
                               capture_output=True, text=True, timeout=20)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(child.stderr, '')
        actual = json.loads(child.stdout)
        capture.validate(actual)
        self.assertEqual(actual, record())
        self.assertEqual(actual['wire'].encode(), WIRE.read_bytes())
        self.assertIn('file_search_call', actual['sdk_final']['output_types'])

    def test_incomplete_wire_sdk_metadata_mismatch_and_replay_cannot_certify(self):
        value = record()
        capture.validate(value)
        mutations = [lambda v: v.update(requests=2),
                     lambda v: v.update(requests=True),
                     lambda v: v.update(wire=v['wire'].rstrip('\r\n')),
                     lambda v: v.update(wire=v['wire']+'data: [DONE]\n\n'),
                     lambda v: v['sdk_final'].update(output_text='invented'),
                     lambda v: v['sdk_events'].pop(),
                     lambda v: v['response'].update(status='incomplete')]
        for mutate in mutations:
            bad = copy.deepcopy(value)
            mutate(bad)
            with self.subTest(mutate=mutate), self.assertRaises(RuntimeError):
                capture.validate(bad)
        for field, changed in [('sequence_number', False), ('snapshot', 'SDK-only')]:
            bad = copy.deepcopy(value)
            bad['events'][0][field] = changed
            bad['wire'] = ''.join('event: '+e['type']+'\ndata: '+json.dumps(e)+'\n\n'
                                  for e in bad['events'])
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                capture.validate(bad)


if __name__ == '__main__':
    unittest.main()
