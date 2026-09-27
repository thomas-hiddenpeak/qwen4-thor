"""Verify synthetic readback failure drains slots and refuses further work.

Uses the existing test-only LD_PRELOAD shim. This is not a real GPU fault.
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
from run_state_lifecycle import ROOT, save


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--lifecycle', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--binary', type=Path, help='Explicit candidate; lifecycle supplies fixtures only')
    args = ap.parse_args()
    out = args.output.resolve()
    assert any(out.is_relative_to(ROOT / p) for p in ['build', '.q4t-work'])
    prior = args.lifecycle.resolve()
    summary = json.loads((prior / 'summary.json').read_text())
    assert summary['failure'] is None and summary['server_exit'] == 0
    plan = json.loads((prior / 'plan.json').read_text())
    command, payload = plan['command'], plan['cases']['A']
    if args.binary:
        command[0] = str(args.binary.resolve())
    digest = hashlib.sha256(Path(command[0]).read_bytes()).hexdigest()
    if not args.binary:
        assert digest == json.loads((prior / 'identity.json').read_text())['binary_sha256']
    out.mkdir(parents=True, exist_ok=False)
    for path in [Path(__file__), ROOT / 'tools/evalscope/run_state_lifecycle.py',
                 ROOT / 'tools/verify/readback_fault.cpp.in']:
        shutil.copy2(path, out)
    save(out / 'plan.json', {'command': command, 'request': payload,
         'binary_sha256': digest, 'expected': '500 then unhealthy 503, two free slots, subsequent 503'})
    library = out / 'readback_fault.so'
    with (out / 'build.log').open('w') as log:
        subprocess.run(['g++-14', '-std=c++23', '-Wall', '-Wextra', '-shared', '-fPIC',
                        '-I/usr/local/cuda/include', '-x', 'c++', str(out / 'readback_fault.cpp.in'),
                        '-ldl', '-o', str(library)], stdout=log, stderr=subprocess.STDOUT, check=True)
    assert 'warning' not in (out / 'build.log').read_text().lower()
    port = int(command[command.index('--port') + 1])

    def request_http(path, data=None):
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=120)
        try:
            conn.request('GET' if data is None else 'POST', path,
                         None if data is None else json.dumps(data), {'Content-Type': 'application/json'})
            response = conn.getresponse()
            return {'status': response.status, 'body': response.read().decode()}
        finally:
            conn.close()

    for mode in ['copy', 'sync']:
        d = out / mode
        d.mkdir()
        env = {k: v for k, v in os.environ.items() if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}
        env.update(LD_PRELOAD=str(library), Q4T_TEST_READBACK_MODE=mode, Q4T_TEST_READBACK_BYTES='496640')
        failure = None
        with (d / 'server.log').open('w') as log:
            server = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT)
            try:
                for _ in range(180):
                    assert server.poll() is None
                    if 'serving on port' in (d / 'server.log').read_text():
                        break
                    time.sleep(1)
                else:
                    raise RuntimeError('startup timeout')
                first = request_http('/v1/chat/completions', payload)
                save(d / 'failed-request.json', first)
                assert first['status'] == 500 and 'data:' not in first['body']
                health = request_http('/healthz')
                save(d / 'health.json', health)
                h = json.loads(health['body'])
                assert health['status'] == 503 and not h['gpu_healthy']
                assert h['seq_slots_total'] == h['seq_slots_free'] == 2
                followup = request_http('/v1/chat/completions', payload)
                save(d / 'followup.json', followup)
                assert followup['status'] == 503
                trace = (d / 'server.log').read_text()
                assert trace.count('[readback-fault] mode=') == 1
                if mode == 'sync':
                    assert trace.count('[readback-fault] sync-return') == 1
            except Exception as exc:
                failure = repr(exc)
                raise
            finally:
                server.terminate()
                try:
                    server.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()
                save(d / 'exit.json', {'failure': failure, 'server': server.returncode})
            assert server.returncode == 0
        print(mode + ': passed', flush=True)


if __name__ == '__main__':
    main()
