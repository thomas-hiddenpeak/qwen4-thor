"""Independent little-endian/CRC fixtures for the C++ trace checker."""
import argparse
import json
from pathlib import Path
import struct
import subprocess
import zlib

ROOT = Path(__file__).resolve().parents[2]


def frame(payload):
    return struct.pack('<II', len(payload), zlib.crc32(payload)) + payload


def header(version=1, layers=2, experts=8, top_k=2, rows=4):
    return b'Q4TRTE01' + frame(struct.pack('<IIIII', version, layers, experts,
                                         top_k, rows) + bytes(range(1, 97)))


def record(kind, sequence, payload=b''):
    return frame(struct.pack('<IQ', kind, sequence) + payload)


def records(outcome=1):
    return [
        record(1, 0, struct.pack('<QQ', 1, 3) + bytes(range(32))),
        record(2, 1, struct.pack('<QIQI', 1, 1, 0, 3)),
        record(3, 2, struct.pack('<I6H', 0, 0, 7, 3, 2, 1, 4)),
        record(3, 3, struct.pack('<I6H', 1, 0, 7, 3, 2, 1, 4)),
        record(4, 4, b'\1\1\1'),
        record(5, 5, struct.pack('<IQ', outcome, 2)),
        record(6, 6),
    ]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--checker', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    out = args.output.resolve()
    if not any(out.is_relative_to(ROOT / p) for p in ['build', '.q4t-work']):
        ap.error('output must be under build/ or .q4t-work/')
    out.mkdir(parents=True, exist_ok=True)
    # Keep each CTest rerun separate; never overwrite a failed run.
    import tempfile
    run = Path(tempfile.mkdtemp(prefix='run-', dir=out))
    checker = str(args.checker.resolve())
    results = []

    def check(name, data, expected):
        path = run / (name + '.bin')
        path.write_bytes(data)
        result = subprocess.run([checker, str(path)], capture_output=True,
                                text=True, timeout=10)
        results.append(dict(name=name, exit=result.returncode,
                            expected_exit=expected, stdout=result.stdout,
                            stderr=result.stderr))
        assert result.returncode == expected, results[-1]
        if expected in (0, 3):
            return json.loads(result.stdout)
        assert result.stdout == '', results[-1]

    try:
        good = header() + b''.join(records())
        result = check('independent-golden', good, 0)
        assert result == dict(structurally_complete=True, requests=1,
                              successful_requests=1, cancelled_requests=0,
                              failed_requests=0, committed_prefill_rows=3,
                              committed_decode_rows=0, route_ids=12,
                              output_tokens=2)
        # Decode rows and output token count are deliberately independent.
        # Failed/cancelled requests are diagnostic, not successful samples.
        for outcome in (2, 3):
            check('outcome-' + str(outcome),
                  header() + b''.join(records(outcome)), 3)
        # A successful sample cannot hide a later cancelled request.
        parts = records()[:-1]
        parts.extend([
            record(1, 6, struct.pack('<QQ', 2, 3) + bytes(range(32))),
            record(5, 7, struct.pack('<IQ', 2, 0)),
            record(6, 8),
        ])
        result = check('mixed-success-cancel', header() + b''.join(parts), 3)
        assert result['successful_requests'] == result['cancelled_requests'] == 1
        for name, data in [
            ('empty', b''), ('magic-only', b'Q4TRTE01'),
            ('empty-run', header() + record(6, 0)),
            ('header-version', header(version=2)),
            ('header-zero-layers', header(layers=0)),
            ('header-zero-rows', header(rows=0)),
            ('header-expert-overflow', header(experts=65537)),
            ('header-topk-overflow', header(top_k=17)),
            ('header-topk-experts', header(experts=1)),
            ('header-rows-overflow', header(rows=0xffffffff)),
            ('no-run-end', good[:-20]),
            ('trailing-data', good + b'x'),
            ('concatenated-runs', good + good),
            ('frame-allocation-overflow', header() + b'\xff' * 8),
            ('frame-zero-size', header() + b'\0' * 8),
        ]:
            check(name, data, 1)
        mutations = [
            ('unknown-record', 0, record(99, 0)),
            ('event-gap', 1, record(2, 2, struct.pack('<QIQI', 1, 1, 0, 3))),
            ('event-repeat', 1, record(2, 0, struct.pack('<QIQI', 1, 1, 0, 3))),
            ('position-gap', 1, record(2, 1, struct.pack('<QIQI', 1, 1, 1, 3))),
            ('unknown-stage', 1, record(2, 1, struct.pack('<QIQI', 1, 9, 0, 3))),
            ('decode-before-prefill', 1,
             record(2, 1, struct.pack('<QIQI', 1, 2, 0, 1))),
            ('zero-forward-rows', 1,
             record(2, 1, struct.pack('<QIQI', 1, 1, 0, 0))),
            ('missing-layer', 2, record(3, 2, struct.pack('<I6H', 1, *range(6)))),
            ('duplicate-layer', 3, record(3, 3, struct.pack('<I6H', 0, *range(6)))),
            ('expert-out-of-range', 2,
             record(3, 2, struct.pack('<I6H', 0, 0, 8, 1, 2, 3, 4))),
            ('duplicate-expert', 2,
             record(3, 2, struct.pack('<I6H', 0, 0, 0, 1, 2, 3, 4))),
            ('wrong-id-count', 2, record(3, 2, struct.pack('<I2H', 0, 1, 2))),
            ('odd-id-bytes', 2, record(3, 2, struct.pack('<I', 0) + b'x')),
            ('commit-no-submit', 4, record(4, 4, b'\0\1\1')),
            ('commit-no-completion', 4, record(4, 4, b'\1\0\1')),
            ('noncanonical-bool', 4, record(4, 4, b'\1\2\1')),
            ('uncommitted-success', 4, record(4, 4, b'\1\1\0')),
            ('unknown-outcome', 5, record(5, 5, struct.pack('<IQ', 0, 2))),
            ('extra-event-field', 6, record(6, 6, b'x')),
        ]
        for name, index, replacement in mutations:
            parts = records()
            parts[index] = replacement
            check(name, header() + b''.join(parts), 1)
        # Valid CRC around malformed data ensures semantic checks, not merely
        # checksum checks, reject the independent mutations above.
        damaged = bytearray(good)
        damaged[180] ^= 1
        check('crc-damage', damaged, 1)
        for size in (1, 7, 8, 15, 16, 100, len(good) - 1):
            check('truncated-' + str(size), good[:size], 1)
        for argv in ([], ['a', 'b'], [str(run / 'absent.bin')]):
            result = subprocess.run([checker, *argv], capture_output=True,
                                    text=True, timeout=10)
            expected = 1 if len(argv) == 1 else 2
            results.append(dict(name='arguments-' + str(len(argv)),
                                exit=result.returncode, expected_exit=expected))
            assert result.returncode == expected
    finally:
        (run / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
    assert results
    print(f'{len(results)} independent wire/CLI cases passed; evidence: {run}')


if __name__ == '__main__':
    main()
