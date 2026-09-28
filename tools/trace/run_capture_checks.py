"""Real HTTP trace lifecycle/failure checks; no performance acceptance claim."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
from http.client import HTTPConnection, HTTPException
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import threading
import time

ROOT = Path(__file__).resolve().parents[2]


def save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--binary', type=Path, required=True)
    ap.add_argument('--checker', type=Path, required=True)
    ap.add_argument('--quality-run', type=Path, required=True)
    ap.add_argument('--performance-run', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--case', action='append', help='Run named cases; unknown/empty selection fails')
    args = ap.parse_args()
    out = args.output.resolve()
    assert any(out.is_relative_to(ROOT / p) for p in ['build', '.q4t-work'])
    out.mkdir(parents=True, exist_ok=False)
    sources = out / 'source'
    sources.mkdir()
    shutil.copy2(__file__, sources / Path(__file__).name)
    binary, checker = args.binary.resolve(), args.checker.resolve()
    prior = args.quality_run.resolve()
    results = json.loads((prior / 'results.json').read_text())
    inputs = [json.loads(x) for x in (prior / 'inputs/requests.jsonl').read_text().splitlines()]
    fixtures = {}
    for name, length in [('short', 1024), ('long', 45056)]:
        expected = next(r for r in results if r['actual_input'] == length)
        fixture = next(r for r in inputs if hashlib.sha256(r['prompt'].encode()).hexdigest() == expected['prompt_sha256'])
        fixtures[name] = dict(prompt=fixture['prompt'], expected=expected['text'])
    fixtures['decode'] = dict(prompt=json.loads((args.performance_run /
        'inputs/context-1024.jsonl').read_text().splitlines()[0])['prompt'])
    save(out / 'workload.json', fixtures)
    base = json.loads((prior / 'server-command.json').read_text())['argv']
    base[0] = str(binary)
    port = 18081
    for key, value in [('--port', str(port)), ('--max-len', '65536')]:
        base[base.index(key) + 1] = value
    libraries = {}
    for name, source in [('writer', ROOT / 'tools/trace/writer_fault.cpp'),
                         ('pool', ROOT / 'tools/trace/pool_fault.cpp'),
                         ('readback', ROOT / 'tools/verify/readback_fault.cpp.in'),
                         ('fallback', ROOT / 'tools/verify/scheduler_alloc_fault.cpp.in')]:
        frozen_source = sources / source.name
        shutil.copy2(source, frozen_source)
        library = out / (name + '.so')
        with (out / (name + '-build.log')).open('w') as log:
            subprocess.run(['g++-14', '-std=c++23', '-Wall', '-Wextra', '-Werror',
                '-shared', '-fPIC', '-I/usr/local/cuda/include', '-x', 'c++',
                str(frozen_source), '-ldl', '-o', str(library)], check=True,
                stdout=log, stderr=subprocess.STDOUT)
        libraries[name] = str(library)

    def http(path, payload=None, first=None):
        conn = HTTPConnection('127.0.0.1', port, timeout=300)
        try:
            conn.request('GET' if payload is None else 'POST', path,
                None if payload is None else json.dumps(payload), {'Content-Type': 'application/json'})
            response = conn.getresponse()
            chunks = []
            while True:
                line = response.readline()
                if not line:
                    break
                chunks.append(line.decode())
                if first and line.startswith(b'data: {'):
                    event = json.loads(line[6:])
                    if any(c.get('delta', {}).get('content') for c in event.get('choices', [])):
                        first.set()
            return dict(status=response.status, body=''.join(chunks))
        finally:
            conn.close()

    def payload(fixture, name='sample'):
        return dict(model='qwen3.8-flash-next', prompt=fixtures[fixture]['prompt'],
                    stream=True, temperature=0, max_tokens=256 if fixture == 'decode' else 32,
                    stream_options=dict(include_usage=True), request_id=name,
                    cancel_token=secrets.token_hex(32))

    def normal(response, expected):
        assert response['status'] == 200
        data = [line[6:] for line in response['body'].splitlines() if line.startswith('data: ')]
        assert data[-1] == '[DONE]'
        events = [json.loads(x) for x in data[:-1]]
        assert not any('error' in e for e in events)
        choices = [c for e in events for c in e.get('choices', [])]
        assert ''.join(c.get('delta', {}).get('content', '') for c in choices) == expected
        assert [c['finish_reason'] for c in choices if c.get('finish_reason')] == ['stop']
        assert any(e.get('usage') for e in events)

    modes = ['lifecycle', 'inline', 'fallback', 'unsupported', 'quota',
             'writer-error', 'queue-full', 'trace-copy', 'trace-sync', 'trace-d2d', 'shutdown', 'crash', 'pool-device', 'pool-pinned', 'manifest-close']
    if args.case:
        assert set(args.case).issubset(modes) and len(set(args.case)) == len(args.case)
        modes = args.case
    assert modes
    summaries = []
    for mode in modes:
        case = out / mode
        case.mkdir()
        trace = case / 'trace'
        command = base + ['--moe-trace-dir', str(trace), '--moe-trace-workload', str(out / 'workload.json')]
        env = {k: v for k, v in os.environ.items() if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}
        if mode.startswith('pool-'):
            env.update(LD_PRELOAD=libraries['pool'], Q4T_TEST_TRACE_ALLOC=mode.split('-')[1])
        if mode == 'unsupported':
            command[command.index('--max-seq') + 1] = '2'
        if mode == 'inline':
            env['Q4T_NO_BATCH_PREFILL'] = '1'
        if mode == 'fallback':
            env['LD_PRELOAD'] = libraries['fallback']
        if mode == 'quota':
            command += ['--moe-trace-max-mib', '64']
        if mode in ('writer-error', 'queue-full', 'manifest-close'):
            env.update(LD_PRELOAD=libraries['writer'],
                       Q4T_TEST_TRACE_WRITE='close' if mode == 'manifest-close' else ('slow' if mode == 'queue-full' else 'error'))
        if mode.startswith('trace-'):
            env.update(LD_PRELOAD=libraries['readback'], Q4T_TEST_READBACK_BYTES='1920',
                       Q4T_TEST_READBACK_MODE='copy' if mode == 'trace-d2d' else mode.split('-')[1])
            if mode == 'trace-d2d':
                env.update(Q4T_TEST_READBACK_KIND='D2D', Q4T_TEST_READBACK_BYTES='40')
        save(case / 'plan.json', dict(command=command, injection={k: v for k, v in env.items()
            if k.startswith('Q4T_') or k == 'LD_PRELOAD'}, binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest()))
        failure = None
        responses = []
        with (case / 'server.log').open('w') as log:
            server = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
            try:
                for _ in range(180):
                    assert server.poll() is None
                    if 'serving on port' in (case / 'server.log').read_text():
                        break
                    time.sleep(1)
                else:
                    raise RuntimeError('startup timeout')
                if mode == 'shutdown':
                    with ThreadPoolExecutor(1) as pool:
                        task = pool.submit(http, '/v1/chat/completions', payload('long'))
                        for _ in range(500):
                            if json.loads(http('/healthz')['body'])['seq_slots_free'] == 0:
                                break
                            time.sleep(.02)
                        time.sleep(1)
                        server.terminate()
                        try:
                            responses.append(task.result(timeout=120))
                        except (HTTPException, OSError) as error:
                            responses.append(dict(connection_closed=repr(error)))
                elif mode == 'lifecycle':
                    with ThreadPoolExecutor(1) as pool:
                        for fixture in ['long', 'decode']:
                            data = payload(fixture, 'reused-http-id')
                            first = threading.Event()
                            task = pool.submit(http, '/v1/chat/completions', data, first)
                            if fixture == 'decode':
                                assert first.wait(120)
                            else:
                                for _ in range(500):
                                    h = http('/healthz')
                                    if json.loads(h['body'])['seq_slots_free'] == 0:
                                        break
                                    time.sleep(.02)
                                time.sleep(1)
                            cancel = http('/v1/requests/cancel', {k: data[k] for k in ['request_id', 'cancel_token']})
                            assert cancel['status'] == 202
                            response = task.result(timeout=120)
                            assert response['status'] == (409 if fixture == 'long' else 200)
                            assert '"error"' in response['body']
                            responses.append(response)
                    response = http('/v1/chat/completions', payload('short', 'reused-http-id'))
                    normal(response, fixtures['short']['expected'])
                    responses.append(response)
                else:
                    fixture = 'long' if mode in ('inline', 'fallback', 'quota') else 'short'
                    response = http('/v1/chat/completions', payload(fixture))
                    responses.append(response)
                    if mode.startswith('trace-'):
                        assert response['status'] == 200
                        assert 'generation_failed' in response['body']
                        assert '"usage"' not in response['body']
                        assert http('/healthz')['status'] == 503
                        assert http('/v1/chat/completions', payload('short'))['status'] == 503
                    else:
                        normal(response, fixtures[fixture]['expected'])
                        follow = http('/v1/chat/completions', payload('short'))
                        normal(follow, fixtures['short']['expected'])
                        responses.append(follow)
                if mode != 'shutdown' and not mode.startswith('trace-'):
                    health = http('/healthz')
                    assert health['status'] == 200
                    h = json.loads(health['body'])
                    assert h['gpu_healthy'] and h['seq_slots_free'] == h['seq_slots_total']
                if mode == 'crash':
                    for _ in range(300):
                        if len(list(trace.glob('request-*.bin'))) == 2:
                            break
                        time.sleep(.01)
                    assert len(list(trace.glob('request-*.bin'))) == 2
                    server.kill()
            except Exception as error:
                failure = repr(error)
                raise
            finally:
                server.terminate()
                try:
                    server.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    server.kill(); server.wait()
                    failure = failure or 'shutdown timeout'
                save(case / 'responses.json', responses)
                save(case / 'exit.json', dict(server_exit=server.returncode, failure=failure))
        assert server.returncode == (-9 if mode == 'crash' else 0) and failure is None
        log = (case / 'server.log').read_text()
        if mode == 'unsupported':
            assert '[trace] unsupported mode' in log and not trace.exists()
        else:
            if mode.startswith('pool-'):
                assert '[trace] disabled: trace pool allocation' in log
                assert '[trace-pool-fault] real_error=2' in log
                assert '[trace-pool-fault] pinned_live=0' in log
            else:
                assert '[trace] enabled full-request' in log
            manifest = json.loads((trace / 'manifest.json').read_text())
            if mode in ('lifecycle', 'inline', 'fallback', 'shutdown'):
                assert manifest['complete'] and manifest['failure'] == 'none'
                checked = []
                for path in sorted(trace.glob('request-*.bin')):
                    result = subprocess.run([str(checker), str(path)], capture_output=True, text=True)
                    assert result.returncode in (0, 3), result.stderr
                    checked.append(json.loads(result.stdout))
                assert len(checked) == (3 if mode == 'lifecycle' else (1 if mode == 'shutdown' else 2))
                if mode == 'shutdown':
                    assert checked[0]['cancelled_requests'] == 1
                if mode == 'lifecycle':
                    assert sum(r['cancelled_requests'] for r in checked) == 2
                    assert sum(r['successful_requests'] for r in checked) == 1
                if mode == 'fallback':
                    assert '[scheduler-alloc-fault]' in log and 'scheduler logits alloc failed' in log
                result = subprocess.run(['python3', '-B', str(ROOT / 'tools/trace/analyze.py'),
                    '--directory', str(trace), '--checker', str(checker), '--binary', str(binary),
                    '--output', str(case / 'analysis.json')], capture_output=True, text=True)
                assert result.returncode == (3 if mode in ('lifecycle', 'shutdown') else 0), result.stderr
            else:
                assert not manifest['complete']
                reason = dict(quota='byte_quota', **{'writer-error': 'writer_io',
                    'queue-full': 'queue_full', 'trace-copy': 'cuda_failure', 'trace-sync': 'cuda_failure', 'trace-d2d': 'cuda_failure', 'crash': 'none', 'pool-device': 'allocation_failed', 'pool-pinned': 'allocation_failed', 'manifest-close': 'writer_io'})[mode]
                assert manifest['failure'] == reason, manifest
                if mode in ('writer-error', 'queue-full', 'manifest-close'):
                    assert '[trace-write-fault]' in log
                if mode.startswith('trace-'):
                    assert '[readback-fault]' in log
                result = subprocess.run(['python3', '-B', str(ROOT / 'tools/trace/analyze.py'),
                    '--directory', str(trace), '--checker', str(checker), '--binary', str(binary),
                    '--output', str(case / 'must-not-exist.json')], capture_output=True, text=True)
                assert result.returncode != 0 and not (case / 'must-not-exist.json').exists()
        summaries.append(dict(case=mode, passed=True))
        save(out / 'summary.json', summaries)
        print(mode + ': PASS', flush=True)


if __name__ == '__main__':
    main()
