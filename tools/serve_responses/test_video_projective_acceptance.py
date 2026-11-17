"""Direct checks for frozen inputs and strict business oracles; no inference."""
import base64
import copy
import json
import struct
import unittest

from tools.serve_responses import video_projective_acceptance as driver
from tools.serve_responses.test_contract import errors
from tools.serve_service.package import read


class ProjectiveAcceptance(unittest.TestCase):
    def test_original_pixel_payload_and_four_request_contract(self):
        self.assertEqual(driver.LIMITS, dict(starts=1, chats=4,
                                            controls=2000, seconds=600))
        original = (driver.video.ASSETS/'geometry.mp4').read_bytes()
        for name, matrix in driver.MATRICES.items():
            actual = bytearray(driver.clip(name))
            at = actual.index(b'tkhd') + 4 + 40
            self.assertEqual(struct.unpack('>9i', actual[at:at+36]), matrix)
            actual[at:at+36] = original[at:at+36]
            self.assertEqual(bytes(actual), original)
        rows = driver.requests()
        self.assertEqual(len(rows), 4)
        for name, request in rows.items():
            self.assertEqual([(list(e.path), e.message[:100]) for e in
                              errors(request, 'response_create')], [], name)
        self.assertEqual(base64.b64decode(rows['perspective']['input'][0]['content'][0]
            ['file_data'], validate=True), driver.clip('perspective'))
        self.assertTrue(rows['homogeneous']['store'] and rows['perspective']['stream'] and
                        rows['replay']['stream'])
        for name in ('homogeneous', 'perspective'):
            self.assertEqual(rows[name]['input'][0]['content'][0]['detail'], 'high')
            question = rows[name]['input'][0]['content'][1]['text']
            for color in ('red', 'green', 'blue', 'yellow'):
                self.assertNotIn(color, question)
        self.assertEqual(driver.invalid_count()['input'][0]['content'][0]
                         ['filename'], 'singular.mp4')

    def test_answer_requires_real_complete_schema_and_exact_geometry(self):
        paths = list((driver.ROOT/'docs/evidence/serve-input-tokens-business-20261110')
                     .rglob('initial.sdk.json'))
        self.assertEqual(len(paths), 1)
        response = copy.deepcopy(read(paths[0])['result'])
        parts = [part for item in response['output'] if item['type'] == 'message'
                 for part in item['content'] if part['type'] == 'output_text']
        self.assertEqual(len(parts), 1)
        parts[0]['text'] = json.dumps(driver.EXPECTED)
        self.assertEqual(driver.answer(response), driver.EXPECTED)
        for wrong in (dict(driver.EXPECTED, layout='square'),
                      dict(driver.EXPECTED, top_left='black'), {},
                      dict(driver.EXPECTED, extra=True)):
            parts[0]['text'] = json.dumps(wrong)
            with self.assertRaises(RuntimeError):
                driver.answer(response)
        parts[0].clear()
        parts[0].update(type='refusal', refusal='Cannot inspect video')
        with self.assertRaisesRegex(RuntimeError, 'actual projective video refused'):
            driver.answer(response)


if __name__ == '__main__':
    unittest.main()
