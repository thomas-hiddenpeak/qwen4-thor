"""Audit terminal HTTP records and seal the bounded delivery, after all writers."""
from pathlib import Path
import base64
import hashlib
import json
import pickle
import shutil
import sqlite3

ROOT = Path(__file__).resolve().parents[3]
P = ROOT / '.q4t-work/prepared/e4m3-site-isolation-20260927'
E = ROOT / '.q4t-work/e2e'
OUT = E / 'e4m3-site-delivery-20260927'


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def main():
    OUT.mkdir()
    plan = json.loads((P / 'plan.json').read_text())
    assert sha(ROOT / 'build/q4t') == plan['baseline_sha256']
    for name, expected in plan['sources'].items():
        assert sha(ROOT / name) == expected
    fixtures = ROOT / '.q4t-work/prepared/e4m3-rounding-20260923/recovery-inputs'
    want = [json.loads(line) for line in (fixtures / 'requests.jsonl').read_text().splitlines()]
    manifest = json.loads((fixtures / 'manifest.json').read_text())
    runs = ['e4m3-input-only-http-20260927', 'e4m3-inter-only-http-20260927',
            'e4m3-site-recovery-20260927']
    audits = []
    for name in runs:
        d = E / name
        terminal = json.loads((d / 'exit.json').read_text())
        assert terminal['server'] == 0 and terminal['completed'] == 3
        db = next((d / 'quality').rglob('benchmark_data.db'))
        with sqlite3.connect('file:' + str(db) + '?mode=ro', uri=True) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute('select * from result order by start_time').fetchall()
        assert len(rows) == 3
        results = json.loads((d / 'results.json').read_text())
        scores = []
        for row, requested, meta, result in zip(rows, want, manifest, results):
            req = json.loads(row['request'])
            assert all(req[k] == v for k, v in requested.items())
            assert hashlib.sha256(req['prompt'].encode()).hexdigest() == meta['prompt_sha256']
            assert req['temperature'] == 0 and req['max_tokens'] == 32 and req['stream']
            assert row['success'] and row['prompt_tokens'] == meta['length']
            # Only deserialize this task's own evalscope databases.
            messages = pickle.loads(base64.b64decode(row['response_messages']))
            choices = [c for m in messages for c in m.get('choices', [])]
            text = ''.join(c.get('delta', c.get('message', {})).get('content', '') for c in choices)
            stop = [c['finish_reason'] for c in choices if c.get('finish_reason')]
            assert text == result['text'] and stop == result['finish'] == ['stop']
            assert row['completion_tokens'] == result['actual_output'] == 7
            scores.append({'id': meta['id'], 'expected': meta['expected'],
                           'actual': text, 'correct': text.strip() == meta['expected'],
                           'prompt_sha256': meta['prompt_sha256'],
                           'input_tokens': row['prompt_tokens'], 'output_tokens': 7,
                           'finish': stop})
        audits.append({'run': name, 'binary_sha256': (d / 'binary.sha256').read_text().strip(),
                       'scores': scores, 'terminal': terminal})
    save(OUT / 'raw-http-audit.json', audits)
    assert json.loads((E / runs[-1] / 'recovery-audit.json').read_text())['all_three_match_baseline']
    roots = [P, *(E / n for n in runs),
             E / 'e4m3-contract-regression-20260927', E / 'e4m3-repro-package-20260927']
    bindings = {}
    for d in roots:
        for f in sorted(d.rglob('*')):
            if f.is_file():
                bindings[str(f.relative_to(ROOT))] = sha(f)
    save(OUT / 'source-binding.json', bindings)
    for name, expected in bindings.items():
        assert sha(ROOT / name) == expected
    shutil.copytree(Path(__file__).parent, OUT / 'tools')
    save(OUT / 'summary.json', {
        'runtime_changed': False, 'accepted_binary_sha256': plan['baseline_sha256'],
        'runtime_candidates': 2, 'new_runtime_candidates_accepted': 0,
        'direct_contract': {'baseline_mismatches': 147, 'high_only_mismatches': 27,
                            'full_fix_mismatches': 0, 'records_each': 33146},
        'full_fix': 'Previously archived implementation; defect regression passed, historical HTTP quality regression remains.',
        'http_requests': 9, 'recovery_matches_original': True,
        'whole_model_correctness_proven': False, 'performance_run': False,
        'source_entries_verified': len(bindings)})
    binding = {str(f.relative_to(OUT)): sha(f) for f in sorted(OUT.rglob('*')) if f.is_file()}
    save(OUT / 'artifact-binding.json', binding)
    for name, expected in binding.items():
        assert sha(OUT / name) == expected
    print({'source_entries_verified': len(bindings),
           'artifact_entries_verified': len(binding), 'all_nine_http_records_verified': True})


if __name__ == '__main__':
    main()
