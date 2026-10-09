"""Fixed real-HTTP MTP context-tail and scheduler-fallback regressions.

Three servers, four evalscope requests each; no performance acceptance.
The preload fault is test-only and fails the second 496640-byte cudaMalloc.
Only deserialize databases created locally by this invocation.
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

from response_identity import response_identity

ROOT = Path(__file__).resolve().parents[2]
GROUPS = [('plain', False, False), ('mtp', True, False),
          ('mtp-scheduler-fault', True, True)]
CASES = [(length, streaming) for length in [26, 29]
         for streaming in [True, False]]
PREPARE = '''import hashlib, json, sys
from pathlib import Path
from transformers import AutoTokenizer
model, destination = sys.argv[1:]
out = Path(destination)
tokenizer = AutoTokenizer.from_pretrained(model, local_files_only=True)
text = ("Write a detailed explanation of how a language model processes a request. "
        "Continue with several paragraphs about memory, scheduling, numerical "
        "correctness, and how to compare two implementations carefully.")
source_ids = tokenizer.encode(text, add_special_tokens=False)
manifest = []
for length in [26, 29]:
    ids = source_ids[:length]
    prompt = tokenizer.decode(ids, clean_up_tokenization_spaces=False)
    actual = tokenizer.encode(prompt, add_special_tokens=False)
    if len(ids) != length or actual != ids:
        raise RuntimeError("fixed truncated prompt did not round-trip exactly")
    (out / f"context-{length}.jsonl").write_text(
        json.dumps({"prompt": prompt}, ensure_ascii=False) + "\\n")
    manifest.append({"length": length, "token_ids": ids,
                     "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest()})
(out / "manifest.json").write_text(json.dumps(manifest, indent=2))
'''


def save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False))


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def records(log, prefix):
    result = []
    for line in log.splitlines():
        if not line.startswith(prefix):
            continue
        item = {}
        for token in line[len(prefix):].split():
            key, separator, value = token.partition('=')
            require(separator and key and value and key not in item,
                    f'malformed log record: {line}')
            item[key] = value
        result.append({'fields': item, 'raw': line})
    return result


def startup_contract(log, mtp, fault):
    effective = records(log, '[q4t][capabilities] effective ')
    capacity = records(log, '[q4t][capacity] ')
    require(len(effective) == len(capacity) == 1,
            'missing or duplicate actual capabilities/capacity')
    for key, expected in {'mtp': str(int(mtp)), 'media_allowed': '0',
                          'vision_loaded': '0', 'max_seq': '1'}.items():
        require(effective[0]['fields'].get(key) == expected,
                f'actual capability {key} differs from {expected}')
    expected_capacity = {'requested_max_len': '32', 'effective_max_len': '32',
                         'requested_max_seq': '1', 'effective_max_seq': '1',
                         'requested_max_prefill': '16',
                         'effective_max_prefill': '16', 'budget_enabled': '1',
                         'budget_feasible': 'true'}
    for key, expected in expected_capacity.items():
        require(capacity[0]['fields'].get(key) == expected,
                f'actual capacity {key} differs from {expected}')
    if mtp:
        require('[q4t] MTP loaded (k=3 max_seq=1)' in log,
                'MTP did not remain loaded with k=3 and max_seq=1')
    observed = records(log, '[mtp-scheduler-fault] ')
    failed = '[q4t] scheduler logits alloc failed; plain decode'
    started = '[q4t] continuous-batching scheduler started (max_seq=1)'
    if fault:
        expected = [
            {'ordinal': '1', 'bytes': '496640', 'action': 'pass', 'result': '0'},
            {'ordinal': '2', 'bytes': '496640', 'action': 'inject', 'result': '2'},
            {'ordinal': '3', 'bytes': '496640', 'action': 'pass', 'result': '0'},
        ]
        require([row['fields'] for row in observed] == expected,
                'fixed allocation ordinals differ: expected MTP logits #1, '
                'scheduler failure #2, prefill logits #3; rule was not adjusted')
        require(log.count(failed) == 1 and started not in log,
                'scheduler fault missing, repeated, or scheduler still started')
        loaded_at = log.index('[q4t] MTP loaded (k=3 max_seq=1)')
        require(log.index(observed[0]['raw']) < loaded_at <
                log.index(observed[1]['raw']) < log.index(failed) <
                log.index(observed[2]['raw']),
                'injection did not match the scheduler allocation after MTP load')
    else:
        require(not observed and failed not in log and started in log,
                'unexpected scheduler failure or preload injection')
    return {'effective_capabilities': effective, 'capacity': capacity,
            'fault_allocations_before_http': observed, 'passed': True}


def path_contract(log, response, mtp, fault):
    matched = [row for row in records(log, '[q4t][decode_path] ')
               if row['fields'].get('id') == response['response_id']]
    require(len(matched) == 1, 'missing or repeated terminal path for actual HTTP ID')
    fields = matched[0]['fields']
    response['execution_path'] = matched[0]
    require(fields.get('requested_mtp') == str(int(mtp)),
            'terminal requested_mtp differs from service group')
    for key in ['mtp_steps', 'plain_tail_tokens']:
        require(fields.get(key, '').isascii() and fields.get(key, '').isdigit(),
                f'invalid path work count: {key}')
    steps, tail = int(fields['mtp_steps']), int(fields['plain_tail_tokens'])
    require(0 <= tail <= response['actual_output'], 'plain tail exceeds output count')
    if not mtp:
        expected = ('plain', 'none', 0, 0)
    elif response['actual_input'] == 29:
        expected = ('plain', 'context_tail', 0, 0)
    elif fault:
        expected = ('plain', 'scheduler_unavailable', 0, 0)
    else:
        require(fields.get('path') == 'mtp_multi_b1' and
                fields.get('fallback') == 'none' and steps > 0,
                '26-token case did not execute actual B=1 MTP')
        require(tail > 0, '26-token output may be correct but required plain-tail '
                'coverage is absent; do not retune the fixed input silently')
        response['boundary_case'] = 'mtp_then_plain_tail'
        return
    require((fields.get('path'), fields.get('fallback'), steps, tail) == expected,
            f'execution path differs from expected {expected}')
    response['boundary_case'] = expected[1] if mtp else 'ordinary_context_limit'


def read_response(case):
    databases = list(case.rglob('benchmark_data.db'))
    require(len(databases) == 1, f'expected one local database, got {len(databases)}')
    with sqlite3.connect(databases[0]) as db:
        rows = db.execute('select success,prompt_tokens,completion_tokens,'
                          'response_messages,first_chunk_latency,latency,request '
                          'from result order by start_time').fetchall()
    parsed = []
    for row in rows:
        messages = pickle.loads(base64.b64decode(row[3]))
        choices = [choice for msg in messages for choice in msg.get('choices', [])]
        text = ''.join(choice.get('delta', choice.get('message', {})).get('content') or ''
                       for choice in choices)
        request = json.loads(row[6])
        parsed.append({'success': row[0], 'actual_input': row[1],
                       'actual_output': row[2], 'text': text,
                       'finish': [c['finish_reason'] for c in choices if c.get('finish_reason')],
                       'request': request, 'response_messages': messages,
                       'prompt_sha256': hashlib.sha256(request['prompt'].encode()).hexdigest(),
                       'ttft_raw': row[4], 'latency_raw': row[5],
                       **response_identity(messages)})
    save(case / 'responses.json', parsed)
    require(len(parsed) == 1, f'expected one HTTP response, got {len(parsed)}')
    return parsed[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--model-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--port', type=int, default=18094)
    parser.add_argument('--startup-timeout', type=int, default=180)
    args = parser.parse_args()
    out = args.output.resolve()
    if not any(out.is_relative_to(ROOT / part) for part in ['build', '.q4t-work']):
        parser.error('output must be under build/ or .q4t-work/')
    if args.startup_timeout <= 0 or not 1 <= args.port <= 65535:
        parser.error('positive startup-timeout and valid port required')
    out.mkdir(parents=True, exist_ok=False)
    binary, model = args.binary.resolve(), args.model_dir.resolve()
    env = {key: value for key, value in os.environ.items()
           if not key.startswith('Q4T_') and key != 'LD_PRELOAD'}
    removed = {key: value for key, value in os.environ.items() if key not in env}
    evalscope = ROOT / 'tools/evalscope/.venv/bin/evalscope'
    python = evalscope.with_name('python')
    for source in [Path(__file__), Path(__file__).with_name('response_identity.py'),
                   ROOT / 'tools/verify/mtp_scheduler_fault.cpp.in']:
        shutil.copy2(source, out / source.name)
    binary_hash = hashlib.sha256(binary.read_bytes()).hexdigest()
    cache = binary.parent / 'CMakeCache.txt'
    deployment = binary.with_name(binary.name + '.release.json')
    if deployment.is_file():
        identity = json.loads(deployment.read_text())
        require(identity['binary_sha256'] == binary_hash, 'stale deployment identity')
        cache = Path(identity['build_cache'])
        require(hashlib.sha256(cache.read_bytes()).hexdigest() == identity['build_cache_sha256'],
                'deployment build-cache identity differs')
        shutil.copy2(deployment, out / 'binary.release.json')
    shutil.copy2(cache, out / 'CMakeCache.txt')
    save(out / 'identity.json', {
        'binary': str(binary), 'binary_sha256': binary_hash,
        'runner_source_commit': subprocess.check_output(
            ['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'model_dir': str(model), 'removed_environment': removed,
        'capacity': {'max_seq': 1, 'max_prefill': 16, 'max_len': 32},
        'max_tokens': 16, 'groups': GROUPS, 'cases': CASES,
        'performance_acceptance': False,
    })
    (out / 'worktree.patch').write_bytes(subprocess.check_output(
        ['git', 'diff', 'HEAD'], cwd=ROOT))
    (out / 'evalscope-version.txt').write_bytes(subprocess.check_output(
        [str(python), '-c', 'from importlib.metadata import version; print(version("evalscope"))']))
    inputs = out / 'inputs'
    inputs.mkdir()
    (out / 'prepare.py').write_text(PREPARE)
    subprocess.run([str(python), str(out / 'prepare.py'), str(model), str(inputs)],
                   env=env, check=True)
    manifest = {row['length']: row for row in json.loads((inputs / 'manifest.json').read_text())}
    shim = out / 'mtp_scheduler_fault.so'
    build_command = ['g++-14', '-std=c++23', '-Wall', '-Wextra', '-Werror',
                     '-shared', '-fPIC', '-I/usr/local/cuda/include', '-x', 'c++',
                     str(out / 'mtp_scheduler_fault.cpp.in'), '-ldl', '-o', str(shim)]
    save(out / 'shim-build-command.json', build_command)
    with (out / 'shim-build.log').open('w') as log:
        subprocess.run(build_command, stdout=log, stderr=subprocess.STDOUT, check=True)
    (out / 'shim.sha256').write_text(hashlib.sha256(shim.read_bytes()).hexdigest())

    summaries, ordinary = [], {}
    for name, mtp, fault in GROUPS:
        group = out / name
        group.mkdir()
        server_command = [str(binary), 'serve', '--model-dir', str(model),
                          '--port', str(args.port), '--max-seq', '1',
                          '--max-prefill', '16', '--max-len', '32',
                          '--max-tokens', '16', '--mtp' if mtp else '--no-mtp']
        server_env = dict(env)
        if fault:
            server_env['LD_PRELOAD'] = str(shim)
        save(group / 'server-command.json', {'argv': server_command,
                                            'ld_preload': server_env.get('LD_PRELOAD'),
                                            'startup_timeout': args.startup_timeout})
        report = {'group': name, 'responses': [], 'failures': []}
        with (group / 'server.log').open('w') as log:
            server = subprocess.Popen(server_command, cwd=ROOT, env=server_env,
                                      stdout=log, stderr=subprocess.STDOUT)
            try:
                for _ in range(args.startup_timeout):
                    require(server.poll() is None, 'server stopped during startup')
                    if 'serving on port' in (group / 'server.log').read_text():
                        break
                    time.sleep(1)
                else:
                    raise RuntimeError('server startup timeout')
                startup = (group / 'server.log').read_text()
                report['startup'] = startup_contract(startup, mtp, fault)
                for length, streaming in CASES:
                    case = group / f'context-{length}-{"stream" if streaming else "nonstream"}'
                    case.mkdir()
                    request_file = inputs / f'context-{length}.jsonl'
                    shutil.copy2(request_file, case / 'requests.jsonl')
                    command = [str(evalscope), 'perf', '--model', 'qwen3.8-flash-next',
                               '--url', f'http://127.0.0.1:{args.port}/v1/chat/completions',
                               '--api', 'openai', '--tokenizer-path', str(model),
                               '--dataset', 'line_by_line', '--dataset-path', str(request_file),
                               '--min-prompt-length', str(length), '--max-prompt-length', str(length),
                               '--no-apply-chat-template', '--max-tokens', '16',
                               '--temperature', '0', '--seed', '20260920', '--parallel', '1',
                               '--number', '1', '--warmup-num', '0', '--connect-timeout', '30',
                               '--read-timeout', '180', '--total-timeout', '240',
                               '--no-test-connection', '--outputs-dir', str(case),
                               '--stream' if streaming else '--no-stream']
                    save(case / 'command.json', command)
                    try:
                        with (case / 'client.log').open('w') as client:
                            completed = subprocess.run(command, cwd=ROOT, env=env,
                                                       stdout=client, stderr=subprocess.STDOUT,
                                                       timeout=300, check=False)
                        save(case / 'client-exit.json', {'returncode': completed.returncode})
                        response = read_response(case)
                        response.update({'case': case.name, 'streaming': streaming})
                        report['responses'].append(response)
                        require(completed.returncode == 0 and response['success'],
                                'evalscope or HTTP request failed')
                        require(response['response_id_valid'], 'invalid or mixed actual HTTP IDs')
                        require(response['actual_input'] == length and
                                response['actual_output'] == 32 - length and
                                response['finish'] == ['length'],
                                'usage or context-limit finish mismatch')
                        require(response['request'].get('stream') == streaming and
                                response['request'].get('max_tokens') == 16,
                                'actual stream/max_tokens differs from fixed request')
                        require(response['prompt_sha256'] == manifest[length]['prompt_sha256'],
                                'actual HTTP prompt differs from frozen prompt')
                        require(bool(response['text']), 'empty successful output is insufficient')
                        if not mtp:
                            if length in ordinary:
                                require(response['text'] == ordinary[length],
                                        'plain streaming/nonstreaming outputs differ')
                            else:
                                ordinary[length] = response['text']
                        else:
                            require(length in ordinary and response['text'] == ordinary[length],
                                    'MTP/fallback text differs from ordinary context-limited output')
                    except Exception as error:
                        report['failures'].append({'case': case.name, 'failure': repr(error)})
                        save(case / 'failure.json', {'failure': repr(error)})
                    save(group / 'result.json', report)
            except Exception as error:
                report['failures'].append({'stage': 'startup', 'failure': repr(error)})
            finally:
                server.terminate()
                try:
                    server.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()
                report['server_exit'] = server.returncode
                final_log = (group / 'server.log').read_text()
                for response in report['responses']:
                    try:
                        path_contract(final_log, response, mtp, fault)
                    except Exception as error:
                        report['failures'].append({'case': response['case'],
                                                   'path_failure': repr(error)})
                if fault:
                    injections = records(final_log, '[mtp-scheduler-fault] ')
                    if sum(r['fields'].get('action') == 'inject' for r in injections) != 1:
                        report['failures'].append({'fault_failure': 'injection did not fire exactly once'})
                if server.returncode != 0:
                    report['failures'].append({'server_failure': f'exit {server.returncode}'})
                report['passed'] = not report['failures'] and len(report['responses']) == len(CASES)
                save(group / 'result.json', report)
                save(group / 'exit.json', {'server_exit': server.returncode,
                                            'completed': len(report['responses']),
                                            'passed': report['passed']})
        summaries.append(report)
        save(out / 'summary.json', {'passed': len(summaries) == len(GROUPS) and
                                               all(r['passed'] for r in summaries),
                                     'completed_groups': len(summaries), 'groups': summaries,
                                     'performance_acceptance': False})
        print(f'{name}: {"PASS" if report["passed"] else "FAIL"}', flush=True)
    require(len(summaries) == 3 and all(row['passed'] for row in summaries),
            'fixed MTP boundary matrix failed; original results retained')


if __name__ == '__main__':
    main()
