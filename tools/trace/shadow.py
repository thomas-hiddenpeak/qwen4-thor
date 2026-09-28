"""Bounded request-completion shadow observer; never controls inference."""
import argparse
from collections import Counter
import json
from pathlib import Path
import resource
import struct
import subprocess
import sys
import time

from analyze import frames, sha
from replay import Cache, EXPERT_PAYLOAD, LAYERS, SLOT_BYTES

IDENTITIES = {'model-index.json': 'model_index_sha256',
              'model-config.json': 'model_config_sha256',
              'workload.json': 'workload_sha256',
              'command.bin': 'command_sha256',
              'environment.bin': 'environment_sha256'}


def manifest_identity(directory, binary):
    manifest = json.loads((directory / 'manifest.json').read_text())
    if (manifest['schema'], manifest['layers'], manifest['experts'],
            manifest['top_k'], manifest['failure']) != (1, 48, 512, 10, 'none'):
        raise ValueError('unsupported or failed capture')
    if sha(binary) != manifest['binary_sha256']:
        raise ValueError('binary mismatch')
    for filename, field in IDENTITIES.items():
        if sha(directory / filename) != manifest[field]:
            raise ValueError('source mismatch: ' + filename)
    config = json.loads((directory / 'model-config.json').read_text())['text_config']
    if (config['hidden_size'], config['moe_intermediate_size']) != (2560, 640):
        raise ValueError('unsupported expert layout')
    return manifest


def committed_groups(path, expected, request_id, previous_forward):
    """Streaming source adapter, only yield after checked forward commit."""
    events = frames(path)
    header = next(events)
    if struct.unpack('<5I', header[:20]) != (
            1, 48, 512, 10, expected['max_rows']):
        raise ValueError('header dimensions mismatch')
    if header[20:].hex() != ''.join(expected[k] for k in (
            'binary_sha256', 'model_index_sha256', 'workload_sha256')):
        raise ValueError('header identity mismatch')
    metadata = json.loads(path.with_suffix('.json').read_text())
    pending = []
    for payload in events:
        kind = struct.unpack('<I', payload[:4])[0]
        body = payload[12:]
        if kind == 1:
            rid, rows = struct.unpack('<QQ', body[:16])
            tokens = path.with_suffix('.tokens')
            if (rid != request_id or rid != metadata['request_id'] or
                    rows != metadata['prompt_tokens'] or
                    tokens.stat().st_size != rows * 4 or
                    sha(tokens) != body[16:].hex()):
                raise ValueError('request/input identity mismatch')
        elif kind == 2:
            fid, stage, position, rows = struct.unpack('<QIQI', body)
            if fid <= previous_forward[0]:
                raise ValueError('forward order reused')
            previous_forward[0] = fid
            pending = []
        elif kind == 3:
            layer = struct.unpack('<I', body[:4])[0]
            values = struct.iter_unpack('<H', body[4:])
            pending.append((stage, layer, Counter(x[0] for x in values)))
        elif kind == 4:
            if body == b'\1\1\1':
                yield from pending
            pending = []


class Shadow:
    def __init__(self, ranks):
        self.ranks = ranks
        self.caches = {}
        self.request_count = 0

    def consume(self, groups):
        rows = {}
        initial = {}
        for capacity in (32, 64):
            for policy in ('static', 'lru'):
                for mode in ('prefill_reset', 'continuous'):
                    key = (capacity, policy, mode)
                    if key not in self.caches or mode == 'prefill_reset':
                        self.caches[key] = [Cache(capacity, policy, rank)
                                            for rank in self.ranks]
                        initial[key] = sum(c.initial for c in self.caches[key]) * SLOT_BYTES
                    else:
                        initial[key] = 0
                    rows[key] = {}
        for stage, layer, counts in groups:
            for key, caches in self.caches.items():
                value = caches[layer].consume(counts, stage)
                bucket = rows[key].setdefault((stage, layer), dict(
                    stage=stage, layer=layer, missing_histogram={},
                    **{k: 0 for k in value}))
                for k, v in value.items():
                    bucket[k] = (max(bucket[k], v) if k == 'occupancy_bytes'
                                 else bucket[k] + v)
                hist = bucket['missing_histogram']
                hist[value['misses']] = hist.get(value['misses'], 0) + 1
        self.request_count += 1
        return [dict(capacity=k[0], policy=k[1], mode=k[2],
                     initial_bytes=initial[k],
                     total_budget_bytes=(48 * k[0] + 10) * SLOT_BYTES,
                     layers=list(v.values())) for k, v in rows.items()]


def observe(directory, binary, checker, ranks, output, deadline, max_requests,
            model_digest=None):
    start = time.monotonic()
    status = dict(complete=False, scope='request-completion shadow, no control',
                  requests=0, saw_open_run=False, backlog_peak=0,
                  slot_bytes=SLOT_BYTES, payload_bytes=EXPERT_PAYLOAD)
    output.mkdir(parents=True, exist_ok=False)
    written = 0
    try:
        while not (directory / 'environment.bin').exists():
            if time.monotonic() - start > deadline:
                raise ValueError('source startup deadline')
            time.sleep(.25)
        # Wait for the post-metadata manifest: startup first writes zero hashes.
        while True:
            m = json.loads((directory / 'manifest.json').read_text())
            if m.get('environment_sha256') != '0' * 64:
                break
            if time.monotonic() - start > deadline:
                raise ValueError('manifest startup deadline')
            time.sleep(.25)
        expected = manifest_identity(directory, binary)
        if model_digest is not None and expected['model_index_sha256'] != model_digest:
            raise ValueError('calibration model mismatch')
        if expected['complete']:
            raise ValueError('source already complete; not an online observation')
        status['saw_open_run'] = True
        status['source'] = expected
        shadow, previous = Shadow(ranks), [0]
        while True:
            if time.monotonic() - start > deadline:
                raise ValueError('observer deadline')
            m = json.loads((directory / 'manifest.json').read_text())
            if m['failure'] != 'none':
                raise ValueError('capture stopped: ' + m['failure'])
            for field in ['binary_sha256', *IDENTITIES.values()]:
                if m[field] != expected[field]:
                    raise ValueError('source identity changed')
            paths = list(directory.glob('request-*.bin'))
            status['backlog_peak'] = max(status['backlog_peak'], len(paths) - status['requests'])
            rid = status['requests'] + 1
            path = directory / f'request-{rid}.bin'
            if path.exists():
                if rid > max_requests or path.stat().st_size > 256 * 1024 * 1024:
                    raise ValueError('request/file bound exceeded')
                before = time.monotonic()
                digest = sha(path)
                check = subprocess.run([str(checker), str(path)], capture_output=True,
                                       text=True, timeout=min(120, max(1, deadline - (before - start))))
                if check.returncode not in (0, 3):
                    raise ValueError('trace checker rejected: ' + check.stderr)
                verified = json.loads(check.stdout)
                if verified['requests'] != 1:
                    raise ValueError('expected one request')
                record = dict(request_id=rid, trace_sha256=digest,
                              metadata=json.loads(path.with_suffix('.json').read_text()),
                              verified=verified, source_complete_when_observed=m['complete'],
                              experiments=shadow.consume(committed_groups(path, expected, rid, previous)))
                if sha(path) != digest:
                    raise ValueError('published trace changed during processing')
                record['processing_seconds'] = time.monotonic() - before
                record['observer_elapsed_seconds'] = time.monotonic() - start
                data = json.dumps(record, separators=(',', ':')) + '\n'
                written += len(data.encode())
                if written > 128 * 1024 * 1024:
                    raise ValueError('output bound exceeded')
                with (output / f'request-{rid}.json').open('x') as file:
                    file.write(data)
                status['requests'] = rid
                continue
            if m['complete']:
                if (not status['requests'] or m['requests_started'] != status['requests'] or
                        m['requests_published'] != status['requests'] or
                        len(paths) != status['requests'] or list(directory.glob('request-*.partial'))):
                    raise ValueError('empty/gapped/incomplete request sequence')
                manifest_identity(directory, binary)
                status['complete'] = True
                status['final_manifest_sha256'] = sha(directory / 'manifest.json')
                break
            time.sleep(.25)
    except BaseException as error:
        status['failure'] = repr(error)
        raise
    finally:
        status['elapsed_seconds'] = time.monotonic() - start
        status['peak_rss_kib'] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        status['output_bytes'] = written
        status['tool_sha256'] = sha(Path(__file__))
        (output / 'status.json').write_text(json.dumps(status, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--checker', type=Path, required=True)
    parser.add_argument('--calibration', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--deadline', type=int, default=7200)
    parser.add_argument('--max-requests', type=int, default=64)
    args = parser.parse_args()
    try:
        root = Path(__file__).resolve().parents[2]
        output = args.output.resolve()
        if not any(output.is_relative_to(root / p) for p in ('build', '.q4t-work')):
            raise ValueError('output must be in build/ or .q4t-work/')
        if not 1 <= args.max_requests <= 64 or not 1 <= args.deadline <= 7200:
            raise ValueError('invalid observer bounds')
        if args.calibration.stat().st_size > 1024 * 1024:
            raise ValueError('calibration exceeds 1 MiB; export compact frozen rankings')
        calibration = json.loads(args.calibration.read_text())
        ranks = calibration['rankings']
        if len(ranks) != LAYERS:
            raise ValueError('invalid calibration layers')
        for rank in ranks:
            Cache(32, 'static', rank)
        observe(args.directory.resolve(), args.binary.resolve(), args.checker.resolve(),
                ranks, output, args.deadline, args.max_requests,
                calibration['model_index_sha256'])
        (output / 'calibration.json').write_text(json.dumps(dict(
            source=str(args.calibration.resolve()), sha256=sha(args.calibration),
            rankings=ranks), indent=2) + '\n')
    except (ValueError, OSError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
        print('shadow rejected:', error, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
