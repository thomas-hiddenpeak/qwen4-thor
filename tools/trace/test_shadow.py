"""Bounded observer state and atomic-publication lifecycle contracts."""
from collections import Counter
import argparse
import hashlib
import sys
import json
from pathlib import Path
import struct
import tempfile
import threading
import time
import unittest
import zlib
from shadow import Shadow, observe

ROOT = Path(__file__).resolve().parents[2]
CHECKER = None
CONTRACT_DIR = ROOT / 'build/shadow-contracts'
RANKS = [list(range(512)) for _ in range(48)]


def frame(data):
    return struct.pack('<II', len(data), zlib.crc32(data)) + data


def fixture(path, outcome=1, failed_forward=False):
    path.mkdir()
    for name, data in [('binary', b'test'), ('model-index.json', b'{}'),
                       ('workload.json', b'{}'), ('command.bin', b'q4t\0'),
                       ('environment.bin', b''), ('model-config.json', json.dumps(
                           {'text_config': {'hidden_size': 2560,
                                            'moe_intermediate_size': 640}}).encode())]:
        (path / name).write_bytes(data)
    digest = lambda name: hashlib.sha256((path / name).read_bytes()).hexdigest()
    m = dict(schema=1, complete=False, failure='none', layers=48, experts=512,
             top_k=10, max_rows=1, requests_started=0, requests_published=0,
             binary_sha256=digest('binary'), model_index_sha256=digest('model-index.json'),
             model_config_sha256=digest('model-config.json'), workload_sha256=digest('workload.json'),
             command_sha256=digest('command.bin'), environment_sha256=digest('environment.bin'))
    (path / 'manifest.json').write_text(json.dumps(m))
    token = struct.pack('<I', 1)
    (path / 'request-1.tokens').write_bytes(token)
    (path / 'request-1.json').write_text(json.dumps(dict(request_id=1, prompt_tokens=1, http_id='test')))
    data = b'Q4TRTE01' + frame(struct.pack('<5I', 1, 48, 512, 10, 1) + bytes.fromhex(
        m['binary_sha256'] + m['model_index_sha256'] + m['workload_sha256']))
    records = [(1, struct.pack('<QQ', 1, 1) + hashlib.sha256(token).digest())]
    for fid, stage, position in [(1, 1, 0), (2, 2, 1)]:
        records.append((2, struct.pack('<QIQI', fid, stage, position, 1)))
        records += [(3, struct.pack('<I10H', layer, *range(10))) for layer in range(48)]
        records.append((4, b'\1\1\0' if failed_forward and stage == 2 else b'\1\1\1'))
    records += [(5, struct.pack('<IQ', outcome, 1 if failed_forward else 2)), (6, b'')]
    for seq, (kind, payload) in enumerate(records):
        data += frame(struct.pack('<IQ', kind, seq) + payload)
    (path / 'request-1.partial').write_bytes(data)
    return m


class Contracts(unittest.TestCase):
    def test_whole_request_and_reset(self):
        shadow = Shadow(RANKS)
        groups = [(1, 0, Counter(range(512))), (2, 0, Counter(range(10)))]
        first, second = shadow.consume(iter(groups)), shadow.consume(iter(groups))
        def row(rows, mode):
            x = next(x for x in rows if (x['capacity'], x['policy'], x['mode']) == (32, 'lru', mode))
            return next(x for x in x['layers'] if x['stage'] == 2)
        self.assertEqual(row(first, 'continuous')['misses'], 10)
        self.assertEqual(row(second, 'continuous')['misses'], 0)
        self.assertEqual(row(second, 'prefill_reset')['misses'], 10)

    def test_full_decode_no_64_truncation(self):
        result = Shadow(RANKS).consume((2, 0, Counter(range(10))) for _ in range(200))
        self.assertTrue(all(x['layers'][0]['groups'] == 200 for x in result))

    def run_case(self, case):
        parent = CONTRACT_DIR
        parent.mkdir(parents=True, exist_ok=True)
        root = Path(tempfile.mkdtemp(prefix=case + '-', dir=parent))
        source, output = root / 'source', root / 'output'
        m = fixture(source, outcome={'cancelled': 2, 'failed-forward': 3}.get(case, 1),
                    failed_forward=case == 'failed-forward')
        if case == 'already-complete':
            m['complete'] = True
            (source / 'manifest.json').write_text(json.dumps(m))
        def publish():
            time.sleep(.4)
            if case == 'deadline' or case == 'already-complete':
                return
            if case == 'oversized-file':
                with (source / 'request-1.partial').open('r+b') as file:
                    file.truncate(256 * 1024 * 1024 + 1)
            if case == 'corrupt':
                p = source / 'request-1.partial'
                p.write_bytes(p.read_bytes()[:-1] + b'x')
            if case == 'source-failure':
                m['failure'] = 'queue_full'
            elif case != 'empty':
                (source / 'request-1.partial').rename(source / 'request-1.bin')
                m['requests_started'] = m['requests_published'] = 1
            else:
                (source / 'request-1.partial').unlink()
            time.sleep(.5)
            m['complete'] = case != 'source-failure'
            if case == 'gap':
                m['requests_started'] = 2
            temp = source / 'manifest.partial'
            temp.write_text(json.dumps(m))
            temp.replace(source / 'manifest.json')
        worker = threading.Thread(target=publish)
        worker.start()
        try:
            if case in ('valid', 'cancelled', 'failed-forward'):
                observe(source, source / 'binary', CHECKER, RANKS, output, 3, 64)
                self.assertTrue(json.loads((output / 'status.json').read_text())['complete'])
                row = json.loads((output / 'request-1.json').read_text())
                self.assertFalse(row['source_complete_when_observed'])
                if case == 'failed-forward':
                    self.assertTrue(all(x['stage'] == 1 for x in row['experiments'][0]['layers']))
                if case == 'cancelled':
                    self.assertEqual(row['verified']['cancelled_requests'], 1)
            else:
                with self.assertRaises(ValueError):
                    observe(source, source / 'binary', CHECKER, RANKS, output, 1 if case == 'deadline' else 3,
                            0 if case == 'request-bound' else 64)
                self.assertFalse(json.loads((output / 'status.json').read_text())['complete'])
        finally:
            worker.join()

    def test_live_publication(self): self.run_case('valid')
    def test_cancelled_prefix(self): self.run_case('cancelled')
    def test_failed_forward_not_consumed(self): self.run_case('failed-forward')
    def test_file_bound(self): self.run_case('oversized-file')
    def test_request_bound(self): self.run_case('request-bound')
    def test_already_complete(self): self.run_case('already-complete')
    def test_corrupt(self): self.run_case('corrupt')
    def test_empty(self): self.run_case('empty')
    def test_gap(self): self.run_case('gap')
    def test_source_failure(self): self.run_case('source-failure')
    def test_deadline(self): self.run_case('deadline')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--checker', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=CONTRACT_DIR)
    args, remaining = parser.parse_known_args()
    CONTRACT_DIR = args.output.resolve()
    CHECKER = args.checker.resolve()
    unittest.main(argv=[sys.argv[0], *remaining])
