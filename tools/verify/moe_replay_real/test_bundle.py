"""Host negative controls for complete capture provenance; no GPU work."""
import copy
import json
import struct
import unittest

from bundle import validate_bundle
from report import sha
from test_report import ReportTest


class BundleTest(unittest.TestCase):
    def setUp(self):
        self.helper = ReportTest()
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)
        self.root = self.helper.root
        self.identity = self.helper.meta['identity']
        tokens = [11, 12, 13, 14, 15]
        (self.root / 'tokens.i32').write_bytes(struct.pack('<5i', *tokens))
        (self.root / 'completion.json').write_text(json.dumps(dict(
            status='PASS', prompt_tokens=1, forced_tokens=4,
            same_shape_cases=5, model_invocations=7)))
        names = [('prefill-t1', 0, 1), ('prefill-t4', 0, 1),
                 ('t4', 1, 4), ('t4-replay', 1, 4)]
        for row in range(4):
            names += [(f't1-row{row}' + suffix, row + 1, 1) for suffix in
                      ['', '-replay', '-batch-ids', '-same-input-normal',
                       '-same-input-batch-ids']]
        for name, position, rows in names:
            meta = copy.deepcopy(self.helper.meta)
            meta.update(position=position, rows=rows, tokens=tokens[:position + rows])
            payload = {key: values * rows for key, values in self.helper.payload.items()}
            self.helper.write(name, payload, meta)
        for row in range(4):
            tag = f't1-row{row}'
            for suffix, parent, parent_row in [('-batch-ids', tag, 0),
                                              ('-same-input-batch-ids', 't4', row)]:
                target = tag + suffix
                digest = lambda name: sha((self.root / name / 'record.json').read_bytes())
                (self.root / target / 'intervention.json').write_text(json.dumps(dict(
                    kind='fixed_ids_from_explicit_row', source_record_sha256=digest('t4'),
                    source_row=row, target_record_sha256=digest(target),
                    target_original_record_sha256=digest(parent), target_input_row=parent_row)))

    def test_complete(self):
        self.assertEqual(len(validate_bundle(self.root, self.identity)), 16)

    def test_wrong_identity(self):
        with self.assertRaisesRegex(ValueError, 'execution identity'):
            validate_bundle(self.root, dict(self.identity, binary=sha(b'wrong')))

    def test_wrong_tokens(self):
        (self.root / 'tokens.i32').write_bytes(struct.pack('<5i', 99, 12, 13, 14, 15))
        with self.assertRaisesRegex(ValueError, 'token mapping'):
            validate_bundle(self.root, self.identity)

    def test_wrong_intervention_row(self):
        path = self.root / 't1-row2-batch-ids/intervention.json'
        data = json.loads(path.read_text())
        data['source_row'] = 0
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'intervention provenance'):
            validate_bundle(self.root, self.identity)

    def test_replay_bytes_changed_with_valid_checksum(self):
        path = self.root / 't4-replay'
        data = struct.pack('<8H', *([0x4000] * 8))
        (path / 'output').write_bytes(data)
        meta = json.loads((path / 'record.json').read_text())
        meta['files']['output']['sha256'] = sha(data)
        (path / 'record.json').write_text(json.dumps(meta))
        with self.assertRaisesRegex(ValueError, 'bitexact mismatch'):
            validate_bundle(self.root, self.identity)


if __name__ == '__main__':
    unittest.main()
