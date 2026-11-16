"""Targeted video business contracts; CPU fixtures never count as model evidence."""
import base64
import copy
import json
import os
from pathlib import Path
import ssl
import tempfile
import unittest

from tools.serve_responses import video_acceptance as driver
from tools.serve_responses import test_mcp_acceptance as previous
from tools.serve_responses.test_contract import errors
from tools.serve_service.package import read


class VideoAcceptance(unittest.TestCase):
    def test_requests_use_real_standard_file_bytes_and_fixed_budget(self):
        self.assertEqual(driver.LIMITS, dict(starts=1, chats=8,
                                             controls=2000, seconds=900))
        rows = driver.requests()
        self.assertEqual(len(rows), 8)
        for name, request in rows.items():
            self.assertEqual([(list(e.path), e.message[:100]) for e in
                              errors(request, 'response_create')], [], name)
        stream = rows['stream']['input'][0]['content'][0]
        self.assertEqual(base64.b64decode(stream['file_data'], validate=True),
                         (driver.ASSETS/driver.CLIPS[1]).read_bytes())
        self.assertNotIn('video_frames', json.dumps(rows))
        for key in ('direct', 'stream', 'tool', 'mcp'):
            self.assertNotIn('red', str(rows[key]['input']))
            self.assertNotIn('blue', str(rows[key]['input']))
        self.assertEqual(driver.inline('broken.mp4', b'bad')['file_data'], 'YmFk')

    def test_answer_oracle_rejects_reverse_order_extra_labels_and_incomplete(self):
        paths = list((driver.ROOT/'docs/evidence/serve-input-tokens-business-20261110')
                     .rglob('initial.sdk.json'))
        self.assertEqual(len(paths), 1)
        response = copy.deepcopy(read(paths[0])['result'])
        parts = [part for item in response['output'] if item['type'] == 'message'
                 for part in item['content'] if part['type'] == 'output_text']
        self.assertEqual(len(parts), 1)
        parts[0]['text'] = '{"start":"red","end":"blue"}'
        self.assertEqual(driver.color_answer(response), dict(start='red', end='blue'))
        for wrong in ('{"start":"blue","end":"red"}',
                      '{"start":"red","end":"blue","extra":true}', '{}'):
            parts[0]['text'] = wrong
            with self.assertRaises(RuntimeError):
                driver.color_answer(response)
        parts[0]['text'] = '{"start":"red","end":"blue"}'
        with self.assertRaises(RuntimeError):
            driver.color_answer(dict(response, status='incomplete'))
        parts[0].clear()
        parts[0].update(type='refusal', refusal='Cannot inspect the frames')
        with self.assertRaisesRegex(RuntimeError, 'video task refused:'):
            driver.color_answer(response)


class VideoPeer(unittest.TestCase):
    rpc = previous.ProjectPeer.rpc

    def setUp(self):
        root = Path(os.environ['Q4T_TEST_OUTPUT_DIR'])
        root.mkdir(parents=True, exist_ok=True)
        self.output = Path(tempfile.mkdtemp(prefix='video-business-peer-', dir=root))
        self.peer = driver.peer.ProjectMcp(driver.ROOT, self.output, 0,
                                           source=driver.VideoSource())
        self.addCleanup(self.peer.close)
        self.context = ssl.create_default_context(cafile=str(self.peer.ca[0]))

    def test_actual_tls_resource_preserves_video_bytes_and_rejects_wrong_target(self):
        self.assertEqual(self.rpc('initialize', secret='wrong')[0], 403)
        session = self.rpc('initialize')[1]['MCP-Session-Id']
        source = driver.VideoSource()
        self.assertEqual(self.rpc('tools/list', session=session)[2]['result'],
                         source.catalog())
        self.assertEqual(self.rpc('tools/call', dict(name=source.name,
            arguments=dict(path='/etc/passwd')), session=session)[0], 400)
        call = self.rpc('tools/call', dict(name=source.name, arguments=source.arguments),
                        session=session)[2]['result']
        self.assertEqual(call['structuredContent'], source.read()[0])
        self.assertEqual(call['content'][1]['uri'], source.uri)
        self.assertNotIn('red', call['content'][0]['text'])
        self.assertNotIn('blue', call['content'][0]['text'])
        self.assertEqual(self.rpc('resources/read', dict(uri='file:///etc/passwd'),
                                  session=session)[0], 400)
        content = self.rpc('resources/read', dict(uri=source.uri),
                           session=session)[2]['result']['contents'][0]
        self.assertEqual(content['mimeType'], 'video/webm')
        self.assertEqual(base64.b64decode(content['blob'], validate=True),
                         (driver.ASSETS/source.filename).read_bytes())
        self.assertEqual((len(self.peer.calls), len(self.peer.reads)), (1, 1))


if __name__ == '__main__':
    unittest.main()
