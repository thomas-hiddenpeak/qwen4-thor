"""Targeted oracle/input checks; these fixtures do not prove model behavior."""
import base64
import copy
import json
import unittest

from tools.serve_responses import video_geometry_acceptance as driver
from tools.serve_responses.test_contract import errors
from tools.serve_service.package import read


class GeometryAcceptance(unittest.TestCase):
    def test_original_requests_and_fixed_budget_have_no_expected_answer(self):
        self.assertEqual(driver.LIMITS, dict(starts=1, chats=4,
                                            controls=2000, seconds=600))
        rows = driver.requests()
        self.assertEqual(len(rows), 4)
        for name, request in rows.items():
            self.assertEqual([(list(e.path), e.message[:100]) for e in
                errors(request, 'response_create')], [], name)
        for name in driver.CLIPS:
            parts = rows[name]['input'][0]['content']
            self.assertEqual(parts[1]['text'], driver.QUESTION)
            if name != 'rotation':
                self.assertEqual(base64.b64decode(parts[0]['file_data'], validate=True),
                    (driver.video.ASSETS/driver.CLIPS[name]).read_bytes())
            for color in ('red', 'green', 'blue', 'yellow'):
                self.assertNotIn(color, parts[1]['text'])
        self.assertTrue(rows['reflection']['stream'])
        self.assertTrue(driver.PROFILE['allow_media'])

    def test_answer_oracle_rejects_coded_orientation_wrong_shape_and_refusal(self):
        paths = list((driver.ROOT/'docs/evidence/serve-input-tokens-business-20261110')
                     .rglob('initial.sdk.json'))
        self.assertEqual(len(paths), 1)
        response = copy.deepcopy(read(paths[0])['result'])
        parts = [part for item in response['output'] if item['type'] == 'message'
                 for part in item['content'] if part['type'] == 'output_text']
        self.assertEqual(len(parts), 1)
        for name, expected in driver.EXPECTED.items():
            value = dict(zip((*driver.CORNERS, 'layout'), expected))
            parts[0]['text'] = json.dumps(value)
            self.assertEqual(driver.answer(response, name), value)
            for wrong in (dict(value, top_left='white'), dict(value, layout='square'),
                          dict(value, extra=True), {}):
                parts[0]['text'] = json.dumps(wrong)
                with self.assertRaises(RuntimeError):
                    driver.answer(response, name)
            parts[0]['text'] = json.dumps(value)
            with self.assertRaises(RuntimeError):
                driver.answer(dict(response, status='incomplete'), name)
        parts[0].clear()
        parts[0].update(type='refusal', refusal='Cannot inspect the video')
        with self.assertRaisesRegex(RuntimeError, 'display task refused:'):
            driver.answer(response, 'rotation')


if __name__ == '__main__':
    unittest.main()
