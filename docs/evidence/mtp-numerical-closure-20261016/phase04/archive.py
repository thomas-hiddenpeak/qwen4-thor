import hashlib,json,shutil
from pathlib import Path
root=Path('/home/rm01/models/dev/qwen4-thor')
r=Path(__file__).resolve().parent
out=root/'docs/evidence/mtp-numerical-closure-20261016/phase04'
summary=json.loads((r/'summary.json').read_text())
assert summary['performance_target_pass']
assert summary['limits']['passed'] and summary['limits']['server_returncode']==0
for side in ['off','on']:
    q=summary['quality'][side]
    assert q['count']==q['exact_match']==q['length_match']==11 and not q['failures']
    rows=json.loads((r/('quality-'+side)/'results.json').read_text())
    prior=json.loads((root/'.q4t-work/mtp-mainline-ready-20261016'/('quality-'+side)/'results.json').read_text())
    assert len({x['id'] for x in rows})==11
    old_by_id={x['id']:x for x in prior}
    for row in rows:
        assert row['success']
        if row['http_status_code'] is None:
            assert row['status_source']=='evalscope success flag; this database schema does not retain HTTP status codes'
        else:
            assert row['http_status_code']==200
        assert row['prompt_sha256']==old_by_id[row['id']]['prompt_sha256']
        assert row['expected']==old_by_id[row['id']]['expected']
for job in ['off','on','quality-off','quality-on','boundaries','final-test-build','analysis']:
    assert json.loads((r/(job+'-exit.json')).read_text())['returncode']==0
for job in ['off','on','quality-off','quality-on']:
    status=json.loads((r/job/'exit.json').read_text())
    assert status['http_output_checks_passed'] and status['server']==0
    assert status['failure'] is None and status['cleanup_failure'] is None
assert hashlib.sha256((root/'build/q4t').read_bytes()).hexdigest()==summary['binary_sha256']
build=(r/'final-test-build-driver.log').read_text()
assert 'warning:' not in build and 'error:' not in build
out.mkdir(exist_ok=False)
manifest={}
for p in sorted(r.rglob('*')):
    if not p.is_file(): continue
    rel=p.relative_to(r)
    manifest[str(p.relative_to(root))]={'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
    # Large input/DB/server log evidence stays in its original work directory.
    keep=(len(rel.parts)==1 or p.name in {
        'results.json','exit.json','binary.sha256','server-command.json',
        'capacity.json','config.json','command.json','responses.json',
        'performance-plan.json','commit.txt',
        'cancellation.json','env.json','evalscope-version.txt'
    } or p.name.startswith('output-') and p.suffix=='.txt')
    if keep:
        target=out/rel
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(p,target)
(out/'raw-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
(out/'final-identity.json').write_text(json.dumps({
    'sha256':{str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in [root/'build/q4t',root/'build/q4t_tests']},
    'five_local_contracts':'phase03/five-contracts.log: 5/5 PASS, no skips; subsequent test changes only comments/diagnostic printing, rebuilt here',
    'numerics_256':'reuse phase02: identical runtime binary',
    'ple_contracts':'reuse phase02: identical runtime implementation',
},indent=2)+'\n')
print(out)
