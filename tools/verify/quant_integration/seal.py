"""Seal the completed September 27 numerical-fix delivery, never live writers."""
from pathlib import Path
import base64
import hashlib
import json
import pickle
import sqlite3
import shutil

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / '.q4t-work/numerical-acceptance-20260927'


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(4 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def main():
    assert 'All requested collection complete' in (OUT / 'finish.log').read_text()
    for proc in Path('/proc').iterdir():
        if proc.name.isdigit():
            try:
                argv = (proc / 'cmdline').read_bytes().split(b'\0')
                exe = (proc / 'exe').resolve(strict=True)
            except OSError:
                continue
            assert not (exe.name == 'q4t' and b'serve' in argv)
    stages = ['numerical-fixed-quality', 'numerical-clean-quality',
              'numerical-clean-performance']
    directories = [OUT]
    audits = []
    for stage in stages:
        d = ROOT / f'.q4t-work/e2e/{stage}-20260927'
        directories.append(d)
        status = json.loads((d / 'exit.json').read_text())
        assert status['server'] == 0
        quality = stage.endswith('quality')
        manifest = {r['prompt_sha256']: r for r in json.loads((d / 'inputs/manifest.json').read_text())} if quality else {}
        count = 0
        for db in d.rglob('benchmark_data.db'):
            case = db.parents[3]
            expected = [json.loads(x) for x in (case / 'requests.jsonl').read_text().splitlines()]
            summary = json.loads((case / 'responses.json').read_text())
            with sqlite3.connect('file:' + str(db) + '?mode=ro', uri=True) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute('select * from result order by start_time').fetchall()
            assert len(rows) == len(summary) == (len(expected) if quality else 3)
            for row, s in zip(rows, summary):
                request = json.loads(row['request'])
                matching = [r for r in expected if r['prompt'] == request['prompt']]
                assert matching and all(request[k] == v for k, v in matching[0].items())
                messages = pickle.loads(base64.b64decode(row['response_messages']))
                choices = [c for m in messages for c in m.get('choices', [])]
                text = ''.join(c.get('delta', c.get('message', {})).get('content', '') for c in choices)
                finish = [c['finish_reason'] for c in choices if c.get('finish_reason')]
                assert row['success'] and text == s['text'] and finish == s['finish']
                assert row['prompt_tokens'] == s['actual_input']
                assert row['completion_tokens'] == s['actual_output']
                assert hashlib.sha256(request['prompt'].encode()).hexdigest() == s['prompt_sha256']
                assert finish == (['stop'] if quality else ['length'])
                if quality:
                    assert row['prompt_tokens'] == manifest[s['prompt_sha256']]['length']
                if not quality:
                    assert row['completion_tokens'] == 256
                    assert row['prompt_tokens'] == int(case.name.split('-')[1])
            count += len(rows)
        assert count == (26 if quality else 15)
        audits.append({'stage': stage, 'records': count, 'server_exit': 0,
                       'binary_sha256': (d / 'binary.sha256').read_text().strip()})
    for kind in ['baseline-final', 'fixed-final', 'captured']:
        directories.append(ROOT / f'.q4t-work/quant-integration-{kind}-20260927')
    for d in sorted((ROOT / '.q4t-work').glob('quant-integration-*-20260927')):
        if d.is_dir() and d not in directories:
            directories.append(d)
    metrics = []
    for r in json.loads((directories[3] / 'results.json').read_text()):
        values = r['metrics']
        times = [v['ttft'] for v in values]
        speeds = [v['decode_tps'] for v in values]
        metrics.append({'length': r['length'], 'ttft_mean': sum(times)/3,
                        'ttft_range': [min(times), max(times)],
                        'decode_tps': 3/sum(1/x for x in speeds),
                        'decode_range': [min(speeds), max(speeds)]})
    result = {'http_audit': audits, 'performance': metrics,
              'scope': 'Finite numerical integration accepted; performance baseline only; no general model or commercial certification'}
    (OUT / 'delivery.json').write_text(json.dumps(result, indent=2))
    shutil.copytree(Path(__file__).parent, OUT / 'tools-final')
    binary = ROOT / '.q4t-work/numerical-clean-build-20260927/q4t'
    assert sha(binary) == json.loads((OUT / 'performance-plan.json').read_text())['binary_sha256']
    shutil.copy2(binary, OUT / 'accepted-q4t')
    binding = {str(f.relative_to(ROOT)): sha(f) for d in directories
               for f in sorted(d.rglob('*')) if f.is_file()}
    target = OUT / 'artifact-binding.json'
    assert not target.exists()
    target.write_text(json.dumps(binding, indent=2))
    for name, digest in binding.items():
        assert sha(ROOT / name) == digest, name
    print(json.dumps({'records': sum(x['records'] for x in audits),
                      'bound_files': len(binding), 'performance': metrics}, indent=2))


if __name__ == '__main__':
    main()
