"""Post-standard-gate HTTP comparisons for inline, vision and MTP consumers.

Every inference request uses tools/evalscope. Parent-before/candidate/parent-
after retain all repetitions; no warmup or connection probe is sent.
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

from run_chunk_interleave import ROOT, save, sha


def parse(case, payload, expect_empty_stop=False, expected_count=3):
    with sqlite3.connect(next(case.rglob('benchmark_data.db'))) as db:
        db.row_factory = sqlite3.Row
        records = db.execute('select * from result order by start_time').fetchall()
    parsed = []
    for row in records:
        actual = json.loads(row['request'])
        for key in ['prompt', 'messages']:
            if key in payload:
                assert actual[key] == payload[key], 'request payload changed'
        messages = pickle.loads(base64.b64decode(row['response_messages']))
        text = ''.join(c.get('delta', c.get('message', {})).get('content', '')
                       for m in messages for c in m.get('choices', []))
        item = {k: row[k] for k in ['success', 'prompt_tokens', 'completion_tokens',
                                   'latency', 'first_chunk_latency']}
        item.update(text=text, output_sha256=hashlib.sha256(text.encode()).hexdigest())
        item['finish_reasons'] = [c['finish_reason'] for m in messages
                                  for c in m.get('choices', [])
                                  if c.get('finish_reason') is not None]
        item['decode_tps'] = ((row['completion_tokens'] - 1) /
                             (row['latency'] - row['first_chunk_latency'])
                             if row['completion_tokens'] > 1 else None)
        parsed.append(item)
    save(case / 'parsed.json', parsed)
    assert len(parsed) == expected_count
    assert all(x['success'] and x['completion_tokens'] > 0 for x in parsed)
    if expect_empty_stop:
        assert all(not x['text'] and x['completion_tokens'] == 1
                   and x['finish_reasons'] == ['stop'] for x in parsed)
    else:
        assert all(x['text'] and x['completion_tokens'] > 1 for x in parsed)
    return parsed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['accepted-root', 'parent-run', 'output', 'model-dir']:
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--scenario', choices=['inline', 'fallback', 'vision', 'mtp'], required=True)
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--lengths', type=int, nargs='+',
                        help='Affected text lengths for a separate HTTP confirmation')
    args = parser.parse_args()
    run, parent, out = [p.resolve() for p in [args.accepted_root, args.parent_run, args.output]]
    assert any(out.is_relative_to(ROOT / p) for p in ['build', '.q4t-work'])
    gate = json.loads((run / 'acceptance.json').read_text())
    assert gate['quality_passed'] and gate['performance_accepted']
    binaries = {'parent': run / 'q4t-before', 'candidate': ROOT / 'build/q4t'}
    for version, baseline in [('parent', parent), ('candidate', run)]:
        for mode, count in [('quality', 11), ('performance', 5)]:
            terminal = json.loads((baseline / mode / 'exit.json').read_text())
            assert terminal['completed'] == count and terminal['server'] == 0
            assert terminal['failure'] is None and terminal['http_output_checks_passed']
            assert sha(binaries[version]) == (baseline / mode / 'binary.sha256').read_text().strip()
    for snapshot in (run / 'source').rglob('*'):
        if snapshot.is_file():
            assert snapshot.read_bytes() == (ROOT / snapshot.relative_to(run / 'source')).read_bytes()
    out.mkdir(parents=True, exist_ok=False)
    shutil.copy2(__file__, out / Path(__file__).name)
    shutil.copy2(ROOT / 'tools/evalscope/run_chunk_interleave.py', out)
    payloads = {}
    expected_lengths = {}
    if args.scenario == 'vision':
        assert args.lengths is None, 'Vision coverage cannot be filtered by text length'
        urls = {}
        for name in ['a', 'b']:
            image = ROOT / f'tools/vision_test_{name}.png'
            shutil.copy2(image, out / image.name)
            urls[name] = 'data:image/png;base64,' + base64.b64encode(image.read_bytes()).decode()
        image_part = lambda name: {'type': 'image_url', 'image_url': {'url': urls[name]}}
        parts = {'image': [image_part('a')],
                 'two-images': [image_part('a'), image_part('b')],
                 'video': [{'type': 'video', 'video_frames': [urls['a'], urls['a']]}]}
        # Keep the original immediate-stop fixture as a behavioral check.
        # It cannot supply decode performance or replace generation coverage.
        question = 'Describe the visual content briefly.'
        payloads['image-raw-stop'] = {'messages': [{'role': 'user', 'content':
            [{'type': 'text', 'text': question}] + parts['image']}]}
        # Serve currently concatenates role + ': ', rather than applying the
        # checkpoint template. An empty role lets these explicit template
        # fragments pass through while media parts still use the real pipeline.
        for name, media in parts.items():
            content = [{'type': 'text', 'text': '<|im_start|>user\n' + question}]
            for part in media:
                content += [{'type': 'text', 'text': '<|vision_start|>'}, part,
                            {'type': 'text', 'text': '<|vision_end|>'}]
            content.append({'type': 'text', 'text':
                '<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n'})
            payloads[name] = {'messages': [{'role': '', 'content': content}]}
        save(out / 'vision-fixture-contract.json', {
            'template_sha256': sha(args.model_dir / 'tokenizer_config.json'),
            'enable_thinking': False, 'add_generation_prompt': True,
            'raw_stop_is_performance_coverage': False,
            'limits': 'Explicit template through empty-role content parts; does not validate standard messages chat templating.'})
    else:
        lengths = {'inline': [1024, 8192], 'fallback': [1024, 45056],
                   'mtp': [1024, 8192, 45056]}[args.scenario]
        if args.lengths is not None:
            assert args.lengths and len(set(args.lengths)) == len(args.lengths)
            assert set(args.lengths).issubset(lengths)
            lengths = args.lengths
        for length in lengths:
            name = str(length)
            payloads[name] = json.loads((run / f'performance/context-{length}/requests.jsonl')
                                       .read_text().splitlines()[0])
            expected_lengths[name] = length
    for name, payload in payloads.items():
        payload.update(model='qwen3.8-flash-next', temperature=0, max_tokens=32, stream=True)
        (out / (name + '.jsonl')).write_text(json.dumps(payload, ensure_ascii=False) + '\n')
    library = out / 'scheduler_alloc_fault.so'
    if args.scenario == 'fallback':
        source = ROOT / 'tools/verify/scheduler_alloc_fault.cpp.in'
        shutil.copy2(source, out)
        command = ['g++-14', '-std=c++23', '-Wall', '-Wextra', '-shared', '-fPIC',
                   '-I/usr/local/cuda/include', '-x', 'c++', str(source), '-ldl', '-o', str(library)]
        save(out / 'build-command.json', command)
        with (out / 'build.log').open('w') as log:
            subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
        assert 'warning' not in (out / 'build.log').read_text().lower()
    expected, all_results = {}, []
    for group in ['parent-before', 'candidate', 'parent-after']:
        version = 'candidate' if group == 'candidate' else 'parent'
        directory = out / group
        directory.mkdir()
        env = {k: v for k, v in os.environ.items() if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}
        if args.scenario == 'inline':
            env['Q4T_NO_BATCH_PREFILL'] = '1'
        elif args.scenario == 'fallback':
            env['LD_PRELOAD'] = str(library)
        elif args.scenario == 'mtp':
            env['Q4T_SCHED_DEBUG'] = '1'
        command = [str(binaries[version]), 'serve', '--model-dir', str(args.model_dir),
                   '--port', str(args.port), '--max-seq', '1', '--max-len', '65536',
                   '--max-prefill', '8192', '--max-tokens', '32',
                   '--mtp' if args.scenario == 'mtp' else '--no-mtp']
        save(directory / 'server-command.json', command)
        save(directory / 'environment.json', {k:v for k,v in env.items() if k.startswith('Q4T_') or k == 'LD_PRELOAD'})
        (directory / 'binary.sha256').write_text(sha(binaries[version]) + '\n')
        server_log = directory / 'server.log'
        with server_log.open('w') as log:
            server = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 240
            while 'serving on port' not in server_log.read_text():
                assert server.poll() is None and time.monotonic() < deadline
                time.sleep(1)
            startup = server_log.read_text()
            assert '=> max_len=65536 max_seq=1' in startup
            if args.scenario == 'fallback':
                assert startup.count('[scheduler-alloc-fault]') == 1
                assert 'scheduler logits alloc failed; plain decode' in startup
                assert 'scheduler started' not in startup
            elif args.scenario == 'mtp':
                assert 'MTP loaded' in startup
            else:
                assert 'MTP disabled; plain decode' in startup
            for name, payload in payloads.items():
                case = directory / name
                case.mkdir()
                start = len(server_log.read_text())
                client = [str(ROOT / 'tools/evalscope/.venv/bin/evalscope'), 'perf',
                          '--model', 'qwen3.8-flash-next', '--url',
                          f'http://127.0.0.1:{args.port}/v1/chat/completions', '--api', 'openai',
                          '--tokenizer-path', str(args.model_dir), '--dataset', 'line_by_line',
                          '--dataset-path', str(out / (name + '.jsonl')), '--no-apply-chat-template',
                          '--max-tokens', '32', '--temperature', '0', '--seed', '20260920',
                          '--parallel', '1', '--number', '3', '--warmup-num', '0', '--no-test-connection',
                          '--connect-timeout', '30', '--read-timeout', '1800', '--total-timeout', '3600',
                          '--stream', '--outputs-dir', str(case)]
                save(case / 'command.json', client)
                with (case / 'client.log').open('w') as log:
                    subprocess.run(client, stdout=log, stderr=subprocess.STDOUT, check=True)
                rows = parse(case, payload, name == 'image-raw-stop')
                if name in expected_lengths:
                    assert all(x['prompt_tokens'] == expected_lengths[name] for x in rows)
                signatures = [(x['prompt_tokens'], x['completion_tokens'], x['output_sha256']) for x in rows]
                assert len(set(signatures)) == 1, 'non-deterministic output'
                expected.setdefault(name, signatures[0])
                assert all(x == expected[name] for x in signatures), (group, name, signatures)
                log_chunk = server_log.read_text()[start:]
                if args.scenario == 'mtp':
                    assert log_chunk.count('[q4t][sched] MTP step B=1') >= 3
                    assert 'plain decode' not in log_chunk, 'MTP fallback is not coverage'
                all_results.append({'group': group, 'case': name, 'metrics': rows})
                save(out / 'results.json', all_results)
                print(group, name, 'HTTP output/coverage passed', flush=True)
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
    for name in payloads:
        new = next(x['metrics'] for x in all_results if x['group'] == 'candidate' and x['case'] == name)
        for group in ['parent-before', 'parent-after']:
            old = next(x['metrics'] for x in all_results if x['group'] == group and x['case'] == name)
            for metric in ['first_chunk_latency', 'decode_tps']:
                if name == 'image-raw-stop':
                    comparisons.append({'case':name,'reference':group,'metric':metric,
                                        'unavailable':True,'reason':'immediate stop, no content token'})
                    continue
                a,b = [x[metric] for x in old], [x[metric] for x in new]
                if any(x is None for x in a+b):
                    comparisons.append({'case':name,'reference':group,'metric':metric,'unavailable':True})
                    continue
                bad = min(b) > max(a) if metric == 'first_chunk_latency' else max(b) < min(a)
                comparisons.append({'case':name,'reference':group,'metric':metric,
                                    'old_range':[min(a),max(a)],'new_range':[min(b),max(b)],'adverse':bad})
                if bad:
                    adverse.append([name,group,metric])
    save(out / 'review.json', {'accepted':not adverse,'http_outputs_passed':True,
                              'comparisons':comparisons,'adverse':adverse,
                              'limits':'Fixed requests, three repeats. Actual output lengths retained; no universal multimodal/MTP guarantee. Synthetic scheduler allocation failure is not real OOM.'})
    assert not adverse, 'Performance ranges require review'


if __name__ == '__main__':
    main()
