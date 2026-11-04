"""Replay actual TLS access rows; never reopen listeners or execute a model."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.serve_gateway import sources_stage as stage


class Access(unittest.TestCase):
    def setUp(self):
        parent = stage.ROOT/'build/gateway-source-finish-tests'
        parent.mkdir(exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=parent))
        (self.root/'nginx').mkdir()
        original = stage.BASE/'execution/business'
        self.rows = [json.loads(line) for line in (original/'nginx/stdout.log').read_bytes().splitlines()]
        self.run = object.__new__(stage.Run)
        self.run.probes = json.loads((original/'sources.json').read_bytes())['probes']

    def validate(self, rows):
        (self.root/'nginx/stdout.log').write_text('\n'.join(json.dumps(row) for row in rows)+'\n')
        with patch.object(stage.s, 'OUT', self.root):
            self.run.logs()

    def test_actual_empty_upstream_rows_and_legacy_absence_marker(self):
        self.assertEqual(sum(row['status'] == 403 and row['upstream_status'] == ''
                             for row in self.rows), 16)
        self.validate(self.rows)
        rows = copy.deepcopy(self.rows)
        for row in rows:
            if row['upstream_status'] == '':
                row['upstream_status'] = '-'
        self.validate(rows)

    def test_a_real_upstream_status_on_any_rejection_still_fails(self):
        for status in (401, 403, 404):
            rows = copy.deepcopy(self.rows)
            next(row for row in rows if row['status'] == status)['upstream_status'] = str(status)
            with self.subTest(status=status), self.assertRaisesRegex(RuntimeError, 'proxied'):
                self.validate(rows)

    def test_missing_duplicate_and_wrong_source_records_fail(self):
        for change in ('missing', 'duplicate', 'source'):
            rows = copy.deepcopy(self.rows)
            if change == 'missing':
                rows.pop()
            elif change == 'duplicate':
                rows[0]['gateway_id'] = rows[1]['gateway_id']
            else:
                rows[0]['source_allowed'] = 1
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                self.validate(rows)


if __name__ == '__main__':
    unittest.main()
