"""Audit and seal the bounded request-control delivery after writers exit."""
import base64
import hashlib
import json
from pathlib import Path
import pickle
import shutil
import sqlite3

ROOT = Path(__file__).resolve().parents[3]
CONTROL = ROOT / '.q4t-work/request-control-accepted-candidate-20260927'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    assert not (CONTROL / 'artifact-binding.json').exists()
    binary = CONTROL / 'candidate-q4t'
    digest = sha(binary)
    assert digest == json.loads((CONTROL / 'plan.json').read_text())['binary_sha256']
    exits = json.loads((ROOT / '.q4t-work/prepared/request-control-driver-exits-20260927.json').read_text())
    assert len(exits) == 5 and all(e['returncode'] == 0 for e in exits)
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            argv = (proc / 'cmdline').read_bytes().split(b'\0')
            exe = (proc / 'exe').resolve(strict=True)
        except OSError:
            continue
        assert not (b'serve' in argv and exe == binary)
        assert not any(b'run-request-control-delivery.py' == Path(a.decode(errors='replace')).name.encode() for a in argv)
    for source in (CONTROL / 'source').rglob('*'):
        if source.is_file():
            assert sha(source) == sha(ROOT / source.relative_to(CONTROL / 'source'))
    directories = [CONTROL]
    for name in ['request-control-unit-v2', 'request-eof-unit']:
        d = ROOT / ('.q4t-work/' + name + '-20260927')
        assert json.loads((d / 'exit.json').read_text())['returncode'] == 0
        directories.append(d)
    lifecycle = ROOT / '.q4t-work/e2e/request-cancellation-v5-20260927'
    s = json.loads((lifecycle / 'summary.json').read_text())
    assert s['failure'] is None and s['server_exit'] == 0 and len(s['records']) == 8
    assert json.loads((lifecycle / 'plan.json').read_text())['binary_sha256'] == digest
    for name in ['active-a', 'nonstream-cancel', 'queued', 'queued-deadline', 'reused-id']:
        assert json.loads((lifecycle / (name + '-response.json')).read_text())['status'] == 409
    for name in ['decode-cancel', 'decode-deadline', 'canary', 'survivor']:
        response = json.loads((lifecycle / (name + '-response.json')).read_text())
        assert response['status'] == 200
        raw = response['body']
        assert raw.strip().endswith('data: [DONE]')
        events = [json.loads(line[6:]) for line in raw.splitlines() if line.startswith('data: {')]
        choices = [c for e in events for c in e.get('choices', [])]
        finishes = [c['finish_reason'] for c in choices if c.get('finish_reason')]
        if name.startswith('decode-'):
            assert any(e.get('error') for e in events) and not finishes
        else:
            expected = '710003' if name == 'canary' else '711146'
            assert ''.join(c.get('delta', {}).get('content', '') for c in choices) == expected
            assert finishes == ['stop']
    log = (lifecycle / 'server.log').read_text()
    assert log.count('position=8192 total=45056') == 4
    assert 'shutdown complete (0 in-flight remaining)' in log
    directories.append(lifecycle)
    for name, expected in [('request-control-readback/copy', 0), ('request-control-readback/sync', 0), ('request-control-accept-failure', 1)]:
        parts = name.split('/')
        d = ROOT / ('.q4t-work/e2e/' + parts[0] + '-20260927')
        if len(parts) == 2:
            d /= parts[1]
        e = json.loads((d / 'exit.json').read_text())
        assert e['failure'] is None and e['server'] == expected
    for name in ['request-control-readback', 'request-control-accept-failure']:
        d = ROOT / ('.q4t-work/e2e/' + name + '-20260927')
        assert json.loads((d / 'plan.json').read_text())['binary_sha256'] == digest
        directories.append(d)
    total = 0
    for mode, cases, rows_expected in [('quality', 11, 11), ('performance', 5, 15)]:
        d = ROOT / f'.q4t-work/e2e/request-control-{mode}-20260927'
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
    for entry in exits:
        shutil.copy2(ROOT / ('.q4t-work/prepared/' + entry['name'] + '-20260927.log'), CONTROL / (entry['name'] + '-driver.log'))
    shutil.copy2(ROOT / '.q4t-work/prepared/request-control-driver-exits-20260927.json', CONTROL / 'driver-exits.json')
    shutil.copytree(Path(__file__).parent, CONTROL / 'tools-final')
    for name in ['performance_reference.json', 'performance_reference.metadata.json']:
        shutil.copy2(ROOT / 'tools/evalscope/fixtures' / name, CONTROL / ('previous-' + name))
    (CONTROL / 'summary.json').write_text(json.dumps({'binary_sha256': digest, 'audited_evalscope_rows': total,
        'lifecycle_groups': 8, 'expected_fatal_accept_server_exit': 1,
        'performance_measured': True, 'strict_non_regression_proven': False}, indent=2))
    binding = {str(f.relative_to(ROOT)): sha(f) for d in directories for f in sorted(d.rglob('*')) if f.is_file()}
    (CONTROL / 'artifact-binding.json').write_text(json.dumps(binding, indent=2))
    for name, value in binding.items():
        assert sha(ROOT / name) == value
    print({'sealed_files': len(binding), 'raw_evalscope_rows': total})


if __name__ == '__main__':
    main()
