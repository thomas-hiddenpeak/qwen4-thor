"""Parent/candidate/parent HTTP comparison for long-prefill/short-decode overlap.

Runs only after the candidate's full single-stream HTTP range gate. Each
request is issued by the bundled evalscope client. No inference warmup/probe.
Only trusted local evalscope response databases are deserialized.
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

ROOT = Path(__file__).resolve().parents[2]


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_result(case, length, tokens):
    with sqlite3.connect(next(case.rglob('benchmark_data.db'))) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute('select * from result order by start_time').fetchall()
    if len(rows) != 1:
        raise RuntimeError('expected one request')
    row = dict(rows[0])
    messages = pickle.loads(base64.b64decode(row['response_messages']))
    text = ''.join(c.get('delta', c.get('message', {})).get('content', '')
                   for m in messages for c in m.get('choices', []))
    request = json.loads(row['request'])
    result = {k: row[k] for k in ['success', 'start_time', 'completed_time',
                                  'latency', 'first_chunk_latency',
                                  'prompt_tokens', 'completion_tokens']}
    result['first_content_time'] = row['start_time'] + row['first_chunk_latency']
    result['prompt_sha256'] = hashlib.sha256(request['prompt'].encode()).hexdigest()
    result['output_sha256'] = hashlib.sha256(text.encode()).hexdigest()
    # These are evalscope response-chunk gaps, not claimed token timestamps.
    gaps = json.loads(row['inter_token_latencies'] or '[]')
    result['max_response_chunk_gap_s'] = max(gaps, default=None)
    (case / 'output.txt').write_text(text)
    save(case / 'parsed.json', result)
    if not row['success'] or row['prompt_tokens'] != length or row['completion_tokens'] != tokens:
        raise RuntimeError('HTTP/length check failed: ' + str(result))
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--accepted-root', type=Path, required=True)
    ap.add_argument('--parent-run', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--model-dir', type=Path, required=True)
    ap.add_argument('--port', type=int, default=8000)
    args = ap.parse_args()
    run, parent, out = (p.resolve() for p in [args.accepted_root, args.parent_run, args.output])
    if not any(out.is_relative_to(ROOT / p) for p in ['build', '.q4t-work']):
        ap.error('output must be under build/ or .q4t-work/')
    gate = json.loads((run / 'acceptance.json').read_text())
    if not gate['quality_passed'] or not gate['performance_accepted']:
        ap.error('full single-stream HTTP range gate required')
    binaries = {'candidate': ROOT / 'build/q4t', 'parent': run / 'q4t-before'}
    for name, baseline in [('candidate', run), ('parent', parent)]:
        digest = sha(binaries[name])
        for mode, count in [('quality', 11), ('performance', 5)]:
            e = json.loads((baseline / mode / 'exit.json').read_text())
            assert e['completed'] == count and e['server'] == 0
            assert e['http_output_checks_passed'] and e['failure'] is None
            assert digest == (baseline / mode / 'binary.sha256').read_text().strip()
    for name in ['src/server/chat_server.cpp', 'include/q4t/server/chat_server.h',
                 'src/model/model.cu', 'include/q4t/model/model.h']:
        assert (ROOT / name).read_bytes() == (run / 'source' / name).read_bytes()
    out.mkdir(parents=True, exist_ok=False)
    shutil.copy2(__file__, out / Path(__file__).name)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}
    fixtures = {}
    for label, length in [('long', 45056), ('short', 1024)]:
        line = (run / f'performance/context-{length}/requests.jsonl').read_text().splitlines()[0]
        fixtures[label] = out / (label + '.jsonl')
        fixtures[label].write_text(line + '\n')
    expected = {}
    results = []
    for group in ['parent-before', 'candidate', 'parent-after']:
        version = 'candidate' if group == 'candidate' else 'parent'
        directory = out / group
        directory.mkdir()
        cmd = [str(binaries[version]), 'serve', '--model-dir', str(args.model_dir),
               '--port', str(args.port), '--max-seq', '2', '--max-prefill', '8192',
               '--max-len', '65536', '--max-tokens', '256', '--no-mtp']
        save(directory / 'server-command.json', cmd)
        (directory / 'binary.sha256').write_text(sha(binaries[version]) + '\n')
        with (directory / 'server.log').open('w') as log:
            server = subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT)
            try:
                for _ in range(180):
                    if server.poll() is not None:
                        raise RuntimeError('server exited during startup')
                    if 'serving on port' in (directory / 'server.log').read_text():
                        break
                    time.sleep(1)
                else:
                    raise RuntimeError('startup timeout')
                startup = (directory / 'server.log').read_text()
                assert '=> max_len=65536 max_seq=2' in startup, startup
                assert 'MTP disabled; plain decode' in startup
                assert 'scheduler started (max_seq=2)' in startup
                for repeat in range(3):
                    processes, logs = [], []
                    round_dir = directory / str(repeat)
                    round_dir.mkdir()
                    try:
                        for label, length, tokens in [('long', 45056, 1), ('short', 1024, 256)]:
                            if label == 'short':
                                # Wait for the long request to enter HandleChat;
                                # metrics queries neither generate nor warm up inference.
                                wanted = repeat * 2 + 1
                                for _ in range(300):
                                    if processes[0].poll() is not None:
                                        raise RuntimeError('long client ended before short submission')
                                    with urllib.request.urlopen(
                                            f'http://127.0.0.1:{args.port}/metrics', timeout=10) as response:
                                        metrics = response.read().decode()
                                    match = re.search(r'^q4t_requests_total (\d+)$', metrics, re.M)
                                    assert match and int(match[1]) <= wanted
                                    if int(match[1]) == wanted:
                                        break
                                    time.sleep(0.2)
                                else:
                                    raise RuntimeError('long request admission timeout')
                                time.sleep(2)
                            case = round_dir / label
                            case.mkdir()
                            client = [str(ROOT / 'tools/evalscope/.venv/bin/evalscope'), 'perf',
                                      '--model', 'qwen3.8-flash-next', '--url',
                                      f'http://127.0.0.1:{args.port}/v1/chat/completions',
                                      '--api', 'openai', '--tokenizer-path', str(args.model_dir),
                                      '--dataset', 'line_by_line', '--dataset-path', str(fixtures[label]),
                                      '--min-prompt-length', str(length), '--max-prompt-length', str(length),
                                      '--no-apply-chat-template', '--max-tokens', str(tokens),
                                      '--temperature', '0', '--seed', '20260920', '--parallel', '1',
                                      '--number', '1', '--warmup-num', '0', '--connect-timeout', '30',
                                      '--read-timeout', '600', '--total-timeout', '900',
                                      '--no-test-connection', '--outputs-dir', str(case), '--stream']
                            save(case / 'command.json', client)
                            log_file = (case / 'client.log').open('w')
                            logs.append(log_file)
                            processes.append(subprocess.Popen(client, env=env, stdout=log_file,
                                                              stderr=subprocess.STDOUT))
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
                        for log_file in logs:
                            log_file.close()
                    pair = {label: read_result(round_dir / label, length, tokens)
                            for label, length, tokens in [('long', 45056, 1), ('short', 1024, 256)]}
                    a, b = pair['long'], pair['short']
                    assert a['start_time'] < b['start_time'] < a['completed_time'], pair
                    for label, response in pair.items():
                        signature = (response['prompt_sha256'], response['output_sha256'])
                        expected.setdefault(label, signature)
                        assert expected[label] == signature, (group, repeat, label)
                    pair.update(group=group, repeat=repeat,
                                makespan_s=max(a['completed_time'], b['completed_time']) - a['start_time'],
                                short_first_before_long_end=b['first_content_time'] < a['completed_time'],
                                short_first_before_long_first=b['first_content_time'] < a['first_content_time'],
                                short_first_lead_s=a['first_content_time'] - b['first_content_time'])
                    save(round_dir / 'result.json', pair)
                    results.append(pair)
                    save(out / 'results.json', results)
                    print(group, repeat, 'short TTFT', round(b['first_chunk_latency'], 3),
                          'overlap', pair['short_first_before_long_end'], flush=True)
            finally:
                server.terminate()
                try:
                    server.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()
                save(directory / 'exit.json', {'server': server.returncode})
            assert server.returncode == 0
    candidates = [x for x in results if x['group'] == 'candidate']
    overlap = all(x['short_first_before_long_first'] for x in candidates)
    new = [x['makespan_s'] for x in candidates]
    adverse = []
    for group in ['parent-before', 'parent-after']:
        old = [x['makespan_s'] for x in results if x['group'] == group]
        if min(new) > max(old):
            adverse.append(group)
    save(out / 'review.json', {'http_outputs_passed': True,
                              'candidate_overlap_all_rounds': overlap,
                              'makespan_adverse_disjoint_vs': adverse,
                              'accepted': overlap and not adverse,
                              'limits': 'Three rounds, two fixed requests; response gaps are not token timestamps.'})
    if not overlap or adverse:
        raise RuntimeError('concurrent acceptance requires review')


if __name__ == '__main__':
    main()
