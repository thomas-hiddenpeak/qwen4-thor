"""Strict retained-service request/oracle checks, without a model launch."""
import base64
import json
from pathlib import Path
import struct
import unittest

from tools.serve_responses import video_affine_service_acceptance as driver

ROOT = Path(__file__).resolve().parents[2]
WIRE = ROOT / 'docs/evidence/serve-video-affine-business-20261117/shear-sse.wire'


class AffineServiceTests(unittest.TestCase):
    def test_actual_accepted_wire_and_strict_failure(self):
        raw = WIRE.read_bytes()
        value = driver.check_sse(raw)
        self.assertEqual(value['status'], 'completed')
        bad = value.copy()
        bad['output'] = [dict(type='message', content=[dict(type='output_text',
            text=json.dumps(dict(driver.EXPECTED, top_right='yellow')))])]
        events = [dict(type='response.created'),
                  dict(type='response.completed', response=bad)]
        with self.assertRaises(RuntimeError):
            driver.check_sse(''.join('data: ' + json.dumps(e) + '\n\n'
                                    for e in events).encode())
        with self.assertRaises(RuntimeError):
            driver.check_sse(raw[:raw.rfind(b'event: response.completed')])

    def test_original_clip_and_explicit_detail_reach_request(self):
        request = driver.requests()['approved']
        part = request['input'][0]['content'][0]
        self.assertEqual(part['detail'], 'high')
        raw = base64.b64decode(part['file_data'])
        original = bytearray((ROOT / 'tests/fixtures/file_video/geometry.mp4').read_bytes())
        offset = original.index(b'tkhd') + 4 + 40
        original[offset:offset + 36] = struct.pack('>9i',
            65536, 0, 0, 32768, 65536, 0, 0, 0, 1 << 30)
        self.assertEqual(raw, original)
        self.assertTrue(request['stream'] and request['store'])
        self.assertNotIn('top_left is red', json.dumps(request))

    def test_wrapper_keeps_exact_lifecycle_and_distinct_acceptance(self):
        self.assertEqual(driver.GLOBALS['ACCEPTANCE_PREFIX'], 'VIDEO_AFFINE_SERVICE_')
        self.assertEqual(driver.BASE['LIMITS']['tool_retained']['starts'], 1)
        self.assertEqual(driver.BASE['LIMITS']['tool_retained']['chats'], 3)
        self.assertIs(driver.Stage.run, driver.BASE['Stage'].run)
        self.assertFalse(driver.business_record(False)['automatic_request_or_tool_replay'])


if __name__ == '__main__':
    unittest.main()
