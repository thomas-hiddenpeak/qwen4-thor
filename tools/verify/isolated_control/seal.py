"""Audit the isolated-control experiment after all processes have exited."""
import hashlib
import json
from pathlib import Path
import shutil

ROOT=Path(__file__).resolve().parents[3]
CONTROL=ROOT/'.q4t-work/isolated-control-control-20260927'
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()


def main():
    assert not CONTROL.exists()
    shared=ROOT/'.q4t-work/e2e/isolated-control-shared-v2-20260927'
    isolated=ROOT/'.q4t-work/e2e/isolated-control-v2-20260927'
    failed=ROOT/'.q4t-work/e2e/isolated-control-shared-20260927'
    completed=json.loads((ROOT/'.q4t-work/prepared/isolated-driver-exits-v2-20260927.json').read_text())
    assert len(completed)==2 and all(x['returncode']==0 for x in completed)
    plans=[json.loads((d/'plan.json').read_text()) for d in [shared,isolated]]
    for d in [shared,isolated]:
        e=json.loads((d/'exit.json').read_text())
        assert e['failure'] is None and e['server']==0
        assert all(x['exit']==0 for x in e['proxy_exits'])
    assert json.loads((shared/'shared-health.json').read_text()).get('status')!=200
    binary=Path(plans[0]['command'][0]);nginx=Path(plans[0]['profiles']['data'][0])
    assert sha(binary)==plans[0]['binary_sha256']==plans[1]['binary_sha256']
    assert sha(nginx)==plans[0]['nginx_sha256']==plans[1]['nginx_sha256']
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():continue
        try:
            argv=(proc/'cmdline').read_bytes().split(b'\0');exe=(proc/'exe').resolve(strict=True)
        except OSError:continue
        assert not(exe==binary and b'serve' in argv)
        assert exe!=nginx
        assert not any(Path(a.decode(errors='replace')).name in ['run_isolated_control.py','run-isolated-control-v2-20260927.py'] for a in argv)
    health=[];cancel=[]
    for i in range(3):
        x=json.loads((isolated/f'flood-{i}-exhaustion.json').read_text())
        assert x['data_master']!=x['control_master'] and set(x['data_workers']).isdisjoint(x['control_workers'])
        for j in range(5):
            r=json.loads((isolated/f'flood-{i}-health-{j}.json').read_text());assert r['status']==200 and r['seconds']<2;health.append(r['seconds'])
        r=json.loads((isolated/f'flood-{i}-cancel.json').read_text());assert r['status']==202 and r['seconds']<2;cancel.append(r['seconds'])
        assert json.loads((isolated/f'flood-{i}-response.json').read_text())['status']==409
    assert (isolated/'data/error.log').read_text().count('worker_connections are not enough')>=3
    assert json.loads((isolated/'data-stop-active-response.json').read_text()).get('status')!=200
    normal=[(shared,'flood-0-recovery','710003',1024)]
    normal += [(isolated,f'flood-{i}-recovery','710003',1024) for i in range(3)]
    normal += [(isolated,'reload-active','711146',45056),(isolated,'data-restart-recovery','710003',1024),(isolated,'control-stop-active','711146',45056),(isolated,'context-200k','711273',204800)]
    for d,label,text,tokens in normal:
        r=json.loads((d/(label+'-response.json')).read_text());assert r['status']==200 and r['body'].strip().endswith('data: [DONE]')
        events=[json.loads(x[6:]) for x in r['body'].splitlines() if x.startswith('data: {')];choices=[c for e in events for c in e.get('choices',[])]
        assert ''.join(c.get('delta',{}).get('content','') for c in choices)==text
        assert [c['finish_reason'] for c in choices if c.get('finish_reason')]==['stop']
        usage=next(e['usage'] for e in events if e.get('usage'));assert usage['prompt_tokens']==tokens and usage['completion_tokens']==7
    for role in ['data','control']:
        assert sha(ROOT/f'tools/deploy/nginx/q4t-{role}.conf.in')==sha(isolated/role/f'q4t-{role}.conf.in')
    CONTROL.mkdir();shutil.copy2(binary,CONTROL/'tested-q4t');shutil.copy2(nginx,CONTROL/'tested-nginx')
    shutil.copytree(Path(__file__).parent,CONTROL/'tools-final')
    shutil.copytree(ROOT/'tools/deploy/nginx',CONTROL/'profiles-final')
    for name in ['isolated-control-shared','isolated-control-shared-v2','isolated-control-v2']:
        shutil.copy2(ROOT/('.q4t-work/prepared/'+name+'-20260927.log'),CONTROL/(name+'-driver.log'))
    shutil.copy2(ROOT/'.q4t-work/prepared/isolated-driver-exits-v2-20260927.json',CONTROL/'driver-exits.json')
    summary={'normal_responses':len(normal),'health_calls':len(health),'max_health_seconds':max(health),'max_cancel_seconds':max(cancel),'runner_changed':False,'production_slo_proven':False}
    (CONTROL/'summary.json').write_text(json.dumps(summary,indent=2))
    binding={str(f.relative_to(ROOT)):sha(f) for d in [CONTROL,shared,isolated,failed] for f in sorted(d.rglob('*')) if f.is_file()}
    (CONTROL/'artifact-binding.json').write_text(json.dumps(binding,indent=2));assert all(sha(ROOT/f)==h for f,h in binding.items())
    print(json.dumps({'files':len(binding),**summary}))


if __name__=='__main__':main()
