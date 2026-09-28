"""Validate a controlled capture and report observed expert coverage only.

No cache or throughput estimate. Requests with failed/cancelled outcomes remain
visible and are excluded from aggregate coverage. All input files are read-only.
"""
import argparse
from array import array
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys
import zlib


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as file:
        for block in iter(lambda: file.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def frames(path):
    with path.open('rb') as file:
        if file.read(8) != b'Q4TRTE01':
            raise ValueError('bad trace magic')
        while True:
            prefix = file.read(8)
            if not prefix:
                break
            if len(prefix) != 8:
                raise ValueError('truncated frame')
            size, crc = struct.unpack('<II', prefix)
            if not 0 < size <= 16 * 1024 * 1024:
                raise ValueError('invalid frame length')
            payload = file.read(size)
            if len(payload) != size or zlib.crc32(payload) != crc:
                raise ValueError('truncated/corrupt payload')
            yield payload


def analyze(directory, checker, binary):
    manifest = json.loads((directory / 'manifest.json').read_text())
    if manifest['schema'] != 1 or not manifest['complete']:
        raise ValueError('run incomplete: ' + manifest.get('failure', 'unknown'))
    if manifest['failure'] != 'none':
        raise ValueError('run has capture failure')
    if sha(binary) != manifest['binary_sha256']:
        raise ValueError('binary identity mismatch')
    if sha(directory / 'model-index.json') != manifest['model_index_sha256']:
        raise ValueError('model index identity mismatch')
    if sha(directory / 'workload.json') != manifest['workload_sha256']:
        raise ValueError('workload identity mismatch')
    for field, filename in [('model_config_sha256', 'model-config.json'),
                            ('command_sha256', 'command.bin'),
                            ('environment_sha256', 'environment.bin')]:
        if field in manifest and sha(directory / filename) != manifest[field]:
            raise ValueError(filename + ' identity mismatch')
    if list(directory.glob('request-*.partial')):
        raise ValueError('unfinished request files')
    paths = sorted(directory.glob('request-*.bin'),
                   key=lambda p: int(p.stem.split('-')[-1]))
    if (not paths or len(paths) != manifest['requests_published'] or
            len(paths) != manifest['requests_started']):
        raise ValueError('empty run or missing requests')
    totals = defaultdict(lambda: [0] * manifest['experts'])
    requests = []
    previous_request = previous_forward = 0
    for path in paths:
        check = subprocess.run([str(checker), str(path)], capture_output=True,
                               text=True, timeout=120)
        if check.returncode not in (0, 3):
            raise ValueError(f'{path.name}: {check.stderr}')
        verified = json.loads(check.stdout)
        if verified['requests'] != 1:
            raise ValueError('collector files must contain exactly one request')
        events = frames(path)
        header = next(events)
        dims = struct.unpack('<IIIII', header[:20])
        if dims != (1, manifest['layers'], manifest['experts'],
                    manifest['top_k'], manifest['max_rows']):
            raise ValueError('header dimensions mismatch')
        if header[20:] != bytes.fromhex(manifest['binary_sha256'] +
                manifest['model_index_sha256'] + manifest['workload_sha256']):
            raise ValueError('header identities mismatch')
        counts = defaultdict(lambda: [0] * manifest['experts'])
        metadata = json.loads(path.with_suffix('.json').read_text())
        outcome = None
        forward_count = 0
        pending = []
        for payload in events:
            kind, _ = struct.unpack('<IQ', payload[:12])
            data = payload[12:]
            if kind == 1:
                request_id, prompt_rows = struct.unpack('<QQ', data[:16])
                if (request_id <= previous_request or request_id != metadata['request_id']
                        or prompt_rows != metadata['prompt_tokens']):
                    raise ValueError('request identity mismatch')
                previous_request = request_id
                tokens = path.with_suffix('.tokens')
                if tokens.stat().st_size != prompt_rows * 4 or sha(tokens) != data[16:].hex():
                    raise ValueError('actual input token identity mismatch')
            elif kind == 2:
                forward_id, stage, position, rows = struct.unpack('<QIQI', data)
                if forward_id <= previous_forward:
                    raise ValueError('forward identity reused across requests')
                previous_forward = forward_id
                pending = []
                forward_count += 1
            elif kind == 3:
                layer = struct.unpack('<I', data[:4])[0]
                ids = array('H')
                ids.frombytes(data[4:])
                if sys.byteorder != 'little':
                    ids.byteswap()
                histogram = [0] * manifest['experts']
                for expert in ids:
                    histogram[expert] += 1
                pending.append(((stage, layer), histogram))
            elif kind == 4:
                if data == b'\1\1\1':
                    for key, histogram in pending:
                        for expert, hits in enumerate(histogram):
                            counts[key][expert] += hits
                pending = []
            elif kind == 5:
                outcome = struct.unpack('<IQ', data)[0]
        if outcome is None:
            raise ValueError('missing outcome')
        if outcome == 1:
            for key, histogram in counts.items():
                for expert, hits in enumerate(histogram):
                    totals[key][expert] += hits
        requests.append(dict(file=path.name, sha256=sha(path), **metadata,
                             outcome={1: 'success', 2: 'cancelled', 3: 'failed'}[outcome],
                             forwards=forward_count, verified=verified))
    coverage = []
    for (stage, layer), histogram in sorted(totals.items()):
        hits = sum(histogram)
        ordered = sorted(histogram, reverse=True)
        coverage.append(dict(stage={1: 'prefill', 2: 'decode'}[stage], layer=layer,
            route_ids=hits, active_experts=sum(v > 0 for v in histogram),
            # Same-sample ranking is descriptive, not held-out cache coverage.
            same_sample_top_n={str(n): sum(ordered[:n]) / hits
                              for n in (10, 20, 32, 64, 128, 256, 384, 512)
                              if n <= manifest['experts']}, counts=histogram))
    return dict(scope='observed full-request routes; no cache or throughput claim',
                manifest=manifest, requests=requests, coverage=coverage,
                successful_requests=sum(r['outcome'] == 'success' for r in requests),
                excluded_requests=sum(r['outcome'] != 'success' for r in requests))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--directory', type=Path, required=True)
    ap.add_argument('--checker', type=Path, required=True)
    ap.add_argument('--binary', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    try:
        result = analyze(args.directory.resolve(), args.checker.resolve(), args.binary.resolve())
    except (ValueError, OSError, KeyError, StopIteration, subprocess.TimeoutExpired) as error:
        print(f'trace analysis rejected: {error}', file=sys.stderr)
        return 1
    # Never overwrite earlier analyses, and do not alter the input capture.
    root = Path(__file__).resolve().parents[2]
    output = args.output.resolve()
    if not any(output.is_relative_to(root / p) for p in ['build', '.q4t-work']):
        ap.error('output must be under build/ or .q4t-work/')
    with output.open('x') as file:
        json.dump(result, file, indent=2)
        file.write('\n')
    print(json.dumps({k: result[k] for k in ['successful_requests', 'excluded_requests']}))
    return 0 if result['successful_requests'] and not result['excluded_requests'] else 3


if __name__ == '__main__':
    sys.exit(main())
