"""HTTP lifecycle checks after single-stream and interleave acceptance.

Uses evalscope for inference requests; health/admission and expected rejection
checks use HTTP directly. Synthetic CUDA return failures are not device faults.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from run_chunk_interleave import ROOT, read_result, save, sha


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--accepted-root', type=Path, required=True)
    ap.add_argument('--interleave', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--port', type=int, default=8000)
    args = ap.parse_args()
    run, paired, out = (x.resolve() for x in [args.accepted_root, args.interleave, args.output])
    assert json.loads((paired / 'review.json').read_text())['accepted']
    gate = json.loads((run / 'acceptance.json').read_text())
    assert gate['quality_passed'] and gate['performance_accepted']
    assert gate['binary_sha256'] == sha(ROOT / 'build/q4t')
    for name in ['src/server/chat_server.cpp', 'include/q4t/server/chat_server.h',
                 'src/model/model.cu', 'include/q4t/model/model.h']:
        assert (ROOT / name).read_bytes() == (run / 'source' / name).read_bytes()
    assert any(out.is_relative_to(ROOT / p) for p in ['build', '.q4t-work'])
    out.mkdir(parents=True, exist_ok=False)
    shutil.copy2(__file__, out / Path(__file__).name)
    shutil.copy2(ROOT / 'tools/evalscope/run_chunk_interleave.py', out)
    library = out / 'readback_fault.so'
    source = ROOT / 'tools/verify/readback_fault.cpp.in'
    shutil.copy2(source, out)
    with (out / 'build.log').open('w') as log:
        subprocess.run(['g++-14', '-std=c++23', '-Wall', '-Wextra', '-shared', '-fPIC',
                        '-I/usr/local/cuda/include', '-x', 'c++', str(source), '-ldl',
                        '-o', str(library)], stdout=log, stderr=subprocess.STDOUT, check=True)
    assert 'warning' not in (out / 'build.log').read_text().lower()
    question = next(x for x in json.loads((run / 'quality/results.json').read_text())
                    if x['actual_input'] == 45056)
    lines = (run / 'quality/inputs/requests.jsonl').read_text().splitlines()
    qline = next(x for x in lines if hashlib.sha256(
        json.loads(x)['prompt'].encode()).hexdigest() == question['prompt_sha256'])
    datasets = {'A': out / 'performance.jsonl', 'B': out / 'retrieval.jsonl'}
    shutil.copy2(paired / 'long.jsonl', datasets['A'])
    datasets['B'].write_text(qline + '\n')
    expected_a = json.loads((paired / 'results.json').read_text())[0]['long']['output_sha256']
    server_template = json.loads((paired / 'candidate/server-command.json').read_text())
    client_template = json.loads((paired / 'candidate/0/long/command.json').read_text())
    base = f'http://127.0.0.1:{args.port}'
    records = []

    def http(path, payload=None):
        req = urllib.request.Request(base + path,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode()

    def wait_admitted(count):
        for _ in range(300):
            status, text = http('/metrics')
            assert status == 200
            line = next(x for x in text.splitlines() if x.startswith('q4t_requests_total '))
            actual = int(line.split()[1])
            assert actual <= count
            if actual == count:
                return
            time.sleep(0.2)
        raise RuntimeError('admission timeout')

    for scenario in ['rotation-reuse', 'shutdown', 'copy', 'sync']:
        directory = out / scenario
        directory.mkdir()
        env = {k: v for k, v in os.environ.items() if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}
        if scenario in ['copy', 'sync']:
            env.update(LD_PRELOAD=str(library), Q4T_TEST_READBACK_MODE=scenario,
                       Q4T_TEST_READBACK_BYTES='496640')
        cmd = server_template.copy()
        cmd[cmd.index('--port') + 1] = str(args.port)
        save(directory / 'command.json', cmd)
        with (directory / 'server.log').open('w') as server_log:
            server = subprocess.Popen(cmd, env=env, stdout=server_log, stderr=subprocess.STDOUT)
            children, logs = [], []
            try:
                for _ in range(180):
                    assert server.poll() is None
                    if 'serving on port' in (directory / 'server.log').read_text():
                        break
                    time.sleep(1)
                else:
                    raise RuntimeError('startup timeout')
                startup = (directory / 'server.log').read_text()
                assert '=> max_len=65536 max_seq=2' in startup
                rounds = 2 if scenario == 'rotation-reuse' else 1
                for repeat in range(rounds):
                    order = ['A', 'B'] if repeat == 0 else ['B', 'A']
                    launched = []
                    for index, label in enumerate(order):
                        if index:
                            wait_admitted(repeat * 2 + 1)
                            time.sleep(2)
                        case = directory / f'{repeat}-{label}'
                        case.mkdir()
                        client = client_template.copy()
                        for flag, value in [('--dataset-path', str(datasets[label])),
                                            ('--outputs-dir', str(case)),
                                            ('--url', base + '/v1/chat/completions'),
                                            ('--max-tokens', '1' if label == 'A' else '32')]:
                            client[client.index(flag) + 1] = value
                        save(case / 'command.json', client)
                        log = (case / 'client.log').open('w')
                        logs.append(log)
                        # The injection environment belongs only to the server.
                        client_env = {k: v for k, v in env.items()
                                      if not k.startswith('Q4T_TEST_') and k != 'LD_PRELOAD'}
                        process = subprocess.Popen(client, env=client_env, stdout=log,
                                                   stderr=subprocess.STDOUT)
                        children.append(process)
                        launched.append((label, case, process))
                    if scenario == 'shutdown':
                        wait_admitted(2)
                        time.sleep(1)
                        stopped = time.monotonic()
                        server.terminate()
                    for label, case, process in launched:
                        code = process.wait(timeout=600)
                        save(case / 'client-exit.json', {'exit': code})
                        if scenario == 'rotation-reuse':
                            assert code == 0
                            parsed = read_result(case, 45056,
                                                 1 if label == 'A' else question['actual_output'])
                            expected = expected_a if label == 'A' else hashlib.sha256(
                                question['text'].encode()).hexdigest()
                            assert parsed['output_sha256'] == expected
                        else:
                            with sqlite3.connect(next(case.rglob('benchmark_data.db'))) as db:
                                rows = db.execute('select success,request,response_messages from result').fetchall()
                            save(case / 'failed-responses.json', rows)
                            assert len(rows) == 1 and rows[0][0] == 0
                    if scenario != 'shutdown':
                        status, body = http('/healthz')
                        health = json.loads(body)
                        save(directory / f'health-{repeat}.json', {'status': status, 'body': health})
                        assert health['seq_slots_total'] == health['seq_slots_free'] == 2
                        assert health['gpu_healthy'] == (scenario == 'rotation-reuse')
                        assert status == (200 if scenario == 'rotation-reuse' else 503)
                    if scenario in ['copy', 'sync']:
                        status, body = http('/v1/chat/completions', {
                            'model': 'qwen3.8-flash-next', 'prompt': 'Return OK.',
                            'max_tokens': 1})
                        save(directory / 'followup.json', {'status': status, 'body': body})
                        assert status == 503
                if scenario == 'shutdown':
                    assert server.wait(timeout=30) == 0
                    save(directory / 'shutdown.json', {'seconds_until_clients_and_server_done':
                                                       time.monotonic() - stopped})
                    assert 'shutdown complete (0 in-flight remaining)' in (directory / 'server.log').read_text()
                if scenario in ['copy', 'sync']:
                    trace = (directory / 'server.log').read_text()
                    assert trace.count('[readback-fault] mode=') == 1
                    if scenario == 'sync':
                        assert trace.count('[readback-fault] sync-return') == 1
            finally:
                for process in children:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                for log in logs:
                    log.close()
                if server.poll() is None:
                    server.terminate()
                    try:
                        server.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.wait()
                save(directory / 'exit.json', {'server': server.returncode})
            assert server.returncode == 0
        records.append({'scenario': scenario, 'passed': True})
        save(out / 'results.json', records)
        print(scenario + ': HTTP passed', flush=True)
    save(out / 'complete.json', {'passed': len(records), 'binary_sha256': gate['binary_sha256'],
                                'limits': 'Fixed two-slot text cases; synthetic return errors, not real hardware faults.'})


if __name__ == '__main__':
    main()
