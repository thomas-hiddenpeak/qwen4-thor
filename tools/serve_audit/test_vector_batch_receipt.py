"""Replay captured native SDK requests and reject broken helper/paging evidence."""
import copy
import json
from pathlib import Path
import unittest

from tools.serve_audit.vector_batch_receipt import verify

PROOF = Path(__file__).resolve().parents[2] / 'docs/evidence/serve-vector-batch-sdk-20261128'
DOCUMENTS = {'release-586.txt': 586, 'release-588.txt': 588}


class VectorBatchReceiptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.samples = json.loads((PROOF / 'cpu-wire.json').read_bytes())

    def validate(self, sample, generate=False):
        return verify(sample['write'], sample['read'], documents=DOCUMENTS, generate=generate)

    def test_native_roundtrips(self):
        seen = set()
        for sample in self.samples:
            identities = self.validate(sample)
            self.assertTrue(seen.isdisjoint(identities))
            seen.update(identities)

    def test_poll_helper_header_required(self):
        sample = copy.deepcopy(self.samples[0])
        next(r for r in sample['write']['requests'] if r['method'] == 'GET')['helper'] = None
        with self.assertRaisesRegex(ValueError, 'stock SDK polling'):
            self.validate(sample)

    def test_next_page_must_use_first_file_cursor(self):
        sample = copy.deepcopy(self.samples[1])
        rows = sample['read']['requests']
        rows[2]['query'] = rows[1]['query']
        with self.assertRaisesRegex(ValueError, 'pagination cursor'):
            self.validate(sample)

    def test_rejects_different_selected_file(self):
        sample = copy.deepcopy(self.samples[0])
        sample['read']['selected'] = sample['read']['files']['release-586.txt']
        with self.assertRaisesRegex(ValueError, 'another document'):
            self.validate(sample)

    def test_cpu_receipt_cannot_claim_model_call(self):
        with self.assertRaisesRegex(ValueError, 'model-backed'):
            self.validate(self.samples[0], generate=True)

    def test_pending_batch_does_not_qualify(self):
        sample = copy.deepcopy(self.samples[0])
        sample['write']['counts'].update(completed=1, in_progress=1)
        with self.assertRaisesRegex(ValueError, 'terminal counts'):
            self.validate(sample)
