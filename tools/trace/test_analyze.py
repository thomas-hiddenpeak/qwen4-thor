"""Independent capture-package identity and failure classification checks."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import struct
import tempfile
import zlib

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('trace_analyze', Path(__file__).with_name('analyze.py'))
analyzer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analyzer)


def frame(data):
    return struct.pack('<II', len(data), zlib.crc32(data)) + data


def fixture(out):
    out.mkdir()
    binary = out / 'binary'
    binary.write_bytes(b'synthetic binary identity')
    (out / 'model-index.json').write_text('{}')
    (out / 'workload.json').write_text('{"synthetic":true}')
    (out / 'model-config.json').write_text('{}')
    (out / 'command.bin').write_bytes(b'q4t\0serve\0')
    (out / 'environment.bin').write_bytes(b'')
    digest = lambda name: hashlib.sha256((out / name).read_bytes()).hexdigest()
    manifest = dict(schema=1, complete=True, failure='none', requests_started=1,
        requests_published=1, layers=2, experts=8, top_k=2, max_rows=4,
        binary_sha256=digest('binary'), model_index_sha256=digest('model-index.json'),
        workload_sha256=digest('workload.json'), model_config_sha256=digest('model-config.json'),
        command_sha256=digest('command.bin'), environment_sha256=digest('environment.bin'))
    (out / 'manifest.json').write_text(json.dumps(manifest))
    tokens = struct.pack('<3I', 101, 202, 303)
    (out / 'request-1.tokens').write_bytes(tokens)
    (out / 'request-1.json').write_text(json.dumps(dict(request_id=1, http_id='test', prompt_tokens=3)))
    data = b'Q4TRTE01' + frame(struct.pack('<5I', 1, 2, 8, 2, 4) + bytes.fromhex(
        manifest['binary_sha256'] + manifest['model_index_sha256'] + manifest['workload_sha256']))
    records = [(1, struct.pack('<QQ', 1, 3) + hashlib.sha256(tokens).digest()),
               (2, struct.pack('<QIQI', 1, 1, 0, 3)),
               (3, struct.pack('<I6H', 0, 0, 7, 3, 2, 1, 4)),
               (3, struct.pack('<I6H', 1, 0, 7, 3, 2, 1, 4)),
               (4, b'\1\1\1'), (5, struct.pack('<IQ', 1, 2)), (6, b'')]
    for sequence, (kind, payload) in enumerate(records):
        data += frame(struct.pack('<IQ', kind, sequence) + payload)
    (out / 'request-1.bin').write_bytes(data)
    return binary


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--checker', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    output = args.output.resolve()
    assert any(output.is_relative_to(ROOT / p) for p in ['build', '.q4t-work'])
    output.mkdir(parents=True, exist_ok=True)
    out = Path(tempfile.mkdtemp(prefix='run-', dir=output))
    source = out / 'valid'
    binary = fixture(source)
    result = analyzer.analyze(source, args.checker.resolve(), binary)
    assert result['successful_requests'] == 1 and result['excluded_requests'] == 0
    assert len(result['coverage']) == 2
    assert all(x['route_ids'] == 6 and x['active_experts'] == 6 for x in result['coverage'])
    results = [dict(name='valid', passed=True)]
    for name in ['incomplete', 'failure', 'empty', 'missing-request', 'tokens',
                 'binary', 'model-index', 'workload', 'request-id', 'partial', 'corrupt',
                 'model-config', 'command', 'environment']:
        case = out / name
        shutil.copytree(source, case)
        manifest = json.loads((case / 'manifest.json').read_text())
        if name == 'incomplete': manifest['complete'] = False
        if name == 'failure': manifest['failure'] = 'queue_full'
        if name == 'empty':
            (case / 'request-1.bin').unlink()
            manifest['requests_published'] = manifest['requests_started'] = 0
        if name == 'missing-request': manifest['requests_started'] = 2
        if name in ('tokens', 'binary', 'model-index', 'workload', 'model-config', 'command', 'environment'):
            path = case / {'tokens': 'request-1.tokens', 'binary': 'binary',
                           'model-index': 'model-index.json', 'workload': 'workload.json',
                           'model-config': 'model-config.json', 'command': 'command.bin',
                           'environment': 'environment.bin'}[name]
            path.write_bytes(path.read_bytes() + b'x')
        if name == 'request-id':
            (case / 'request-1.json').write_text('{"request_id":2,"http_id":"test","prompt_tokens":3}')
        if name == 'partial': (case / 'request-2.partial').write_bytes(b'')
        if name == 'corrupt':
            path = case / 'request-1.bin'
            data = bytearray(path.read_bytes()); data[-1] ^= 1; path.write_bytes(data)
        (case / 'manifest.json').write_text(json.dumps(manifest))
        try:
            analyzer.analyze(case, args.checker.resolve(), case / 'binary')
        except ValueError as error:
            results.append(dict(name=name, passed=True, rejection=str(error)))
        else:
            raise AssertionError('bad package accepted: ' + name)
    (out / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
    print(f'{len(results)} capture analysis checks passed; evidence: {out}')


if __name__ == '__main__':
    main()
