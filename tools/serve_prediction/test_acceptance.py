import os
import json
import subprocess
import unittest
from types import SimpleNamespace

from tools.serve_prediction.acceptance import LIMITS, prediction_usage, requests


class Driver(unittest.TestCase):
    def test_requests_match_budget_and_official_prediction_conditions(self):
        rows=requests(22)
        self.assertEqual(len(rows),7)
        self.assertLessEqual(len(rows),LIMITS['chats'])
        for request in rows.values():
            self.assertNotIn('max_completion_tokens',request)
            self.assertNotIn('tools',request)
            self.assertFalse(request.get('logprobs'))
            self.assertEqual(request.get('n',1),1)
        self.assertIn('response_format',rows['strict'])
        self.assertNotIn('prediction',rows['greedy'])

    def test_accounting_assertion_rejects_corruption(self):
        value=SimpleNamespace(usage=dict(completion_tokens=7,
            completion_tokens_details=dict(accepted_prediction_tokens=4,rejected_prediction_tokens=2)))
        self.assertEqual(prediction_usage(value,accepted=4,rejected=2),(4,2))
        with self.assertRaises(RuntimeError):prediction_usage(value,accepted=5)
        value.usage['completion_tokens_details']['rejected_prediction_tokens']=5
        with self.assertRaises(RuntimeError):prediction_usage(value)

    def test_frozen_requests_reach_native_contract(self):
        probe=os.environ['Q4T_PREDICTION_PROBE']
        lines=[json.dumps(dict(raw=json.dumps(req))) for req in requests(22).values()]
        run=subprocess.run([probe],input='\n'.join(lines)+'\n',text=True,
            capture_output=True,timeout=10,check=True)
        rows=[json.loads(line) for line in run.stdout.splitlines()]
        self.assertEqual(len(rows),7)
        self.assertTrue(all(row.get('accepted') for row in rows),rows)


if __name__=='__main__':unittest.main()
