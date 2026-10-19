"""Pure host numerical/report contract tests. No model or GPU invocation."""
import copy
import json
from pathlib import Path
import struct
import tempfile
import unittest

from report import Record, compare, sha

ROOT = Path(__file__).resolve().parents[3]


class ReportTest(unittest.TestCase):
    def setUp(self):
        root = ROOT / '.q4t-work/moe-real-report-tests'
        root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.meta = dict(version=1, role='main', layer=2, position=1, rows=1,
                         experts=4, top_k=2, hidden_size=2, vocab=248320,
                         tokens=[11, 12],
                         identity={key: sha(key.encode()) for key in
                                   ['model', 'weights', 'binary', 'tokenizer', 'source']})
        self.payload = dict(hidden=[0x3f80, 0x4000],
                            logits=[0x3f80, 0, 0x4000, 0], ids=[0, 2],
                            weights=[0.25, 0.75], output=[0x3f80, 0x4000])

    def write(self, name, payload=None, meta=None):
        directory = self.root / name
        directory.mkdir()
        metadata = copy.deepcopy(meta or self.meta)
        metadata['files'] = {}
        for key, values in (payload or self.payload).items():
            code = 'i' if key == 'ids' else ('f' if key == 'weights' else 'H')
            data = struct.pack('<' + code * len(values), *values)
            (directory / key).write_bytes(data)
            metadata['files'][key] = {'path': key, 'sha256': sha(data)}
        path = directory / 'record.json'
        path.write_text(json.dumps(metadata))
        return path

    def test_permutation_uses_expert_alignment(self):
        other = copy.deepcopy(self.payload)
        other.update(ids=[2, 0], weights=[0.75, 0.25])
        row = compare(Record(self.write('a')), Record(self.write('b', other)))['rows'][0]
        self.assertEqual(row['selection']['ordered_slot_differences'], 2)
        self.assertEqual(row['selection']['left_only'], [])
        self.assertEqual(row['aligned_common_experts']['weights']['max_abs'], 0)

    def test_different_sets_do_not_impute_missing_weights(self):
        other = copy.deepcopy(self.payload)
        other.update(ids=[2, 3], weights=[0.5, 0.5])
        row = compare(Record(self.write('a')), Record(self.write('b', other)))['rows'][0]
        self.assertEqual(row['selection']['left_only'], [0])
        self.assertEqual(row['selection']['right_only'], [3])
        weights = row['aligned_common_experts']['weights']
        self.assertEqual(weights['elements'], 1)
        self.assertEqual(weights['max_abs'], 0.25)

    def test_known_difference_and_upstream_warning(self):
        other = copy.deepcopy(self.payload)
        other['hidden'] = [0x4000, 0x4000]  # [2, 2] vs [1, 2]
        row = compare(Record(self.write('a')), Record(self.write('b', other)))['rows'][0]
        self.assertEqual(row['input_hidden']['bitwise_differences'], 1)
        self.assertEqual(row['input_hidden']['l2'], 1)
        self.assertEqual(row['input_hidden']['max_abs'], 1)
        self.assertIn('does not isolate', row['interpretation'])
        self.assertEqual(row['moe_output']['bitwise_differences'], 0)

    def test_logical_alignment_ignores_only_future_suffix(self):
        batch = {key: values * 2 for key, values in self.payload.items()}
        metadata = copy.deepcopy(self.meta)
        metadata.update(rows=2, tokens=[11, 12, 99])
        report = compare(Record(self.write('a')), Record(self.write('b', batch, metadata)))
        self.assertEqual(len(report['rows']), 1)
        self.assertEqual(report['rows'][0]['position'], 1)
        self.assertEqual(report['rows'][0]['input_hidden']['bitwise_differences'], 0)

    def test_wrong_prefix_or_model_is_rejected(self):
        left = Record(self.write('a'))
        for field in ['tokens', 'model']:
            metadata = copy.deepcopy(self.meta)
            if field == 'tokens':
                metadata['tokens'][0] += 1
            else:
                metadata['identity']['model'] = sha(b'other-model')
            with self.assertRaises(ValueError):
                compare(left, Record(self.write(field, meta=metadata)))

    def test_nonfinite_duplicate_out_of_range_and_missing_rows(self):
        bad = []
        for key, values in [('hidden', [0x7fc0, 0]), ('ids', [0, 0]),
                            ('ids', [0, 4]), ('weights', [float('nan'), 0]),
                            ('output', [0x3f80])]:
            payload = copy.deepcopy(self.payload)
            payload[key] = values
            bad.append(payload)
        for i, payload in enumerate(bad):
            with self.assertRaises(ValueError):
                Record(self.write(str(i), payload))

    def test_checksum_truncation_and_escape_rejected(self):
        for name in ['checksum', 'truncate', 'escape']:
            path = self.write(name)
            if name == 'checksum':
                (path.parent / 'ids').write_bytes(struct.pack('<ii', 1, 2))
            elif name == 'truncate':
                (path.parent / 'ids').write_bytes(b'')
            else:
                metadata = json.loads(path.read_text())
                metadata['files']['ids']['path'] = '../ids'
                path.write_text(json.dumps(metadata))
            with self.assertRaises(ValueError):
                Record(path)

    def test_duplicate_fields_and_boolean_dimensions_rejected(self):
        path = self.write('duplicates')
        path.write_text(path.read_text().replace('"rows": 1', '"rows": 1, "rows": 1'))
        with self.assertRaises(ValueError):
            Record(path)
        metadata = copy.deepcopy(self.meta)
        metadata['rows'] = True
        with self.assertRaises(ValueError):
            Record(self.write('boolean', meta=metadata))


if __name__ == '__main__':
    unittest.main()
