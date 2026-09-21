"""Post-gate HTTP failure checks; injected return codes, not hardware faults."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.request


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--accepted-run', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--model-dir', type=Path, required=True)
    ap.add_argument('--port', type=int, default=8000)
    args = ap.parse_args()
    root = Path.cwd()
    run = args.accepted_run.resolve()
    gate = json.loads((run / 'acceptance.json').read_text())
    assert gate['quality_passed'] and gate['performance_accepted']
    binary = root / 'build/q4t'
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    assert digest == gate['binary_sha256']
    assert (run / 'chat_server.cpp').read_bytes() == (
        root / 'src/server/chat_server.cpp').read_bytes()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    lib = out / 'readback_fault.so'
    command = ['g++-14', '-std=c++23', '-Wall', '-Wextra', '-shared', '-fPIC',
               '-I/usr/local/cuda/include', '-x', 'c++',
               str(root / 'tools/verify/readback_fault.cpp.in'), '-ldl',
               '-o', str(lib)]
    with (out / 'build.log').open('w') as log:
        subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
    prompt = json.loads((run / 'performance/inputs/context-1024.jsonl')
                        .read_text().splitlines()[0])['prompt']
    base = f'http://127.0.0.1:{args.port}'

    def request(path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(base + path, data=data,
                                     headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=300) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    payload = {'model': 'qwen3.8-flash-next', 'prompt': prompt,
               'max_tokens': 16, 'temperature': 0, 'stream': False}
    cases = [(version, stage, mode)
             for version in ['parent', 'candidate']
             for stage in ['prefill', 'inline-prefill', 'decode']
             for mode in ['copy', 'sync']]
    results = []
    for version, stage, mode in cases:
        name = f'{version}-{stage}-{mode}'
        case = out / name
        case.mkdir()
        env = {k: v for k, v in os.environ.items()
               if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}
        env.update(LD_PRELOAD=str(lib), Q4T_TEST_READBACK_MODE=mode,
                   Q4T_TEST_READBACK_BYTES='4' if stage == 'decode' else '496640')
        if stage == 'inline-prefill':
            env['Q4T_NO_BATCH_PREFILL'] = '1'
        target = run / 'q4t-before' if version == 'parent' else binary
        cmd = [str(target), 'serve', '--model-dir', str(args.model_dir),
               '--port', str(args.port), '--max-seq', '1', '--max-prefill',
               '8192', '--max-len', '208896', '--max-tokens', '256', '--no-mtp']
        (case / 'command.json').write_text(json.dumps(cmd, indent=2))
        (case / 'binary.sha256').write_text(hashlib.sha256(target.read_bytes()).hexdigest())
        response = {}
        with (case / 'server.log').open('w') as log:
            server = subprocess.Popen(cmd, env=env, stdout=log,
                                      stderr=subprocess.STDOUT)
            try:
                for _ in range(180):
                    if server.poll() is not None:
                        raise RuntimeError('server exited during startup')
                    if 'serving on port' in (case / 'server.log').read_text():
                        break
                    time.sleep(1)
                else:
                    raise RuntimeError('startup timeout')
                response['first'] = request('/v1/chat/completions', payload)
                response['health'] = request('/healthz')
                response['followup'] = request('/v1/chat/completions', payload)
                (case / 'responses.json').write_text(
                    json.dumps(response, ensure_ascii=False, indent=2))
                logs = (case / 'server.log').read_text()
                assert logs.count('[readback-fault] mode=') == 1, logs[-2000:]
                if mode == 'sync':
                    assert logs.count('[readback-fault] sync-return') == 1
                first, health, followup = (response[k] for k in
                                           ['first', 'health', 'followup'])
                if version == 'parent':
                    assert first[0] == health[0] == followup[0] == 200, response
                    assert health[1]['gpu_healthy'] is True
                else:
                    assert health[0] == 503 and health[1]['gpu_healthy'] is False
                    assert followup[0] == 503, response
                    if stage == 'decode':
                        assert first[0] == 200, response
                        assert first[1]['usage']['completion_tokens'] == 1, response
                    else:
                        assert first[0] == 500, response
            finally:
                server.terminate()
                try:
                    server.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()
                (case / 'exit.json').write_text(json.dumps({'server': server.returncode}))
        assert server.returncode == 0
        results.append({'case': name, 'passed': True})
        (out / 'results.json').write_text(json.dumps(results, indent=2))
        print(name + ': passed', flush=True)
    (out / 'complete.json').write_text(json.dumps({
        'passed': len(results), 'binary_sha256': digest,
        'limits': 'Synthetic CUDA return codes, not device failure or MTP validation; '
                  'nonstream single request, no forced multi-request batching.'}, indent=2))


if __name__ == '__main__':
    main()
