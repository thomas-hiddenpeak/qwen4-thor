"""Seal this cancellation experiment only after all writer processes exit."""
import base64
import hashlib
import json
from pathlib import Path
import pickle
import shutil
import sqlite3

ROOT = Path(__file__).resolve().parents[3]
CONTROL = ROOT / '.q4t-work/prefill-cancel-control-20260927'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    binary = CONTROL / 'candidate-q4t'
    digest = sha(binary)
    assert digest == json.loads((CONTROL / 'plan.json').read_text())['binary_sha256']
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            argv = (proc / 'cmdline').read_bytes().split(b'\0')
            exe = (proc / 'exe').resolve(strict=True)
        except OSError:
            continue
        assert not (b'serve' in argv and exe in [binary, ROOT / '.q4t-work/sequence-slot-build-20260927/q4t'])
    directories = [CONTROL, ROOT / '.q4t-work/cancel-connection-20260927']
    assert json.loads((directories[-1] / 'exit.json').read_text())['returncode'] == 0
    lifecycle = ROOT / '.q4t-work/e2e/prefill-cancel-lifecycle-20260927'
    s = json.loads((lifecycle / 'summary.json').read_text())
    assert s['failure'] is None and s['server_exit'] == 0
    assert all(x.get('passed', x.get('A_equal') and x.get('B_equal')) for x in s['records'])
    assert json.loads((lifecycle / 'identity.json').read_text())['binary_sha256'] == digest
    assert json.loads((lifecycle / 'early-cancel.json').read_text()) == {'position': 8192, 'total': 45056}
    for name in ['cancel-prefill', 'cancel-decode']:
        def counter(side, key):
            lines = (lifecycle / f'{name}-metrics-{side}.txt').read_text().splitlines()
            return int(next(x.split()[1] for x in lines if x.startswith(key + ' ')))
        assert counter('after', 'q4t_requests_error_total') == counter('before', 'q4t_requests_error_total')
        assert counter('after', 'q4t_requests_aborted_total') == counter('before', 'q4t_requests_aborted_total') + 1
    files = list(lifecycle.glob('*-result.json'))
    assert len(files) == 9
    for f in files:
        raw = (lifecycle / (f.name.removesuffix('-result.json') + '.sse')).read_text()
        assert raw.strip().endswith('data: [DONE]')
        events = [json.loads(line[6:]) for line in raw.splitlines() if line.startswith('data: {')]
        choices = [c for e in events for c in e.get('choices', [])]
        r = json.loads(f.read_text())
        assert ''.join(c.get('delta', {}).get('content', '') for c in choices) == r['text']
        assert [c['finish_reason'] for c in choices if c.get('finish_reason')] == r['finish']
        assert next(e['usage'] for e in events if e.get('usage')) == r['usage']
    directories.append(lifecycle)
    total = 0
    for mode, cases, rows_expected in [('quality', 11, 11), ('performance', 5, 15)]:
        d = ROOT / f'.q4t-work/e2e/prefill-cancel-{mode}-20260927'
        e = json.loads((d / 'exit.json').read_text())
        assert e['server'] == 0 and e['failure'] is None and e['completed'] == cases
        assert e['http_output_checks_passed'] and (d / 'binary.sha256').read_text().strip() == digest
        results = json.loads((d / 'results.json').read_text())
        seen = []
        for dbfile in sorted(d.rglob('benchmark_data.db')):
            with sqlite3.connect('file:' + str(dbfile) + '?mode=ro', uri=True) as db:
                db.row_factory = sqlite3.Row
                rows = db.execute('select * from result order by start_time').fetchall()
            for row in rows:
                request = json.loads(row['request'])
                prompt_hash = hashlib.sha256(request['prompt'].encode()).hexdigest()
                events = pickle.loads(base64.b64decode(row['response_messages']))
                choices = [c for event in events for c in event.get('choices', [])]
                text = ''.join(c.get('delta', c.get('message', {})).get('content', '') for c in choices)
                finish = [c['finish_reason'] for c in choices if c.get('finish_reason')]
                assert row['success'] and request['stream']
                record = next(x for x in results if x['prompt_sha256'] == prompt_hash)
                if mode == 'quality':
                    assert text == record['text'] and finish == ['stop']
                    assert row['prompt_tokens'] == record['actual_input']
                    assert row['completion_tokens'] == record['actual_output']
                else:
                    assert hashlib.sha256(text.encode()).hexdigest() in record['outputs']
                    assert row['prompt_tokens'] == record['length']
                    assert row['completion_tokens'] == 256 and finish == ['length']
                seen.append(prompt_hash)
        assert len(seen) == rows_expected
        total += len(seen)
        directories.append(d)
    for mode in ['build', 'lifecycle', 'quality', 'performance']:
        shutil.copy2(ROOT / f'.q4t-work/prepared/prefill-cancel-{mode}-20260927.log', CONTROL / (mode + '-driver.log'))
    shutil.copytree(Path(__file__).parent, CONTROL / 'tools-final')
    (CONTROL / 'summary.json').write_text(json.dumps({'binary_sha256': digest, 'audited_evalscope_rows': total,
        'audited_normal_lifecycle_responses': 9, 'early_cancel_position': 8192,
        'full_prompt_tokens': 45056, 'performance_measured': True,
        'strict_non_regression_proven': False}, indent=2))
    binding = {str(f.relative_to(ROOT)): sha(f) for d in directories for f in sorted(d.rglob('*')) if f.is_file()}
    target = CONTROL / 'artifact-binding.json'
    assert not target.exists()
    target.write_text(json.dumps(binding, indent=2))
    for name, value in binding.items():
        assert sha(ROOT / name) == value
    print({'sealed_files': len(binding), 'raw_evalscope_rows': total, 'lifecycle_responses': 9})


if __name__ == '__main__':
    main()
