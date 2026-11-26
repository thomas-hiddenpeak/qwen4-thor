"""Verify the cancellation handoff rejects mismatched routes, owners and state."""
from copy import deepcopy
import unittest

from tools.serve_recovery import ledger, profile_live_reuse as reuse, profile_record
from tools.serve_responses import eval_cancel_profiles as profiles
from tools.serve_service import package


class EvalCancelProfilesTest(unittest.TestCase):
    def setUp(self):
        proof = package.read(profiles.PROOF)
        old, current = deepcopy(proof['old']), deepcopy(proof['current'])
        current.pop('deleted')
        # The standalone record predates cross-capacity canonical resource hashing.
        old['resource_sha256'] = current['resource_sha256'] = 'a' * 64
        self.first = dict(old=old, current=current, restored=None)
        self.next = deepcopy(self.first)
        self.next['restored'] = dict(current, deleted=404)
        self.next['restored'].pop('fresh_chat')
        for row in (self.next['old'], self.next['current']):
            row['eval_id'] = 'eval_' + 'b' * 32
            row['run_id'] = 'evalrun_' + 'c' * 32
            row['path'] = row['path'].replace(old['eval_id'], row['eval_id']).replace(old['run_id'], row['run_id'])

    def test_canceled_original_survives_and_fresh_run_is_independent(self):
        profiles.check(self.first)
        profiles.check(self.next, self.first)
        for field, wrong in [('run_id', self.next['old']['run_id']), ('peer', 200),
                             ('resource_sha256', '0' * 64), ('before', 'in_progress'), ('deleted', 200)]:
            sample = deepcopy(self.next)
            sample['restored'][field] = wrong
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                profiles.check(sample, self.first)

    def test_initial_controls_and_wire_path(self):
        for part, field, value in [('old', 'path', self.first['current']['path']),
                                   ('current', 'path', self.first['old']['path']),
                                   ('old', 'foreign_cancel', 200), ('old', 'malformed', 200),
                                   ('old', 'resource_sha256', 'b' * 64),
                                   ('current', 'fresh_chat', '41'), ('current', 'deleted', 404)]:
            sample = deepcopy(self.first)
            sample[part][field] = value
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                profiles.check(sample)

    def test_only_reviewed_source_format_gets_evidence_limit(self):
        sample = dict(format=reuse.EVAL_CANCEL_RECORD, reused_business=dict(format=reuse.EVAL_CANCEL_FORMAT))
        profile_record.shape(sample, profile_record.LEGACY_BYTES + 1)
        sample['reused_business']['format'] = reuse.FRAME_FORMAT
        with self.assertRaises(ledger.LedgerError):
            profile_record.shape(sample, profile_record.LEGACY_BYTES + 1)
