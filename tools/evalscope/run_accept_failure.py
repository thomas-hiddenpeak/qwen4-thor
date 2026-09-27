"""Fault-inject fatal accept while prefill and a partial HTTP reader are live."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
from http.client import HTTPConnection, HTTPException
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time

ROOT = Path(__file__).resolve().parents[2]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--lifecycle', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    out = args.output.resolve()
    assert any(out.is_relative_to(ROOT / p) for p in ['build', '.q4t-work'])
    previous = args.lifecycle.resolve()
    summary = json.loads((previous / 'summary.json').read_text())
    assert summary['failure'] is None and summary['server_exit'] == 0
    plan = json.loads((previous / 'plan.json').read_text())
    cmd = plan['command']
    assert hashlib.sha256(Path(cmd[0]).read_bytes()).hexdigest() == plan['binary_sha256']
    payload = json.loads((previous / 'active-a-request.json').read_text())
    payload['request_id'] = 'accept-failure-active'
    port = int(cmd[cmd.index('--port') + 1])
    out.mkdir(parents=True, exist_ok=False)
    shutil.copy2(__file__, out)
    source = ROOT / 'tools/verify/request_control/accept_failure.cpp'
    shutil.copy2(source, out)
    library = out / 'accept_failure.so'
    with (out / 'build.log').open('w') as log:
        subprocess.run(['g++-14', '-std=c++23', '-Wall', '-Wextra', '-shared', '-fPIC', str(source), '-ldl', '-o', str(library)], stdout=log, stderr=subprocess.STDOUT, check=True)
    assert 'warning' not in (out / 'build.log').read_text().lower()
    (out / 'plan.json').write_text(json.dumps({'command': cmd, 'payload': payload, 'binary_sha256': plan['binary_sha256'], 'expected_server_exit': 1}, indent=2))

    def request(path, body=None):
        c = HTTPConnection('127.0.0.1', port, timeout=120)
        try:
            c.request('GET' if body is None else 'POST', path, None if body is None else json.dumps(body), {'Content-Type': 'application/json'})
            r = c.getresponse()
            return {'status': r.status, 'body': r.read().decode()}
        finally:
            c.close()

    env = {k: v for k, v in os.environ.items() if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}
    env.update(LD_PRELOAD=str(library), Q4T_TEST_ACCEPT_FAIL_FLAG=str(out / 'fire'))
    failure = None
    stalled = None
    with (out / 'server.log').open('w') as log, ThreadPoolExecutor(max_workers=1) as pool:
        server = subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            for _ in range(180):
                assert server.poll() is None
                if 'serving on port' in (out / 'server.log').read_text():
                    break
                time.sleep(1)
            else:
                raise RuntimeError('startup timeout')
            stalled = socket.create_connection(('127.0.0.1', port), timeout=10)
            stalled.sendall(b'POST /v1/chat/completions HTTP/1.1\r\n')
            future = pool.submit(request, '/v1/chat/completions', payload)
            for _ in range(300):
                if json.loads(request('/healthz')['body'])['seq_slots_free'] == 1:
                    break
                time.sleep(.02)
            else:
                raise RuntimeError('no active request')
            time.sleep(.5)
            (out / 'fire').touch()
            with socket.create_connection(('127.0.0.1', port), timeout=10):
                pass
            assert server.wait(timeout=60) == 1
            try:
                result = future.result(timeout=5)
            except (HTTPException, OSError) as exc:
                result = {'disconnected': repr(exc)}
            (out / 'response.json').write_text(json.dumps(result))
            trace = (out / 'server.log').read_text()
            assert trace.count('[accept-fault] injected EMFILE') == 1
            assert 'shutdown complete (0 in-flight remaining)' in trace
            assert 'position=8192 total=45056' in trace
        except Exception as exc:
            failure = repr(exc)
            raise
        finally:
            if stalled:
                stalled.close()
            if server.poll() is None:
                server.terminate()
                try:
                    server.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()
            (out / 'exit.json').write_text(json.dumps({'failure': failure, 'server': server.returncode, 'expected_server': 1}))
    print('fatal accept cleanup passed (expected server exit 1)', flush=True)


if __name__ == '__main__':
    main()
