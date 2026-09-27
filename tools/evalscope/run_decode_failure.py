"""Inject decode readback errors and require an explicit failed HTTP terminal.

This is a bug regression test, not a performance or numerical acceptance test.
Each case owns a fresh real-model server; the shim never enters production.
"""
import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

ROOT = Path(__file__).resolve().parents[2]


def save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--fixture-plan', type=Path, required=True,
                        help='Existing readback-failure plan with request/command')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--port', type=int, default=18092)
    args = parser.parse_args()
    out = args.output.resolve()
    assert any(out.is_relative_to(ROOT / d) for d in ['build', '.q4t-work'])
    out.mkdir(parents=True, exist_ok=False)
    plan = json.loads(args.fixture_plan.read_text())
    command = plan['command']
    command[0] = str(args.binary.resolve())
    for key, value in [('--port', str(args.port)), ('--max-seq', '1')]:
        command[command.index(key) + 1] = value
    assert '--no-mtp' in command
    request = plan['request']
    request['max_tokens'] = 16
    request['stream_options'] = {'include_usage': True}
    library = out / 'readback_fault.so'
    shutil.copy2(ROOT / 'tools/verify/readback_fault.cpp.in', out)
    shutil.copy2(__file__, out)
    with (out / 'build.log').open('w') as log:
        subprocess.run(['g++-14', '-std=c++23', '-Wall', '-Wextra', '-shared',
                        '-fPIC', '-I/usr/local/cuda/include', '-x', 'c++',
                        str(out / 'readback_fault.cpp.in'), '-ldl', '-o', str(library)],
                       stdout=log, stderr=subprocess.STDOUT, check=True)
    assert 'warning:' not in (out / 'build.log').read_text()
    save(out / 'identity.json', {
        'command': command,
        'binary_sha256': hashlib.sha256(args.binary.read_bytes()).hexdigest(),
        'source_commit': subprocess.check_output(
            ['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'fault_bytes': 4,
    })
    (out / 'worktree.patch').write_bytes(subprocess.check_output(
        ['git', 'diff', 'HEAD', '--', 'src', 'include'], cwd=ROOT))

    def request_http(path, payload=None):
        conn = http.client.HTTPConnection('127.0.0.1', args.port, timeout=120)
        try:
            conn.request('GET' if payload is None else 'POST', path,
                         None if payload is None else json.dumps(payload),
                         {'Content-Type': 'application/json'})
            response = conn.getresponse()
            return {'status': response.status, 'body': response.read().decode()}
        finally:
            conn.close()

    summaries = []
    for mode in ['copy', 'sync']:
        for stream in [False, True]:
            name = mode + ('-stream' if stream else '-nonstream')
            case = out / name
            case.mkdir()
            request['stream'] = stream
            save(case / 'request.json', request)
            env = {k: v for k, v in os.environ.items()
                   if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}
            env.update(LD_PRELOAD=str(library), Q4T_TEST_READBACK_BYTES='4',
                       Q4T_TEST_READBACK_MODE=mode)
            result = {}
            with (case / 'server.log').open('w') as log:
                server = subprocess.Popen(command, env=env, stdout=log,
                                          stderr=subprocess.STDOUT)
                try:
                    for _ in range(180):
                        assert server.poll() is None, 'server stopped'
                        if 'serving on port' in (case / 'server.log').read_text():
                            break
                        time.sleep(1)
                    else:
                        raise RuntimeError('startup timeout')
                    result['first'] = first = request_http('/v1/chat/completions', request)
                    result['health'] = request_http('/healthz')
                    result['metrics'] = request_http('/metrics')
                    result['followup'] = request_http('/v1/chat/completions', request)
                    if stream:
                        assert first['status'] == 200
                        events = [line[6:] for line in first['body'].splitlines()
                                  if line.startswith('data: ')]
                        assert events[-1] == '[DONE]'
                        records = [json.loads(e) for e in events[:-1]]
                        assert records[-1]['error']['code'] == 'generation_failed'
                        assert sum('error' in r for r in records) == 1
                        assert not any('usage' in r for r in records)
                        assert not any(c.get('finish_reason') is not None
                                       for r in records for c in r.get('choices', []))
                    else:
                        assert first['status'] == 500
                        assert json.loads(first['body'])['error']['code'] == 'generation_failed'
                    assert result['health']['status'] == 503
                    health = json.loads(result['health']['body'])
                    assert not health['gpu_healthy']
                    assert health['seq_slots_free'] == health['seq_slots_total'] == 1
                    metrics = result['metrics']['body']
                    # Sample before the intentionally rejected followup is counted.
                    for key, value in [('success', 0), ('error', 1), ('aborted', 0)]:
                        assert f'q4t_requests_{key}_total {value}\n' in metrics
                    assert result['followup']['status'] == 503
                    trace = (case / 'server.log').read_text()
                    assert trace.count('[readback-fault] mode=') == 1
                    if mode == 'sync':
                        assert trace.count('[readback-fault] sync-return') == 1
                except Exception as exc:
                    result['failure'] = repr(exc)
                    raise
                finally:
                    server.terminate()
                    try:
                        server.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.wait()
                    result['server_exit'] = server.returncode
                    save(case / 'result.json', result)
            assert result['server_exit'] == 0
            summaries.append({'case': name, 'passed': True})
            save(out / 'summary.json', summaries)
            print(name + ': PASS', flush=True)


if __name__ == '__main__':
    main()
