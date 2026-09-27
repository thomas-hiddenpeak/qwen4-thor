"""Seal this bounded state-contract delivery after model and HTTP writers exit."""
import base64
import hashlib
import json
from pathlib import Path
import pickle
import shutil
import sqlite3

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / '.q4t-work/sequence-contract-control-20260927'
HTTP = ROOT / '.q4t-work/e2e/sequence-slot-quality-20260927'


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main():
    status = json.loads((HTTP / 'exit.json').read_text())
    assert status['server'] == 0 and status['http_output_checks_passed']
    assert status['completed'] == 11
    for proc in Path('/proc').iterdir():
        if proc.name.isdigit():
            try:
                argv = (proc / 'cmdline').read_bytes().split(b'\0')
                exe = (proc / 'exe').resolve(strict=True)
            except OSError:
                continue
            assert not (exe.name == 'q4t' and b'serve' in argv)
    manifest = {r['prompt_sha256']:r for r in json.loads((HTTP/'inputs/manifest.json').read_text())}
    summaries = {r['prompt_sha256']:r for r in json.loads((HTTP/'results.json').read_text())}
    db = next((HTTP/'quality').rglob('benchmark_data.db'))
    with sqlite3.connect('file:'+str(db)+'?mode=ro',uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute('select * from result order by start_time').fetchall()
    assert len(rows) == len(manifest) == len(summaries) == 11
    seen = set()
    for r in rows:
        request = json.loads(r['request'])
        h = hashlib.sha256(request['prompt'].encode()).hexdigest()
        assert h not in seen
        seen.add(h)
        messages = pickle.loads(base64.b64decode(r['response_messages']))
        choices = [c for m in messages for c in m.get('choices',[])]
        text = ''.join(c.get('delta',c.get('message',{})).get('content','') for c in choices)
        finish = [c['finish_reason'] for c in choices if c.get('finish_reason')]
        assert r['success'] and text == summaries[h]['text']
        assert text.strip() == manifest[h]['expected'] and finish == ['stop']
        assert r['prompt_tokens'] == manifest[h]['length'] == summaries[h]['actual_input']
        assert r['completion_tokens'] == summaries[h]['actual_output']
    directories = [OUT,HTTP]
    for name,code in [('baseline',1),('fixed',1),('fixed-v2',0)]:
        d = ROOT/f'.q4t-work/sequence-slot-{name}-20260927'
        assert json.loads((d/'exit.json').read_text())['returncode'] == code
        directories.append(d)
        binding = json.loads((d/'binding.json').read_text())['libraries']
        target = OUT/('baseline-libs' if name=='baseline' else 'fixed-libs')
        target.mkdir(exist_ok=True)
        for name,digest in binding.items():
            source = Path(name)
            assert sha(source) == digest
            dest = target/source.name
            if not dest.exists(): shutil.copy2(source,dest)
            assert sha(dest) == digest
    plan=json.loads((OUT/'plan.json').read_text())
    assert sha(ROOT/'docs/SEQUENCE_CONTRACT_2026-09-27.md') == plan['plan_sha256']
    binary=ROOT/'.q4t-work/sequence-slot-build-20260927/q4t'
    assert sha(binary)==(HTTP/'binary.sha256').read_text().strip()
    shutil.copy2(binary,OUT/'accepted-q4t')
    shutil.copy2(binary.parent/'CMakeCache.txt',OUT/'candidate-CMakeCache.txt')
    shutil.copytree(Path(__file__).parent,OUT/'tools-final')
    (OUT/'summary.json').write_text(json.dumps({'numerical_passed':True,
        'http_raw_records_verified':11,'http_quality':'11/11 unchanged',
        'performance_measured':False,'binary_sha256':sha(binary),
        'scope':'Fixed slot binding, short full-model recurrence/cache/logit comparisons; not full path matrix'},indent=2))
    binding={str(f.relative_to(ROOT)):sha(f) for d in directories for f in sorted(d.rglob('*')) if f.is_file()}
    target=OUT/'artifact-binding.json'
    assert not target.exists()
    target.write_text(json.dumps(binding,indent=2))
    for name,digest in binding.items(): assert sha(ROOT/name)==digest,name
    print({'sealed_files':len(binding),'raw_http_records':11,'binary_sha256':sha(binary)})


if __name__=='__main__':
    main()
