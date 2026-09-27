"""Audit raw local HTTP databases and seal only after all writers have exited."""
from pathlib import Path
import base64
import hashlib
import json
import pickle
import shutil
import sqlite3

ROOT = Path(__file__).resolve().parents[3]
P = ROOT / '.q4t-work/prepared/quality-review-v2-20260927'
OUT = ROOT / '.q4t-work/e2e/quality-review-v2-20260927'


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')


def main():
    assert json.loads((OUT / 'collection.json').read_text())['complete']
    analysis = json.loads((OUT / 'analysis.json').read_text())
    if analysis['bounded_repeat_ids']:
        assert (OUT / 'repeat.json').exists()
    plan = json.loads((P / 'plan.json').read_text())
    assert sha(ROOT / 'build/q4t') == plan['baseline_sha256']
    for name, expected in plan['bindings'].items():
        assert sha(ROOT / name) == expected, name
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            argv = (proc / 'cmdline').read_bytes().split(b'\0')
            exe = (proc / 'exe').resolve(strict=True)
        except OSError:
            continue
        assert not (exe.name.startswith('q4t') and b'serve' in argv)
    audits = []
    stages = ['baseline-first', 'fixed', 'baseline-last']
    if (OUT / 'repeat.json').exists():
        stages += ['repeat-baseline', 'repeat-fixed']
    for stage in stages:
        d = OUT / stage
        terminal = json.loads((d / 'exit.json').read_text())
        assert terminal['server'] == 0
        expected = {}
        for line in (d / 'inputs/requests.jsonl').read_text().splitlines():
            req = json.loads(line)
            expected[hashlib.sha256(req['prompt'].encode()).hexdigest()] = req
        summaries = {r['prompt_sha256']: r for r in json.loads((d / 'results.json').read_text())}
        db = next((d / 'quality').rglob('benchmark_data.db'))
        with sqlite3.connect('file:' + str(db) + '?mode=ro', uri=True) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute('select * from result order by start_time').fetchall()
        assert len(rows) == len(expected) == len(summaries) == terminal['completed']
        seen = set()
        for row in rows:
            req = json.loads(row['request'])
            h = hashlib.sha256(req['prompt'].encode()).hexdigest()
            assert h not in seen
            seen.add(h)
            assert all(req[k] == v for k, v in expected[h].items())
            # These are this task's own local evalscope databases.
            messages = pickle.loads(base64.b64decode(row['response_messages']))
            choices = [c for message in messages for c in message.get('choices', [])]
            text = ''.join(c.get('delta', c.get('message', {})).get('content', '') for c in choices)
            finish = [c['finish_reason'] for c in choices if c.get('finish_reason')]
            s = summaries[h]
            assert text == s['text'] and finish == s['finish']
            assert row['success'] == s['success']
            assert row['prompt_tokens'] == s['actual_input'] == s['length']
            assert row['completion_tokens'] == s['actual_output']
            assert s['exact_match'] == (text.strip() == s['expected'])
        audits.append({'stage': stage, 'raw_records_verified': len(rows),
                       'server_exit': terminal['server'],
                       'driver_exit': json.loads((OUT / (stage + '-driver-exit.json')).read_text())['exit'],
                       'binary_sha256': (d / 'binary.sha256').read_text().strip()})
    save(OUT / 'raw-http-audit.json', audits)
    logs = ['quality-review-v2-run-20260927.log', 'quality-review-v2-analysis-20260927.log']
    if (OUT / 'repeat.json').exists():
        logs.append('quality-review-v2-repeat-20260927.log')
    for name in logs:
        shutil.copy2(P.parent / name, OUT / name)
    shutil.copytree(Path(__file__).parent, OUT / 'tools-final')
    save(OUT / 'source-binding.json', {str(f.relative_to(ROOT)): sha(f) for f in sorted(P.rglob('*')) if f.is_file()})
    final = 'eligible_for_performance_screening' if analysis['eligible_for_performance_screening'] else 'insufficient_or_structural_failure'
    if (OUT / 'repeat.json').exists():
        final = json.loads((OUT / 'repeat.json').read_text())['decision']
    save(OUT / 'summary.json', {'decision': final, 'runtime_accepted': False,
         'production_binary_changed': False, 'performance_run': False,
         'new': analysis['new'], 'legacy': analysis['legacy'],
         'http_records_verified': sum(r['raw_records_verified'] for r in audits),
         'scope': 'Finite frozen synthetic engineering-record screening, not general model quality.'})
    binding = {str(f.relative_to(OUT)): sha(f) for f in sorted(OUT.rglob('*')) if f.is_file()}
    assert not (OUT / 'artifact-binding.json').exists()
    save(OUT / 'artifact-binding.json', binding)
    for name, expected in binding.items():
        assert sha(OUT / name) == expected
    print({'artifact_entries_verified': len(binding), 'all_raw_http_records_verified': sum(r['raw_records_verified'] for r in audits), 'decision': final})


if __name__ == '__main__':
    main()
