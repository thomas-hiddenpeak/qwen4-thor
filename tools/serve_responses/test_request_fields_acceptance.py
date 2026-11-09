"""Bounded business requests and actual-response failure oracle, no model."""
import copy
import unittest

from tools.serve_responses import request_fields_acceptance as driver
from tools.serve_responses.test_contract import errors
from tools.serve_service.package import read


class RequestFieldsAcceptance(unittest.TestCase):
    def test_frozen_valid_and_invalid_requests(self):
        self.assertEqual(driver.LIMITS, dict(starts=1, chats=2, controls=2000, seconds=600))
        rows = driver.requests()
        self.assertEqual(len(rows), 2)
        self.assertNotIn('input', rows['instructions'])
        for value in rows.values():
            self.assertEqual(errors(value, 'response_create'), [])
        self.assertEqual(len(driver.invalid_requests()), 10)
        for param, value in driver.invalid_requests():
            with self.subTest(param=param):
                self.assertTrue(errors(value, 'response_create'))

    def test_business_oracle_rejects_false_tier_and_wrong_output(self):
        paths = list((driver.ROOT/'docs/evidence/serve-input-tokens-business-20261110').rglob('initial.sdk.json'))
        self.assertEqual(len(paths), 1)
        value = read(paths[0])['result']
        driver.check_generation(value)
        with self.assertRaises(RuntimeError):
            driver.check_generation(dict(value, service_tier='priority'))
        wrong = copy.deepcopy(value)
        for item in wrong['output']:
            if item['type'] == 'message':
                for part in item['content']:
                    if part['type'] == 'output_text':
                        part['text'] = '43'
        with self.assertRaises(RuntimeError):
            driver.check_generation(wrong)


if __name__ == '__main__':
    unittest.main()
