"""Check that the SDK receipt proves an overlapping, owner-scoped cancellation."""
from pathlib import Path
import json
import unittest

from tools.serve_audit.batch_cancel_receipt import verify

SOURCE = Path(__file__).resolve().parents[2] / 'docs/evidence/serve-batch-cancel-20261128'


class BatchCancelHandoffTest(unittest.TestCase):
    def setUp(self):
        observed = json.loads((SOURCE / 'requests.json').read_bytes())
        self.writer = observed['writer']
        self.reader = observed['reader']
        self.document = (SOURCE / 'project-release.json').read_text()
        self.model = json.loads((SOURCE / 'device.json').read_bytes())['model']

    def verify(self):
        return verify(self.writer, self.reader, self.document, self.model)

    def test_original_pair(self):
        self.assertEqual(len(self.verify()), len(self.writer['requests']) + len(self.reader['requests']))

    def test_sequential_cancellations(self):
        first = [r for r in self.writer['requests'] if r.get('attempt') == 1]
        last = first[-1]
        end = max(r['headers_ns'] for r in first[:-1])
        shift = max(1, end - last['start_ns'])
        for row in self.writer['requests']:
            if row.get('client') == last['client']:
                row['start_ns'] += shift
                row['headers_ns'] += shift
        with self.assertRaisesRegex(ValueError, 'did not overlap'):
            self.verify()

    def test_new_cancellation_timestamp(self):
        self.writer['cancel'][0]['cancelling_at'] += 1
        with self.assertRaisesRegex(ValueError, 'one cancellation'):
            self.verify()

    def test_cross_owner_success(self):
        next(r for r in self.writer['requests'] if r['status'] == 404)['status'] = 200
        with self.assertRaisesRegex(ValueError, 'ownership'):
            self.verify()

    def test_replaced_downloaded_row(self):
        self.reader['output_text'] = json.dumps(dict(self.reader['result'], id='another-row'))
        with self.assertRaisesRegex(ValueError, 'downloaded JSONL'):
            self.verify()

    def test_wrong_second_page(self):
        pages = [r for r in self.reader['requests'] if r['path'] == '/v1/batches']
        pages[1]['query'] = 'limit=1&after=another-batch'
        with self.assertRaisesRegex(ValueError, 'cursor'):
            self.verify()
