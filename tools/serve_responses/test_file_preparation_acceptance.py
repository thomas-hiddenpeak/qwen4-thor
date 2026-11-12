"""Checks the bounded real-model driver's request and failure oracle."""
import copy
import json
import unittest

from tools.serve_responses import file_preparation_acceptance as driver
from tools.serve_responses.test_contract import errors
from tools.serve_service.package import read


class FilePreparationAcceptance(unittest.TestCase):
    def test_budget_and_actual_standard_file_requests(self):
        self.assertEqual(driver.LIMITS, dict(starts=1, chats=4, controls=2000, seconds=600))
        rows = driver.requests()
        self.assertEqual(len(rows), 4)
        for name in ('initial', 'follow', 'background'):
            self.assertEqual(errors(rows[name], 'response_create'), [])
        self.assertEqual(driver.base64.b64decode(
            rows['initial']['input'][0]['content'][0]['file_data']), driver.CSV)
        self.assertTrue(rows['background']['background'])
        self.assertTrue(rows['follow']['stream'])
        self.assertNotIn('previous_response_id', rows['follow'])

    def test_answer_oracle_rejects_wrong_answer_and_incomplete(self):
        paths = list((driver.ROOT/'docs/evidence/serve-input-tokens-business-20261110').rglob('initial.sdk.json'))
        self.assertEqual(len(paths), 1)
        value = copy.deepcopy(read(paths[0])['result'])
        for item in value['output']:
            if item['type'] == 'message':
                for part in item['content']:
                    if part['type'] == 'output_text':
                        part['text'] = json.dumps(dict(value=42))
        driver.check_answer(value, 42)
        with self.assertRaises(RuntimeError):
            driver.check_answer(value, 7)
        with self.assertRaises(RuntimeError):
            driver.check_answer(dict(value, status='incomplete'), 42)
        self.assertEqual(driver.model_steps('[sampling] request=one\nnoise [sampling] request=two\n'), 1)


if __name__ == '__main__':
    unittest.main()
