"""Deterministic queued/admitted fault race. Caller owns the GPU lease."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import secrets
import subprocess
import time

ROOT = Path(__file__).resolve().parents[3]
REFERENCE = ROOT / '.q4t-work/request-diagnostics-20261020/acceptance-v1/run'


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--binary', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--baseline', action='store_true')
    args = ap.parse_args()
    out = args.output.resolve()
    assert out.is_relative_to(ROOT / '.q4t-work')
    out.mkdir(exist_ok=False)
    library = out / 'fault.so'
    source = ROOT / 'tools/verify/inference_metadata/admission_fault.cpp.in'
    (out / source.name).write_bytes(source.read_bytes())
    with (out / 'build.log').open('w') as log:
        subprocess.run(['g++-14', '-std=c++23', '-Wall', '-Wextra', '-shared',
                        '-fPIC', '-I/usr/local/cuda/include', '-x', 'c++',
                        str(out / source.name), '-ldl', '-o', str(library)],
                       stdout=log, stderr=subprocess.STDOUT, check=True)
    assert 'warning' not in (out / 'build.log').read_text().lower()
    command = json.loads((REFERENCE / 'quality-off/server-command.json').read_text())['argv']
    command[0] = str(args.binary.resolve())
    command[command.index('--max-len') + 1] = '65536'
    port = int(command[command.index('--port') + 1])
    prompt = json.loads((REFERENCE / 'reference-off/inputs/requests.jsonl').read_text().splitlines()[0])['prompt']
    results = []
    cases = [(1, False)] if args.baseline else [(1, False), (1, True), (2, False), (2, True)]
    for slots, inline in cases:
        d = out / f'slots{slots}-inline{int(inline)}'
        d.mkdir()
        command[command.index('--max-seq') + 1] = str(slots)
        (d / 'command.json').write_text(json.dumps(command))
        env = {k: v for k, v in os.environ.items() if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}
        env.update(LD_PRELOAD=str(library), Q4T_TEST_ADMISSION_RELEASE=str(d / 'release'))
        if inline:
            env['Q4T_NO_BATCH_PREFILL'] = '1'

        def http(path, identity=None):
            conn = HTTPConnection('127.0.0.1', port, timeout=120)
            try:
                body = None if identity is None else json.dumps(dict(
                    model='qwen3.8-flash-next', prompt=prompt, max_tokens=2,
                    temperature=0, stream=False, request_id=identity,
                    cancel_token=secrets.token_hex(32)))
                conn.request('GET' if body is None else 'POST', path, body,
                             {'Content-Type': 'application/json'})
                r = conn.getresponse()
                return dict(status=r.status, body=r.read().decode())
            finally:
                conn.close()

        def trace():
            return (d / 'server.log').read_text()

        def wait(check, seconds=60):
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                if check():
                    return
                assert server.poll() is None
                time.sleep(.02)
            raise RuntimeError('barrier timeout')

        failure = None
        with (d / 'server.log').open('w') as log:
            server = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT)
            try:
                wait(lambda: 'serving on port' in trace(), 300)
                with ThreadPoolExecutor(max_workers=2) as pool:
                    first = pool.submit(http, '/v1/chat/completions', 'fault-first')
                    wait(lambda: '[admission-fault] waiting' in trace())
                    second = pool.submit(http, '/v1/chat/completions', 'fault-waiter')
                    event = 'stage=slot_admission' if slots == 1 else 'stage=prefill'
                    wait(lambda: any(' id=fault-waiter ' in line and event in line
                                     for line in trace().splitlines()))
                    # First request still holds model_mu_ and its sequence slot.
                    assert not second.done()
                    (d / 'release').touch()
                    a, b = first.result(timeout=120), second.result(timeout=120)
                wait(lambda: sum('event=end' in l for l in trace().splitlines()) == 2)
                health = http('/healthz')
                followup = http('/v1/chat/completions', 'fault-followup')
                h = json.loads(health['body'])
                readbacks = trace().count('[admission-fault] logits-readback')
                result = dict(first=a, waiter=b, health=health, followup=followup,
                              readbacks=readbacks, slots=slots, inline=inline)
                (d / 'result.json').write_text(json.dumps(result, indent=2))
                assert a['status'] == 500
                assert health['status'] == 503 and not h['gpu_healthy']
                assert h['seq_slots_free'] == h['seq_slots_total'] == slots
                assert followup['status'] == 503
                if args.baseline:
                    assert b['status'] == 200 and readbacks == 2
                else:
                    assert b['status'] == (503 if slots == 1 else 500)
                    assert readbacks == 1
                    if slots == 1:
                        assert not any(' id=fault-waiter ' in line and
                                       'event=admitted' in line for line in trace().splitlines())
                results.append(dict(case=d.name, status='PASS'))
            except BaseException as exc:
                failure = repr(exc)
                raise
            finally:
                (d / 'release').touch()
                server.terminate()
                try:
                    server.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()
                (d / 'exit.json').write_text(json.dumps(dict(server=server.returncode, failure=failure)))
            assert server.returncode == 0
    (out / 'result.json').write_text(json.dumps(dict(
        status='REPRODUCED' if args.baseline else 'PASS', cases=results), indent=2))


if __name__ == '__main__':
    main()
