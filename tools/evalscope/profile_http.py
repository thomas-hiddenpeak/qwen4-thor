"""Profile accepted HTTP workloads; trace timings are not acceptance timings.

Requires a completed five-length acceptance run of the identical binary.
Nsight starts after model loading, around one real evalscope request per length.
Only local, trusted evalscope databases are deserialized.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import pickle
import shutil
import sqlite3
import subprocess
import time

ROOT = Path(__file__).resolve().parents[2]
LENGTHS = [1024, 4096, 8192, 45056, 204800]


def save(path, obj):
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--accepted-run', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--model-dir', type=Path, required=True)
    parser.add_argument('--binary', type=Path, default=ROOT / 'build/q4t')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--lengths', type=int, nargs='+', choices=LENGTHS,
                        default=LENGTHS)
    args = parser.parse_args()
    accepted = args.accepted_run.resolve()
    binary = args.binary.resolve()
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    gate = json.loads((accepted / 'exit.json').read_text())
    refs = json.loads((accepted / 'results.json').read_text())
    if (not gate['http_output_checks_passed'] or gate['server'] != 0 or
            {r['length'] for r in refs} != set(LENGTHS) or
            (accepted / 'binary.sha256').read_text().strip() != digest):
        parser.error('requires completed five-length HTTP acceptance of this binary')
    out = args.output.resolve()
    if not any(out.is_relative_to(ROOT / d) for d in ['build', '.q4t-work']):
        parser.error('output must be under build/ or .q4t-work/')
    out.mkdir(parents=True, exist_ok=False)
    refs = {r['length']: r for r in refs}
    for length in args.lengths:
        case = out / f'context-{length}'
        case.mkdir()
        line = (accepted / f'context-{length}/requests.jsonl').read_text().splitlines()[0]
        prompt = json.loads(line)['prompt']
        if hashlib.sha256(prompt.encode()).hexdigest() != refs[length]['prompt_sha256']:
            raise RuntimeError('accepted prompt hash mismatch')
        (case / 'requests.jsonl').write_text(line + '\n')
    env = os.environ.copy()
    removed = {}
    for key in list(env):
        if key.startswith(('Q4T_FP8', 'Q4T_PROFILE', 'Q4T_MTP_TIMING',
                           'Q4T_SCHED_DEBUG', 'Q4T_ACCESS_LOG')):
            removed[key] = env.pop(key)
    (out / 'binary.sha256').write_text(digest + '\n')
    (out / 'commit.txt').write_bytes(subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT))
    (out / 'worktree.patch').write_bytes(subprocess.check_output(['git', 'diff', 'HEAD'], cwd=ROOT))
    shutil.copyfile(__file__, out / 'profile_http.py')
    shutil.copyfile(accepted / 'CMakeCache.txt', out / 'CMakeCache.txt')
    shutil.copyfile(accepted / 'evalscope-version.txt', out / 'evalscope-version.txt')
    (out / 'nsys-version.txt').write_bytes(subprocess.check_output(['nsys', '--version']))
    session = f'q4t-http-{os.getpid()}'
    command = ['nsys', 'launch', '--session-new=' + session, '--trace=cuda',
               '--discard-environment=true', str(binary), 'serve',
               '--model-dir', str(args.model_dir.resolve()), '--port', str(args.port),
               '--max-seq', '1', '--max-prefill', '8192', '--max-len', '208896',
               '--max-tokens', '256', '--no-mtp']
    save(out / 'server-command.json', {'argv': command, 'removed_environment': removed,
                                      'accepted_run': str(accepted)})
    results = []
    failure = None
    server = None
    with (out / 'server.log').open('w') as log, (out / 'nsys.log').open('w') as trace_log:
        try:
            server = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log,
                                      stderr=subprocess.STDOUT)
            for _ in range(180):
                if server.poll() is not None:
                    raise RuntimeError('profiled server exited during startup')
                if 'serving on port' in (out / 'server.log').read_text():
                    break
                time.sleep(1)
            else:
                raise RuntimeError('startup timeout')
            for length in args.lengths:
                case = out / f'context-{length}'
                start = ['nsys', 'start', '--session=' + session, '--sample=none',
                         '--cpuctxsw=none', '--output=' + str(case / 'trace')]
                save(case / 'capture-command.json', start)
                subprocess.run(start, cwd=ROOT, stdout=trace_log,
                               stderr=subprocess.STDOUT, check=True)
                command = [str(ROOT / 'tools/evalscope/.venv/bin/evalscope'), 'perf',
                           '--model', 'qwen3.8-flash-next', '--url',
                           f'http://127.0.0.1:{args.port}/v1/chat/completions',
                           '--api', 'openai', '--tokenizer-path', str(args.model_dir.resolve()),
                           '--dataset', 'line_by_line', '--dataset-path', str(case / 'requests.jsonl'),
                           '--min-prompt-length', str(length), '--max-prompt-length', str(length),
                           '--no-apply-chat-template', '--max-tokens', '256', '--temperature', '0',
                           '--seed', '20260920', '--parallel', '1', '--number', '1', '--warmup-num', '0',
                           '--connect-timeout', '30', '--read-timeout', '7200', '--total-timeout', '10800',
                           '--no-test-connection', '--stream', '--outputs-dir', str(case)]
                save(case / 'command.json', command)
                with (case / 'client.log').open('w') as client:
                    subprocess.run(command, cwd=ROOT, env=env, stdout=client,
                                   stderr=subprocess.STDOUT, check=True)
                subprocess.run(['nsys', 'stop', '--session=' + session], cwd=ROOT,
                               stdout=trace_log, stderr=subprocess.STDOUT, check=True)
                with sqlite3.connect(next(case.rglob('benchmark_data.db'))) as db:
                    rows = db.execute('select success,prompt_tokens,completion_tokens,'
                                      'response_messages,first_chunk_latency,latency from result').fetchall()
                if len(rows) != 1:
                    raise RuntimeError('expected one diagnostic request')
                row = rows[0]
                choices = [c for m in pickle.loads(base64.b64decode(row[3]))
                           for c in m.get('choices', [])]
                text = ''.join(c.get('delta', {}).get('content', '') for c in choices)
                finish = [c['finish_reason'] for c in choices if c.get('finish_reason')]
                item = {'length': length, 'success': row[0], 'input_tokens': row[1],
                        'output_tokens': row[2], 'finish': finish, 'ttft': row[4],
                        'latency': row[5], 'output_sha256': hashlib.sha256(text.encode()).hexdigest()}
                (case / 'output.txt').write_text(text)
                results.append(item)
                save(out / 'results.json', results)
                if (not row[0] or row[1] != length or row[2] != 256 or finish != ['length'] or
                        item['output_sha256'] != refs[length]['outputs'][0]):
                    raise RuntimeError('profiled HTTP result differs from accepted output')
                print(f'{length}: profiled HTTP/output passed', flush=True)
        except Exception as error:
            failure = repr(error)
            raise
        finally:
            shutdown = subprocess.run(['nsys', 'shutdown', '--session=' + session,
                                       '--kill=sigterm'], cwd=ROOT, stdout=trace_log,
                                      stderr=subprocess.STDOUT)
            if server is not None:
                try:
                    server.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()
            save(out / 'exit.json', {'completed': len(results), 'failure': failure,
                                    'shutdown': shutdown.returncode,
                                    'launcher': server.returncode if server else None,
                                    'trace_exports_complete': False})
    for length in args.lengths:
        case = out / f'context-{length}'
        with (case / 'export.log').open('w') as log:
            subprocess.run(['nsys', 'export', '--type=sqlite', '--output=' + str(case / 'trace.sqlite'),
                            str(case / 'trace.nsys-rep')], cwd=ROOT, stdout=log,
                           stderr=subprocess.STDOUT, check=True)

    status = json.loads((out / 'exit.json').read_text())
    status['trace_exports_complete'] = True
    save(out / 'exit.json', status)


if __name__ == '__main__':
    main()
