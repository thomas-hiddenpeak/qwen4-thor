"""Regression tests using the actual frozen equal-length quality prompts."""
import copy
import hashlib
import json
from pathlib import Path
import unittest

from oracle import short_answer

ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / '.q4t-work/session-cache-20261027/phase-c-fixed-cap/plan.json'


class OracleTest(unittest.TestCase):
    def setUp(self):
        self.plan = json.loads(PLAN.read_text())

    def test_actual_recovery_answer(self):
        self.assertEqual(short_answer(self.plan), '710130')
        self.assertNotEqual(short_answer(self.plan), self.plan['short_expected'])

    def test_reordered_equal_lengths(self):
        self.plan['quality'] = dict(reversed(list(self.plan['quality'].items())))
        self.assertEqual(short_answer(self.plan), '710130')

    def test_changed_prompt_is_not_reused(self):
        self.plan['short_prompt'] += 'changed'
        with self.assertRaises(KeyError):
            short_answer(self.plan)

    def test_bad_record_identity_rejected(self):
        digest = hashlib.sha256(self.plan['short_prompt'].encode()).hexdigest()
        self.plan['quality'][digest]['prompt_sha256'] = 'incorrect'
        with self.assertRaises(ValueError):
            short_answer(self.plan)


if __name__ == '__main__':
    unittest.main()
