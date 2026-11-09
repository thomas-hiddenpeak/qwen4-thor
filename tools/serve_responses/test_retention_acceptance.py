"""Bounded real retention driver contracts and failure oracles, no model."""
import copy
import unittest

from tools.serve_responses import retention_acceptance as driver
from tools.serve_responses.test_contract import errors
from tools.serve_service.package import read


class RetentionAcceptance(unittest.TestCase):
    def test_frozen_requests_and_budget(self):
        self.assertEqual(driver.LIMITS, dict(starts=2, chats=4, controls=2000, seconds=900))
        self.assertEqual(len(driver.requests()), 4)
        for value in driver.requests().values():
            self.assertEqual(errors(value, 'response_create'), [])
        self.assertTrue(driver.requests()['background']['background'])
        self.assertFalse(driver.requests()['encrypted']['store'])

    def test_exact_plan_oracle_rejects_payload_leak_and_false_receipt(self):
        candidate = dict(owner_sha256='a'*64, kind='item', id='item_1',
                         revision=1, created_at=1, expires_at=0, bytes=123)
        receipt = dict(status=200, commit_unknown=False, value=dict(
            object='q4t.state_retention', plan_sha256='b'*64, applied=False,
            candidates=[candidate], limit=128, deleted_objects=0))
        driver.check_plan(receipt)
        for path, value in [('commit_unknown', True), ('payload', 'secret'),
                            ('deleted_objects', 1), ('plan_sha256', 'bad')]:
            changed = copy.deepcopy(receipt)
            if path == 'commit_unknown':
                changed[path] = value
            elif path == 'payload':
                changed['value']['candidates'][0][path] = value
            else:
                changed['value'][path] = value
            with self.assertRaises(RuntimeError):
                driver.check_plan(changed)

    def test_answer_oracle_uses_real_encrypted_response(self):
        path = driver.ROOT/'docs/evidence/serve-responses-encryption-business-20261109/background.sdk.json'
        value = read(path)['result']
        driver.check_answer(value)
        changed = copy.deepcopy(value)
        for item in changed['output']:
            if item['type'] == 'reasoning':
                item.pop('encrypted_content', None)
        with self.assertRaises(RuntimeError):
            driver.check_answer(changed)


if __name__ == '__main__':
    unittest.main()
