"""Run real evalscope HTTP quality/performance checks, owning one server.

No connection probe, unit test, microbenchmark or profile precedes the requests.
Only deserialize evalscope databases created locally by this invocation.
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


def save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['quality', 'performance', 'limits'], required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--binary', type=Path, default=ROOT / 'build/q4t')
    parser.add_argument('--model-dir', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--startup-timeout', type=int, default=180,
                        help='Seconds to wait for model loading before HTTP requests')
    parser.add_argument('--fixtures', type=Path,
                        help='Existing quality-inputs or performance matrix root')
    parser.add_argument('--reference', type=Path,
                        help='Prior results.json: require identical prompts and outputs')
    args = parser.parse_args()
    if args.startup_timeout <= 0:
        parser.error('startup-timeout must be positive')
    out = args.output.resolve()
    if not any(out.is_relative_to(ROOT / d) for d in ['build', '.q4t-work']):
        parser.error('output must be under build/ or .q4t-work/')
    out.mkdir(parents=True, exist_ok=False)
    binary = args.binary.resolve()
    model = args.model_dir.resolve()
    env = os.environ.copy()
    removed = {}
    for key in list(env):
        if key.startswith(('Q4T_FP8', 'Q4T_PROFILE', 'Q4T_MTP_TIMING',
                           'Q4T_SCHED_DEBUG', 'Q4T_ACCESS_LOG')):
            removed[key] = env.pop(key)
    (out / 'binary.sha256').write_text(hashlib.sha256(binary.read_bytes()).hexdigest())
    (out / 'commit.txt').write_bytes(subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT))
    (out / 'worktree.patch').write_bytes(subprocess.check_output(['git', 'diff', 'HEAD'], cwd=ROOT))
    shutil.copyfile(__file__, out / 'run_acceptance.py')
    shutil.copyfile(ROOT / 'build/CMakeCache.txt', out / 'CMakeCache.txt')
    evalscope = ROOT / 'tools/evalscope/.venv/bin/evalscope'
    python = evalscope.with_name('python')
    (out / 'evalscope-version.txt').write_bytes(subprocess.check_output(
        [str(python), '-c', 'from importlib.metadata import version; print(version("evalscope"))']))
    prepared = out / 'inputs'
    if args.mode == 'quality':
        if args.fixtures:
            shutil.copytree(args.fixtures, prepared)
        else:
            subprocess.run([str(python), str(ROOT / 'tools/evalscope/prepare_quality.py'),
                            '--model-dir', str(model), '--output', str(prepared)], check=True)
        manifest = json.loads((prepared / 'manifest.json').read_text())
        cases = [(out / 'quality', prepared / 'requests.jsonl', len(manifest), 1, 208896, 32, True)]
    elif args.mode == 'performance':
        prepared.mkdir()
        cases = []
        for length in LENGTHS:
            target = prepared / f'context-{length}.jsonl'
            if args.fixtures:
                first = (args.fixtures / f'context-{length}/requests.jsonl').read_text().splitlines()[0]
            else:
                subprocess.run([str(python), str(ROOT / 'tools/evalscope/prepare_inputs.py'),
                                '--model-dir', str(model), '--length', str(length),
                                '--number', '1', '--output', str(target)], check=True)
                first = target.read_text().splitlines()[0]
            target.write_text((first + '\n') * 3)
            cases.append((out / f'context-{length}', target, 3, length, length, 256, True))
    else:
        prepared.mkdir()
        target = prepared / 'context-1024.jsonl'
        subprocess.run([str(python), str(ROOT / 'tools/evalscope/prepare_inputs.py'),
                        '--model-dir', str(model), '--length', '1024', '--number', '1',
                        '--output', str(target)], check=True)
        cases = [(out / f'{"stream" if streaming else "nonstream"}-{limit}',
                  target, 1, 1024, 1024, limit, streaming)
                 for streaming in [True, False] for limit in [1, 2, 8]]
    reference = json.loads(args.reference.read_text()) if args.reference else None
    command = [str(binary), 'serve', '--model-dir', str(model), '--port', str(args.port),
               '--max-seq', '1', '--max-prefill', '8192', '--max-len', '208896',
               '--max-tokens', '256', '--no-mtp']
    save(out / 'server-command.json', {'argv': command, 'removed_environment': removed,
                                       'startup_timeout_seconds': args.startup_timeout})
    results = []
    passed = False
    failure = None
    with (out / 'server.log').open('w') as log:
        server = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            for _ in range(args.startup_timeout):
                if server.poll() is not None:
                    raise RuntimeError('server exited during startup')
                if 'serving on port' in (out / 'server.log').read_text():
                    break
                time.sleep(1)
            else:
                raise RuntimeError(f'server startup timeout after {args.startup_timeout}s')
            for case, inputs, count, minimum, maximum, tokens, streaming in cases:
                case.mkdir()
                shutil.copyfile(inputs, case / 'requests.jsonl')
                cmd = [str(evalscope), 'perf', '--model', 'qwen3.8-flash-next',
                       '--url', f'http://127.0.0.1:{args.port}/v1/chat/completions',
                       '--api', 'openai', '--tokenizer-path', str(model),
                       '--dataset', 'line_by_line', '--dataset-path', str(inputs),
                       '--min-prompt-length', str(minimum), '--max-prompt-length', str(maximum),
                       '--no-apply-chat-template', '--max-tokens', str(tokens),
                       '--temperature', '0', '--seed', '20260920', '--parallel', '1',
                       '--number', str(count), '--warmup-num', '0', '--connect-timeout', '30',
                       '--read-timeout', '7200', '--total-timeout', '10800',
                       '--no-test-connection', '--outputs-dir', str(case)]
                cmd.append('--stream' if streaming else '--no-stream')
                save(case / 'command.json', cmd)
                with (case / 'client.log').open('w') as client:
                    subprocess.run(cmd, cwd=ROOT, env=env, stdout=client,
                                   stderr=subprocess.STDOUT, check=True)
                with sqlite3.connect(next(case.rglob('benchmark_data.db'))) as db:
                    rows = db.execute('select success,prompt_tokens,completion_tokens,'
                                      'response_messages,first_chunk_latency,latency,request '
                                      'from result order by start_time').fetchall()
                parsed = []
                for i, row in enumerate(rows):
                    messages = pickle.loads(base64.b64decode(row[3]))
                    choices = [choice for msg in messages for choice in msg.get('choices', [])]
                    text = ''.join(c.get('delta', c.get('message', {})).get('content', '') for c in choices)
                    finish = [c['finish_reason'] for c in choices if c.get('finish_reason')]
                    wire_request = json.loads(row[6])
                    prompt = wire_request['prompt']
                    parsed.append({'success': row[0], 'actual_input': row[1],
                                   'actual_output': row[2], 'text': text, 'finish': finish,
                                   'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
                                   'ttft': row[4], 'latency': row[5],
                                   'request_stream': wire_request.get('stream')})
                    (case / f'output-{i}.txt').write_text(text)
                # Preserve failure evidence before checking acceptance.
                save(case / 'responses.json', parsed)
                if len(rows) != count or not all(r['success'] for r in parsed):
                    raise RuntimeError('request count or HTTP success mismatch')
                if args.mode == 'quality':
                    expected = {r['prompt_sha256']: r for r in manifest}
                    for row in parsed:
                        row.update(expected[row['prompt_sha256']])
                        row['exact_match'] = row['text'].strip() == row['expected']
                        row['length_match'] = row['actual_input'] == row['length']
                    results = parsed
                    save(out / 'results.json', results)
                    if len({r['id'] for r in parsed}) != count or not all(
                            r['exact_match'] and r['length_match'] and r['finish'] == ['stop'] for r in parsed):
                        raise RuntimeError('quality acceptance failed')
                    if reference:
                        old = {r['id']: r for r in reference}
                        if any(r['text'] != old[r['id']]['text'] or
                               r['prompt_sha256'] != old[r['id']]['prompt_sha256'] for r in parsed):
                            raise RuntimeError('quality output/prompt differs from reference')
                elif args.mode == 'limits':
                    row = parsed[0]
                    row.update({'id': case.name, 'streaming': streaming,
                                'max_tokens': tokens})
                    results.append(row)
                    save(out / 'results.json', results)
                    if (row['actual_input'] != 1024 or row['actual_output'] != tokens or
                            row['finish'] != ['length'] or row['request_stream'] != streaming):
                        raise RuntimeError('output-limit/finish/stream acceptance failed')
                    if reference:
                        old = next(r for r in reference if r['id'] == row['id'])
                        if (row['text'] != old['text'] or
                                row['prompt_sha256'] != old['prompt_sha256']):
                            raise RuntimeError('output-limit prompt/text differs from reference')
                else:
                    hashes = [hashlib.sha256(r['text'].encode()).hexdigest() for r in parsed]
                    item = {'length': minimum, 'outputs': hashes,
                            'prompt_sha256': parsed[0]['prompt_sha256'],
                            'deterministic': len(set(hashes)) == 1,
                            'metrics': [{'ttft': r['ttft'], 'decode_tps':
                                         (r['actual_output'] - 1) / (r['latency'] - r['ttft'])} for r in parsed]}
                    results.append(item)
                    save(out / 'results.json', results)
                    if not item['deterministic'] or len({r['prompt_sha256'] for r in parsed}) != 1 or not all(
                            r['actual_input'] == minimum and r['actual_output'] == 256 and
                            r['finish'] == ['length'] for r in parsed):
                        raise RuntimeError('performance request/determinism acceptance failed')
                    if reference:
                        old = next(r for r in reference if r['length'] == minimum)
                        expected_prompt = old.get('prompt_sha256')
                        if expected_prompt is None:
                            old_prompt = json.loads((args.reference.parent / f'context-{minimum}/requests.jsonl')
                                                    .read_text().splitlines()[0])['prompt']
                            expected_prompt = hashlib.sha256(old_prompt.encode()).hexdigest()
                        if hashes != old['outputs'] or item['prompt_sha256'] != expected_prompt:
                            raise RuntimeError('performance output/prompt differs from reference')
                print(f'{case.name}: HTTP/output checks passed', flush=True)
            passed = True
        except Exception as error:
            failure = f'{type(error).__name__}: {error}'
            raise
        finally:
            server.terminate()
            try:
                server.wait(timeout=25)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
            save(out / 'exit.json', {'server': server.returncode, 'completed': len(results),
                                      'http_output_checks_passed': passed and server.returncode == 0,
                                      'failure': failure})
        if server.returncode != 0:
            raise RuntimeError('server did not exit normally')


if __name__ == '__main__':
    main()
