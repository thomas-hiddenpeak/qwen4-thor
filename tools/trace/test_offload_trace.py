"""Host-only token-order adapter contracts; no model, HTTP or GPU execution."""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
import zlib

from offload_trace import iter_layers, validate_request

ROOT = Path(__file__).resolve().parents[2]
CHECKER = None
OUTPUT = ROOT / 'build/offload-trace-contracts'


def _frame(payload):
    return struct.pack('<II', len(payload), zlib.crc32(payload)) + payload


class OffloadTraceContracts(unittest.TestCase):
    def setUp(self):
        OUTPUT.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='case-', dir=OUTPUT)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.binary = self.directory / 'archived-binary'
        self.binary.write_bytes(b'old source identity, not the current runtime')
        self.trace = self.directory / 'request-1.bin'
        for name, data in [('model-index.json', b'{}'), ('model-config.json', b'{}'),
                           ('workload.json', b'{"fixture":true}'),
                           ('command.bin', b'q4t\0serve\0'),
                           ('environment.bin', b'')]:
            (self.directory / name).write_bytes(data)
        digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
        self.manifest = dict(schema=1, complete=True, failure='none',
                             requests_started=1, requests_published=1,
                             layers=48, experts=512, top_k=10, max_rows=8192,
                             binary_sha256=digest(self.binary))
        for field, name in [('model_index_sha256', 'model-index.json'),
                            ('model_config_sha256', 'model-config.json'),
                            ('workload_sha256', 'workload.json'),
                            ('command_sha256', 'command.bin'),
                            ('environment_sha256', 'environment.bin')]:
            self.manifest[field] = digest(self.directory / name)
        self.write_manifest()
        tokens = struct.pack('<3I', 12, 34, 56)
        self.trace.with_suffix('.tokens').write_bytes(tokens)
        self.trace.with_suffix('.json').write_text(json.dumps(
            dict(request_id=1, prompt_tokens=3, http_id='synthetic')))
        self.records = [(1, struct.pack('<QQ', 1, 3) + hashlib.sha256(tokens).digest())]
        # Two prefill forwards and two decode forwards, all 48 layers each.
        # Distinct row/rank order makes sorting or Counter reduction detectable.
        for fid, stage, position, rows in [(10, 1, 0, 2), (11, 1, 2, 1),
                                           (12, 2, 3, 1), (13, 2, 4, 1)]:
            self.records.append((2, struct.pack('<QIQI', fid, stage, position, rows)))
            for layer in range(48):
                ids = [(layer * 7 + fid + row * 13 + rank * 3) % 512
                       for row in range(rows) for rank in reversed(range(10))]
                self.records.append((3, struct.pack('<I', layer) +
                                     struct.pack('<' + 'H' * len(ids), *ids)))
            self.records.append((4, b'\1\1\1'))
        self.records.extend([(5, struct.pack('<IQ', 1, 3)), (6, b'')])
        self.write_trace()

    def write_manifest(self):
        (self.directory / 'manifest.json').write_text(json.dumps(self.manifest))

    def write_trace(self):
        header = struct.pack('<5I', 1, 48, 512, 10, 8192) + bytes.fromhex(
            self.manifest['binary_sha256'] + self.manifest['model_index_sha256'] +
            self.manifest['workload_sha256'])
        payload = b'Q4TRTE01' + _frame(header)
        for seq, (kind, body) in enumerate(self.records):
            payload += _frame(struct.pack('<IQ', kind, seq) + body)
        self.trace.write_bytes(payload)

    def validate(self, **kwargs):
        return validate_request(self.trace, checker=CHECKER,
                                source_binary=self.binary, **kwargs)

    def rejected(self):
        with self.assertRaises((ValueError, OSError, KeyError)):
            self.validate()

    def test_full_token_order_and_all_decode(self):
        identity = self.validate()
        rows = list(iter_layers(self.trace, identity))
        self.assertEqual(len(rows), 4 * 48)
        self.assertEqual(identity['summary'], dict(forwards=4, prefill_rows=3,
                         decode_rows=2, route_ids=5 * 48 * 10, output_tokens=3))
        self.assertEqual(list(rows[0]['topk_ids']),
                         [10 + row * 13 + rank * 3 for row in range(2)
                          for rank in reversed(range(10))])
        self.assertEqual([(r['forward_id'], r['position'], r['phase'])
                          for r in rows[::48]],
                         [(10, 0, 'prefill'), (11, 2, 'prefill'),
                          (12, 3, 'decode'), (13, 4, 'decode')])
        self.assertEqual(identity['current_runtime_route_equivalence'], 'NOT_VERIFIED')
        self.assertEqual(rows[0]['topk_ids'].typecode, 'H')

    def test_source_binary_is_bound(self):
        self.binary.write_bytes(b'wrong binary')
        self.rejected()

    def test_expected_trace_sha_is_bound(self):
        with self.assertRaisesRegex(ValueError, 'selected trace SHA'):
            self.validate(expected_sha256='0' * 64)

    def test_token_sha_rejected(self):
        self.trace.with_suffix('.tokens').write_bytes(struct.pack('<3I', 99, 34, 56))
        self.rejected()

    def test_incomplete_manifest_rejected(self):
        self.manifest['complete'] = False
        self.write_manifest()
        self.rejected()

    def test_partial_sibling_rejected(self):
        (self.directory / 'request-2.partial').write_bytes(b'')
        self.rejected()

    def test_missing_sibling_rejected(self):
        self.manifest['requests_started'] = self.manifest['requests_published'] = 2
        self.write_manifest()
        self.rejected()

    def test_failed_request_rejected_before_consumption(self):
        self.records[-2] = (5, struct.pack('<IQ', 3, 3))
        self.write_trace()
        self.rejected()

    def test_uncommitted_forward_rejected(self):
        self.records[50] = (4, b'\1\1\0')
        self.write_trace()
        self.rejected()

    def test_missing_layer_rejected(self):
        del self.records[3]
        self.write_trace()
        self.rejected()

    def test_duplicate_forward_rejected(self):
        self.records[51] = (2, struct.pack('<QIQI', 10, 1, 2, 1))
        self.write_trace()
        self.rejected()

    def test_bad_expert_id_rejected(self):
        kind, body = self.records[2]
        self.records[2] = (kind, body[:4] + struct.pack('<H', 512) + body[6:])
        self.write_trace()
        self.rejected()

    def test_duplicate_topk_id_rejected(self):
        kind, body = self.records[2]
        self.records[2] = (kind, body[:6] + body[4:6] + body[8:])
        self.write_trace()
        self.rejected()

    def test_crc_rejected(self):
        payload = bytearray(self.trace.read_bytes())
        payload[-1] ^= 1
        self.trace.write_bytes(payload)
        self.rejected()

    def test_mutation_after_validation_rejected(self):
        identity = self.validate()
        self.trace.with_suffix('.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'source changed'):
            next(iter_layers(self.trace, identity))

    def test_selected_request_metadata_bound(self):
        self.trace.with_suffix('.json').write_text(json.dumps(
            dict(request_id=2, prompt_tokens=3, http_id='wrong')))
        self.rejected()

    def test_missing_run_end_rejected(self):
        self.records.pop()
        self.write_trace()
        self.rejected()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checker', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=OUTPUT)
    args, rest = parser.parse_known_args()
    CHECKER, OUTPUT = args.checker.resolve(), args.output.resolve()
    if not any(OUTPUT.is_relative_to(ROOT / p) for p in ['build', '.q4t-work']):
        parser.error('output must be below build/ or .q4t-work/')
    unittest.main(argv=[__file__, *rest])
