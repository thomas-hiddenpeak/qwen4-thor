"""HTTP gate for a vision determinism fix; retain unstable parent outputs."""
import argparse
import base64
import json
import os
from pathlib import Path
import pickle
import shutil
import sqlite3
import subprocess
import time

from run_chunk_interleave import ROOT, save, sha


def read_rows(case, payload):
    with sqlite3.connect(next(case.rglob('benchmark_data.db'))) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute('select * from result order by start_time').fetchall()
    result = []
    for row in rows:
        request = json.loads(row['request'])
        assert request['messages'] == payload['messages']
        assert request['temperature'] == 0 and request['max_tokens'] == 32
        messages = pickle.loads(base64.b64decode(row['response_messages']))
        text = ''.join(c.get('delta', c.get('message', {})).get('content', '')
                       for m in messages for c in m.get('choices', []))
        item = {k: row[k] for k in ['success', 'prompt_tokens', 'completion_tokens',
                                   'latency', 'first_chunk_latency']}
        item['text'] = text
        item['finish_reasons'] = [c['finish_reason'] for m in messages
                                 for c in m.get('choices', []) if c.get('finish_reason')]
        item['decode_tps'] = ((item['completion_tokens'] - 1) /
                             (item['latency'] - item['first_chunk_latency'])
                             if item['completion_tokens'] > 1 and text else None)
        result.append(item)
    save(case / 'parsed.json', result)
    assert len(result) == 3 and all(r['success'] for r in result)
    return result


def signature(row):
    return (row['prompt_tokens'], row['completion_tokens'], row['text'],
            tuple(row['finish_reasons']))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['accepted-root', 'parent-run', 'fixtures', 'output', 'model-dir']:
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    run, out = args.accepted_root.resolve(), args.output.resolve()
    assert any(out.is_relative_to(ROOT / d) for d in ['build', '.q4t-work'])
    gate = json.loads((run / 'acceptance.json').read_text())
    assert gate['quality_passed'] and gate['performance_accepted']
    binaries = {'parent': run / 'q4t-before', 'candidate': ROOT / 'build/q4t'}
    for version, baseline in [('parent', args.parent_run), ('candidate', run)]:
        for mode, count in [('quality', 11), ('performance', 5)]:
            terminal = json.loads((baseline / mode / 'exit.json').read_text())
            assert terminal['completed'] == count and terminal['server'] == 0
            assert terminal['failure'] is None and terminal['http_output_checks_passed']
            assert sha(binaries[version]) == (baseline / mode / 'binary.sha256').read_text().strip()
    for snapshot in (run / 'source').rglob('*'):
        if snapshot.is_file():
            assert snapshot.read_bytes() == (ROOT / snapshot.relative_to(run / 'source')).read_bytes()
    out.mkdir(parents=True, exist_ok=False)
    shutil.copy2(__file__, out)
    names = ['image-raw-stop', 'image', 'two-images', 'video']
    for name in names:
        shutil.copy2(args.fixtures / (name + '.jsonl'), out)
    shutil.copy2(args.fixtures / 'vision-fixture-contract.json', out)
    records, expected = [], {}
    groups = ['parent-before', 'candidate', 'candidate-restart', 'parent-after']
    for group in groups:
        candidate = group.startswith('candidate')
        binary = binaries['candidate' if candidate else 'parent']
        directory = out / group
        directory.mkdir()
        command = [str(binary), 'serve', '--model-dir', str(args.model_dir),
                   '--port', '8000', '--max-seq', '1', '--max-len', '65536',
                   '--max-prefill', '8192', '--max-tokens', '32', '--no-mtp']
        save(directory / 'server-command.json', command)
        (directory / 'binary.sha256').write_text(sha(binary) + '\n')
        env = {k:v for k,v in os.environ.items() if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}
        log_path = directory / 'server.log'
        with log_path.open('w') as log:
            server = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 240
            while 'serving on port' not in log_path.read_text():
                assert server.poll() is None and time.monotonic() < deadline
                time.sleep(1)
            startup = log_path.read_text()
            assert '=> max_len=65536 max_seq=1' in startup
            assert 'MTP disabled; plain decode' in startup and 'vision tower loaded' in startup
            for name in names:
                case = directory / name
                case.mkdir()
                fixture = out / (name + '.jsonl')
                payload = json.loads(fixture.read_text())
                command = [str(ROOT / 'tools/evalscope/.venv/bin/evalscope'), 'perf',
                    '--model', 'qwen3.8-flash-next', '--url', 'http://127.0.0.1:8000/v1/chat/completions',
                    '--api', 'openai', '--tokenizer-path', str(args.model_dir),
                    '--dataset', 'line_by_line', '--dataset-path', str(fixture),
                    '--no-apply-chat-template', '--max-tokens', '32', '--temperature', '0',
                    '--seed', '20260920', '--parallel', '1', '--number', '3',
                    '--warmup-num', '0', '--no-test-connection', '--connect-timeout', '30',
                    '--read-timeout', '1800', '--total-timeout', '3600', '--stream',
                    '--outputs-dir', str(case)]
                save(case / 'command.json', command)
                with (case / 'client.log').open('w') as log:
                    subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
                rows = read_rows(case, payload)
                if name != 'image-raw-stop':
                    assert all(r['text'] and r['completion_tokens'] > 1 for r in rows)
                stable = len({signature(r) for r in rows}) == 1
                records.append({'group':group,'case':name,'deterministic':stable,'metrics':rows})
                save(out / 'results.json', records)
                if candidate:
                    assert stable, (group, name, 'candidate nondeterministic')
                    expected.setdefault(name, signature(rows[0]))
                    assert expected[name] == signature(rows[0]), 'candidate restart changed output'
                print(group, name, 'deterministic=' + str(stable), flush=True)
        finally:
            server.terminate()
            try:
                server.wait(timeout=30)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
            save(directory / 'exit.json', {'server':server.returncode})
        assert server.returncode == 0
    comparisons, adverse = [], []
    for name in names[1:]:
        for candidate in ['candidate', 'candidate-restart']:
            new = next(r['metrics'] for r in records if r['group'] == candidate and r['case'] == name)
            for parent in ['parent-before', 'parent-after']:
                old = next(r['metrics'] for r in records if r['group'] == parent and r['case'] == name)
                for metric in ['first_chunk_latency', 'decode_tps']:
                    a,b = [r[metric] for r in old], [r[metric] for r in new]
                    bad = min(b) > max(a) if metric == 'first_chunk_latency' else max(b) < min(a)
                    comparisons.append({'case':name,'candidate':candidate,'parent':parent,
                        'metric':metric,'old_range':[min(a),max(a)],'new_range':[min(b),max(b)],'adverse':bad})
                    if bad: adverse.append([name,candidate,parent,metric])
    save(out / 'review.json', {'http_determinism_passed':True,'performance_accepted':not adverse,
        'production_accepted':False,'comparisons':comparisons,'adverse':adverse,
        'limits':'Fixed image/video fixtures; unstable parent text retained, not an accuracy reference. Raw stop fixture excluded from performance. Correctness needs independent numerical reference after HTTP gates.'})
    assert not adverse, 'performance requires review'


if __name__ == '__main__':
    main()
