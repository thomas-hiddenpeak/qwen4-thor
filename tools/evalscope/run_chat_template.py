"""Replay frozen model-native prompts against the real messages HTTP entrypoint."""
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
FIXTURES = ROOT / 'tools/evalscope/fixtures/chat_template_cases.jsonl'


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--binary', type=Path, default=ROOT / 'build/q4t')
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    model, out, binary = (x.resolve() for x in
                          [args.model_dir, args.output, args.binary])
    if not any(out.is_relative_to(ROOT / d) for d in ['build', '.q4t-work']):
        parser.error('--output must be under build/ or .q4t-work/')
    meta = json.loads(FIXTURES.with_suffix('.metadata.json').read_text())
    assert sha(FIXTURES) == meta['fixtures_sha256']
    assert sha(model / 'chat_template.jinja') == meta['template_sha256']
    assert sha(model / 'tokenizer.json') == meta['tokenizer_sha256']
    # Own one service, without killing an existing process or probing HTTP.
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            argv = (proc / 'cmdline').read_bytes().split(b'\0')
            exe = (proc / 'exe').resolve(strict=True)
        except OSError:
            continue
        assert not (exe.name.startswith('q4t') and b'serve' in argv), \
            'another q4t service is running'
    out.mkdir(parents=True, exist_ok=False)
    shutil.copy2(__file__, out / 'run_chat_template.py')
    shutil.copy2(FIXTURES, out / FIXTURES.name)
    save(out / 'fixtures-metadata.json', meta)
    save(out / 'runtime.json', {'binary': str(binary), 'sha256': sha(binary),
                              'model_dir': str(model)})
    (out / 'commit.txt').write_bytes(subprocess.check_output(
        ['git', 'rev-parse', 'HEAD'], cwd=ROOT))
    (out / 'worktree.patch').write_bytes(subprocess.check_output(
        ['git', 'diff', 'HEAD'], cwd=ROOT))
    source_files = ['CMakeLists.txt', 'include/q4t/io/json.h', 'src/io/json.cpp',
                    'include/q4t/server/chat_template.h',
                    'src/server/chat_template.cpp', 'src/server/chat_server.cpp']
    save(out / 'source-hashes.json', {f: sha(ROOT / f) for f in source_files})
    cases = [json.loads(line) for line in FIXTURES.read_text().splitlines()]
    manifest = []
    for case in cases:
        for form in ['native-before', 'messages', 'native-after']:
            request = {'temperature': 0, 'max_tokens': 32, 'stream': True,
                       'stream_options': {'include_usage': True}}
            if form == 'messages':
                request.update(case['request'])
            else:
                request['prompt'] = case['expected']
            manifest.append({'case': case['id'], 'form': form,
                             'expected_answer': case['answer'],
                             'prompt_tokens': case['prompt_tokens'],
                             'request': request})
    save(out / 'manifest.json', manifest)
    (out / 'requests.jsonl').write_text(''.join(
        json.dumps(x['request'], ensure_ascii=False) + '\n' for x in manifest))
    env = {k: v for k, v in os.environ.items()
           if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}
    command = [str(binary), 'serve', '--model-dir', str(model), '--port',
               str(args.port), '--max-seq', '1', '--max-prefill', '8192',
               '--max-len', '208896', '--max-tokens', '32', '--no-mtp']
    save(out / 'server-command.json', command)
    server = None
    completed = 0
    failure = None
    passed = False
    try:
        with (out / 'server.log').open('w') as log:
            server = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log,
                                      stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 240
        while 'serving on port' not in (out / 'server.log').read_text():
            assert server.poll() is None, 'server exited during startup'
            assert time.monotonic() < deadline, 'startup timeout'
            time.sleep(1)
        assert 'MTP disabled; plain decode' in (out / 'server.log').read_text()
        client = [str(ROOT / 'tools/evalscope/.venv/bin/evalscope'), 'perf',
                  '--model', 'qwen3.8-flash-next', '--url',
                  f'http://127.0.0.1:{args.port}/v1/chat/completions',
                  '--api', 'openai', '--tokenizer-path', str(model),
                  '--dataset', 'line_by_line', '--dataset-path',
                  str(out / 'requests.jsonl'), '--min-prompt-length', '1',
                  '--max-prompt-length', '4096', '--no-apply-chat-template',
                  '--max-tokens', '32', '--temperature', '0', '--seed',
                  '20260920', '--parallel', '1', '--number', str(len(manifest)),
                  '--warmup-num', '0', '--no-test-connection', '--stream',
                  '--connect-timeout', '30', '--read-timeout', '7200',
                  '--total-timeout', '10800', '--outputs-dir', str(out / 'client')]
        save(out / 'client-command.json', client)
        with (out / 'client.log').open('w') as log:
            code = subprocess.run(client, cwd=ROOT, env=env, stdout=log,
                                  stderr=subprocess.STDOUT).returncode
        save(out / 'client-exit.json', {'exit_code': code})
        database = next((out / 'client').rglob('benchmark_data.db'))
        # Only read trusted local evalscope databases: response storage is pickle.
        with sqlite3.connect('file:' + str(database) + '?mode=ro', uri=True) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute('select * from result order by start_time').fetchall()
        completed = len(rows)
        assert completed == len(manifest), 'missing HTTP records'
        parsed = []
        for row, item in zip(rows, manifest):
            request = json.loads(row['request'])
            assert all(request[k] == v for k, v in item['request'].items())
            assert ('messages' in request) == (item['form'] == 'messages')
            assert ('prompt' in request) == (item['form'] != 'messages')
            messages = pickle.loads(base64.b64decode(row['response_messages']))
            choices = [c for m in messages for c in m.get('choices', [])]
            text = ''.join(c.get('delta', c.get('message', {})).get('content', '')
                           for c in choices)
            finish = [c['finish_reason'] for c in choices if c.get('finish_reason')]
            parsed.append({'case': item['case'], 'form': item['form'],
                           'success': bool(row['success']), 'text': text,
                           'prompt_tokens': row['prompt_tokens'],
                           'completion_tokens': row['completion_tokens'],
                           'finish': finish,
                           'length_matches': row['prompt_tokens'] == item['prompt_tokens'],
                           'answer_correct': None if item['expected_answer'] is None
                           else text.strip() == item['expected_answer'] and finish == ['stop']})
        save(out / 'responses.json', parsed)
        checks = []
        keys = ['text', 'prompt_tokens', 'completion_tokens', 'finish']
        for case in cases:
            group = [x for x in parsed if x['case'] == case['id']]
            checks.append({'case': case['id'], 'equal': len(group) == 3 and all(
                x['success'] and x['length_matches'] and x['answer_correct'] is not False
                and all(x[k] == group[0][k] for k in keys) for x in group)})
        passed = code == 0 and all(x['equal'] for x in checks)
        save(out / 'comparison.json', {'cases': checks,
             'template_checks_passed': passed,
             'fixed_answer_responses': sum(x['answer_correct'] is not None for x in parsed),
             'correct_answer_responses': sum(x['answer_correct'] is True for x in parsed),
             'performance_accepted': False})
        assert passed, 'template HTTP comparison failed'
    except BaseException as error:
        failure = repr(error)
        raise
    finally:
        if server is not None and server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=30)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
        save(out / 'exit.json', {'server': server.returncode if server else None,
                                'completed': completed, 'template_checks_passed': passed,
                                'failure': failure, 'runtime_accepted': False})
    assert server.returncode == 0, 'server did not exit normally'


if __name__ == '__main__':
    main()
