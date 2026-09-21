"""Real HTTP parent/candidate/parent gate for packed prefill output rows.

Requires the complete single-stream gate. All inference uses evalscope; a
readback-only preload observer establishes actual B=2 coverage. No warmup,
connection probe, kernel timing, or numerical tests are performed here.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import pickle
import re
import shutil
import sqlite3
import subprocess
import time
import urllib.request

from run_chunk_interleave import ROOT, save, sha


def results(case):
    # Only trusted, locally generated evalscope databases are deserialized.
    with sqlite3.connect(next(case.rglob('benchmark_data.db'))) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute('select * from result order by start_time').fetchall()
    parsed = []
    for row in rows:
        messages = pickle.loads(base64.b64decode(row['response_messages']))
        text = ''.join(c.get('delta', c.get('message', {})).get('content', '')
                       for m in messages for c in m.get('choices', []))
        request = json.loads(row['request'])
        item = {k: row[k] for k in ['success', 'start_time', 'completed_time',
                                    'latency', 'first_chunk_latency',
                                    'prompt_tokens', 'completion_tokens']}
        item.update(prompt_sha256=hashlib.sha256(request['prompt'].encode()).hexdigest(),
                    output_sha256=hashlib.sha256(text.encode()).hexdigest(),
                    text=text)
        parsed.append(item)
    save(case / 'parsed.json', parsed)
    return parsed


def client(args, fixture, case, parallel, count, tokens, low, high):
    case.mkdir()
    command = [str(ROOT / 'tools/evalscope/.venv/bin/evalscope'), 'perf',
               '--model', 'qwen3.8-flash-next', '--url',
               f'http://127.0.0.1:{args.port}/v1/chat/completions',
               '--api', 'openai', '--tokenizer-path', str(args.model_dir),
               '--dataset', 'line_by_line', '--dataset-path', str(fixture),
               '--min-prompt-length', str(low), '--max-prompt-length', str(high),
               '--no-apply-chat-template', '--max-tokens', str(tokens),
               '--temperature', '0', '--seed', '20260920',
               '--parallel', str(parallel), '--number', str(count),
               '--warmup-num', '0', '--no-test-connection', '--stream',
               '--connect-timeout', '30', '--read-timeout', '600',
               '--total-timeout', '900', '--outputs-dir', str(case)]
    save(case / 'command.json', command)
    with (case / 'client.log').open('w') as log:
        return subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['accepted-root', 'parent-run', 'output', 'model-dir']:
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--parent-selected-rows', action='store_true',
                        help='parent already writes one packed row per sequence')
    args = parser.parse_args()
    run, parent, out = [p.resolve() for p in
                        [args.accepted_root, args.parent_run, args.output]]
    if not any(out.is_relative_to(ROOT / p) for p in ['build', '.q4t-work']):
        parser.error('output must be under build/ or .q4t-work/')
    gate = json.loads((run / 'acceptance.json').read_text())
    assert gate['quality_passed'] and gate['performance_accepted']
    binaries = {'candidate': ROOT / 'build/q4t', 'parent': run / 'q4t-before'}
    for name, baseline in [('candidate', run), ('parent', parent)]:
        for mode, count in [('quality', 11), ('performance', 5)]:
            terminal = json.loads((baseline / mode / 'exit.json').read_text())
            assert terminal['completed'] == count and terminal['server'] == 0
            assert terminal['http_output_checks_passed'] and terminal['failure'] is None
            assert sha(binaries[name]) == (baseline / mode / 'binary.sha256').read_text().strip()
    for name in ['src/model/model.cu', 'include/q4t/model/model.h',
                 'src/server/chat_server.cpp', 'include/q4t/server/chat_server.h']:
        assert (ROOT / name).read_bytes() == (run / 'source' / name).read_bytes()
    out.mkdir(parents=True, exist_ok=False)
    save(out / 'coverage-mode.json',
         {'parent_selected_rows': args.parent_selected_rows})
    shutil.copy2(__file__, out / Path(__file__).name)
    shutil.copy2(ROOT / 'tools/evalscope/run_chunk_interleave.py', out)
    observer = ROOT / 'tools/verify/prefill_batch_readback.cpp.in'
    shutil.copy2(observer, out / observer.name)
    library = out / 'readback.so'
    build = ['g++-14', '-std=c++23', '-Wall', '-Wextra', '-shared', '-fPIC',
             '-I/usr/local/cuda/include', '-x', 'c++', str(observer), '-ldl',
             '-o', str(library)]
    save(out / 'build-command.json', build)
    with (out / 'build.log').open('w') as log:
        subprocess.run(build, stdout=log, stderr=subprocess.STDOUT, check=True)
    assert 'warning:' not in (out / 'build.log').read_text()
    env = {k: v for k, v in os.environ.items()
           if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}
    env['LD_PRELOAD'] = str(library)
    lines = {n: (run / f'performance/context-{n}/requests.jsonl').read_text().splitlines()[0]
             for n in [1024, 4096, 45056]}
    input_hashes = {n: hashlib.sha256(json.loads(line)['prompt'].encode()).hexdigest()
                    for n, line in lines.items()}
    (out / 'long.jsonl').write_text(lines[45056] + '\n')
    all_results, expected = [], {}
    for group in ['parent-before', 'candidate', 'parent-after']:
        version = 'candidate' if group == 'candidate' else 'parent'
        directory = out / group
        directory.mkdir()
        log_path = directory / 'server.log'
        command = [str(binaries[version]), 'serve', '--model-dir', str(args.model_dir),
                   '--port', str(args.port), '--max-seq', '3', '--max-len', '65536',
                   '--max-prefill', '8192', '--max-tokens', '256', '--no-mtp']
        save(directory / 'server-command.json', command)
        (directory / 'binary.sha256').write_text(sha(binaries[version]) + '\n')
        with log_path.open('w') as log:
            server = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 180
            while 'serving on port' not in log_path.read_text():
                assert server.poll() is None and time.monotonic() < deadline
                time.sleep(1)
            startup = log_path.read_text()
            assert '=> max_len=65536 max_seq=3' in startup
            assert 'MTP disabled; plain decode' in startup
            for repeat in range(3):
                case = directory / str(repeat)
                case.mkdir()
                lengths = [1024, 4096] if repeat % 2 == 0 else [4096, 1024]
                fixture = case / 'shorts.jsonl'
                fixture.write_text('\n'.join(lines[n] for n in lengths) + '\n')
                log_start = len(log_path.read_text())
                processes = []
                try:
                    processes.append(client(args, out / 'long.jsonl', case / 'long',
                                            1, 1, 1, 45056, 45056))
                    deadline = time.monotonic() + 120
                    while True:
                        assert processes[0].poll() is None
                        assert time.monotonic() < deadline
                        with urllib.request.urlopen(
                                f'http://127.0.0.1:{args.port}/metrics', timeout=10) as response:
                            metrics = response.read().decode()
                        match = re.search(r'^q4t_requests_total (\d+)$', metrics, re.M)
                        assert match
                        if int(match[1]) == repeat * 3 + 1:
                            break
                        time.sleep(0.2)
                    processes.append(client(args, fixture, case / 'shorts',
                                            2, 2, 16, 1024, 4096))
                    for process in processes:
                        assert process.wait(timeout=900) == 0
                finally:
                    for process in processes:
                        if process.poll() is None:
                            process.terminate()
                            try:
                                process.wait(timeout=10)
                            except subprocess.TimeoutExpired:
                                process.kill()
                                process.wait()
                long = results(case / 'long')
                short = results(case / 'shorts')
                assert len(long) == 1 and len(short) == 2
                assert long[0]['prompt_tokens'] == 45056
                assert sorted(x['prompt_tokens'] for x in short) == [1024, 4096]
                for response in long + short:
                    assert response['success']
                    assert response['completion_tokens'] == (1 if response in long else 16)
                    key = response['prompt_sha256']
                    assert key == input_hashes[response['prompt_tokens']]
                    expected.setdefault(key, response['output_sha256'])
                    assert expected[key] == response['output_sha256'], (group, repeat, response)
                observations = re.findall(
                    r'\[prefill-batch-readback\] rows=(\d+) stride=(-?\d+) ok=(\d+)',
                    log_path.read_text()[log_start:])
                save(case / 'coverage.json', observations)
                assert len(observations) == 1, observations
                rows, stride, ok = map(int, observations[0])
                assert rows == 2 and ok == 1
                selected_rows = version == 'candidate' or args.parent_selected_rows
                allowed = [496640] if selected_rows else [n * 496640 for n in lengths]
                assert stride in allowed, observations
                assert all(long[0]['start_time'] < x['start_time'] < long[0]['completed_time']
                           for x in short)
                item = {'group': group, 'repeat': repeat, 'long': long, 'short': short,
                        'coverage': observations,
                        'makespan_s': max(x['completed_time'] for x in long + short) - long[0]['start_time']}
                all_results.append(item)
                save(out / 'results.json', all_results)
                print(group, repeat, 'B=2 confirmed', flush=True)
        finally:
            server.terminate()
            try:
                server.wait(timeout=30)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
            save(directory / 'exit.json', {'server': server.returncode})
        assert server.returncode == 0
    adverse, comparisons = [], []
    for length in [1024, 4096, 45056, None]:
        def metric(item):
            if length is None:
                return item['makespan_s']
            return next(x['first_chunk_latency'] for x in item['long'] + item['short']
                        if x['prompt_tokens'] == length)
        new = [metric(x) for x in all_results if x['group'] == 'candidate']
        for group in ['parent-before', 'parent-after']:
            old = [metric(x) for x in all_results if x['group'] == group]
            bad = min(new) > max(old)
            comparisons.append({'length': length, 'reference': group,
                                'old_range': [min(old), max(old)],
                                'new_range': [min(new), max(new)], 'adverse': bad})
            if bad:
                adverse.append([length, group])
    save(out / 'review.json', {'http_outputs_passed': True, 'batch_coverage_passed': True,
                              'comparisons': comparisons, 'adverse': adverse,
                              'accepted': not adverse,
                              'limits': 'Three fixed requests, three repetitions; readback observer active in all versions.'})
    assert not adverse, 'Performance ranges require review'


if __name__ == '__main__':
    main()
