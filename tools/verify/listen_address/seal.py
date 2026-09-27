"""Audit this listen-address delivery only after all writers terminate."""
import base64
import hashlib
import json
from pathlib import Path
import pickle
import shutil
import sqlite3

ROOT = Path(__file__).resolve().parents[3]
CONTROL = ROOT / '.q4t-work/listen-address-control-20260927'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    assert not (CONTROL / 'artifact-binding.json').exists()
    binary = CONTROL / 'candidate-q4t'
    digest = sha(binary)
    assert digest == json.loads((CONTROL / 'plan.json').read_text())['binary_sha256']
    exits = json.loads((ROOT / '.q4t-work/prepared/listen-driver-exits-20260927.json').read_text())
    assert len(exits) == 4 and all(e['returncode'] == 0 for e in exits)
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            args = (proc / 'cmdline').read_bytes().split(b'\0')
            exe = (proc / 'exe').resolve(strict=True)
        except OSError:
            continue
        assert not (exe == binary and b'serve' in args)
        assert not any(Path(a.decode(errors='replace')).name == 'run-listen-address-20260927.py' for a in args)
    for f in (CONTROL / 'source').rglob('*'):
        if f.is_file():
            assert sha(f) == sha(ROOT / f.relative_to(CONTROL / 'source'))
    direct = ROOT / '.q4t-work/listen-address-tests-20260927'
    assert json.loads((direct / 'plan.json').read_text())['binary_sha256'] == digest
    assert len(json.loads((direct / 'results.json').read_text())) == 10
    for name in ['default', 'wildcard', 'interface']:
        assert json.loads((direct / (name + '-exit.json')).read_text())['server'] == 0
    proxy = ROOT / '.q4t-work/e2e/listen-proxy-20260927'
    result = json.loads((proxy / 'exit.json').read_text())
    assert result['failure'] is None and result['server'] == result['proxy'] == 0
    assert len(result['records']) == 7
    assert json.loads((proxy / 'plan.json').read_text())['binary_sha256'] == digest
    for label, expected, tokens in [('context-200k', '711273', 204800)] + [(f'recovery-{i}', '710003', 1024) for i in range(3)]:
        response = json.loads((proxy / (label + '-response.json')).read_text())
        assert response['status'] == 200 and response['body'].strip().endswith('data: [DONE]')
        events = [json.loads(x[6:]) for x in response['body'].splitlines() if x.startswith('data: {')]
        choices = [c for e in events for c in e.get('choices', [])]
        assert ''.join(c.get('delta', {}).get('content', '') for c in choices) == expected
        assert [c['finish_reason'] for c in choices if c.get('finish_reason')] == ['stop']
        usage = next(e['usage'] for e in events if e.get('usage'))
        assert usage['prompt_tokens'] == tokens and usage['completion_tokens'] == 7
    directories = [CONTROL, direct, proxy]
    total = 0
    for mode, cases, rows_expected in [('quality', 11, 11), ('performance', 5, 15)]:
        d = ROOT / f'.q4t-work/e2e/listen-{mode}-20260927'
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
    for e in exits:
        shutil.copy2(ROOT / ('.q4t-work/prepared/' + e['name'] + '-20260927.log'), CONTROL / (e['name'] + '-driver.log'))
    shutil.copy2(ROOT / '.q4t-work/prepared/listen-driver-exits-20260927.json', CONTROL / 'driver-exits.json')
    shutil.copytree(Path(__file__).parent, CONTROL / 'tools-final')
    for name in ['performance_reference.json', 'performance_reference.metadata.json']:
        shutil.copy2(ROOT / 'tools/evalscope/fixtures' / name, CONTROL / ('previous-' + name))
    (CONTROL / 'summary.json').write_text(json.dumps({'binary_sha256': digest, 'audited_evalscope_rows': total, 'proxy_normal_outputs': 4, 'listen_modes': 3, 'invalid_config_cases': 7, 'strict_non_regression_proven': False}, indent=2))
    binding = {str(f.relative_to(ROOT)): sha(f) for d in directories for f in sorted(d.rglob('*')) if f.is_file()}
    (CONTROL / 'artifact-binding.json').write_text(json.dumps(binding, indent=2))
    assert all(sha(ROOT / f) == h for f, h in binding.items())
    print(json.dumps({'files': len(binding), 'raw_evalscope_rows': total, 'proxy_outputs': 4}))


if __name__ == '__main__':
    main()
