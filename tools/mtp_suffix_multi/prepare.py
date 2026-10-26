"""Freeze F inputs/identities before the first test; never loads a model."""
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from run_stage import ROOT, reuse, validate

BASE=ROOT/'.q4t-work/suffix-multi-20261027'
OLD=ROOT/'.q4t-work/suffix-causal-20261026/acceptance-b/frozen-plan.json'
def sha(p):
    with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def save(p,x):
    with p.open('x') as f:json.dump(x,f,indent=2,ensure_ascii=False);f.write('\n')


def main():
    old=json.loads(OLD.read_text())
    build=json.loads((BASE/'build-3-result.json').read_text())
    source=json.loads((BASE/'build-3-source.json').read_text())
    if not build['passed']:raise RuntimeError('build not clean')
    if not json.loads((BASE/'capacity-cpu-v2-result.json').read_text())['passed']:
        raise RuntimeError('capacity repair CPU contract not passed')
    for p,h in source['files'].items():
        if sha(Path(p))!=h:raise RuntimeError('source changed since build: '+p)
    stage=BASE/'feasibility-v2';stage.mkdir()
    first=json.loads((BASE/'feasibility/frozen-plan.json').read_text())
    prior=json.loads((BASE/'feasibility/run/state.json').read_text())
    fixtures=stage/'fixtures';fixtures.mkdir()
    original={c['id']:c for c in old['performance']}
    def fixture(name,keys):
        p=fixtures/(name+'.jsonl')
        p.write_text(''.join(Path(original[k]['path']).read_text().splitlines()[0]+'\n' for k in keys))
        return str(p)
    cases=[]
    for name,keys in [('repeat-code',['repeat-code']*4),
        ('repeat-text',['repeat-text']*4),
        ('mixed',['repeat-code','context-8192','repeat-text','context-8192']),
        ('nonrepeat',['context-8192']*4)]:
        cases.append(dict(id=name,path=fixture(name,keys),count=4,parallel=4,tokens=256,
            min=min(original[k]['input_min'] for k in keys),
            max=max(original[k]['input_max'] for k in keys),
            sources=[dict(original[k]) for k in keys]))
    qdir=Path(old['quality_fixtures'])
    quality={r['prompt_sha256']:r for r in json.loads((qdir/'manifest.json').read_text()) if r['length']<=8192}
    qpath=fixtures/'quality.jsonl'
    qpath.write_text(''.join(line+'\n' for line in (qdir/'requests.jsonl').read_text().splitlines()
        if hashlib.sha256(json.loads(line)['prompt'].encode()).hexdigest() in quality))
    if len(quality)!=9 or len(qpath.read_text().splitlines())!=9:raise RuntimeError('nine frozen quality prompts')
    qcase=dict(id='quality',path=str(qpath),count=9,parallel=4,tokens=32,min=1024,max=8192)
    queue=[]
    for n in [16,32]:
        p=fixture(f'queue-{n}',['repeat-code']*n)
        queue.append(dict(id=f'queue-{n}',path=p,count=n,parallel=n,tokens=32,
            min=original['repeat-code']['input_min'],max=original['repeat-code']['input_max']))
    saved=stage/'programs';saved.mkdir()
    for name in ['q4t','q4t_tests','q4t_host_tests']:
        shutil.copy2(build[name]['path'],saved/name)
    binary=str(saved/'q4t');tests=str(saved/'q4t_tests')
    plan=dict(stage='F',output=str(stage/'run'),lease=str(stage/'lease'),
        candidate=binary,candidate_sha256=sha(Path(binary)),parent=str(ROOT/'build/q4t'),
        parent_sha256=sha(ROOT/'build/q4t'),artifact=old['artifact'],model=old['model'],
        model_file_metadata=old['model_file_metadata'],quality=quality,port=8762,
        source_commit=source['commit'],http_requests=136,http_loads=4,
        stage_timeout_seconds=int(prior['started']+7200-time.time()),direct_contract_timeout_seconds=1200,
        task_deadline_unix=first['task_deadline_unix'],
        prior_failure=str(BASE/'feasibility/run/state.json'),prior_start_attempts=1,
        development=old['development'],telemetry_sources=reuse.telemetry.discover_sources(),
        resource_sample_interval_seconds=1,resource_sample_max_age_seconds=5,
        warmups=0,retries=0,cache='natural inherited; no flush/power/affinity changes',
        design=str(ROOT/'docs/MTP_SUFFIX_MULTI_PLAN_2026-10-27.md'),
        goals='F feasibility only: both heads repeated code/text positive throughput; then B',
        statistics='one startup pair per head; descriptive, no noninferiority claim',
        host_contracts=[[build['q4t_host_tests']['path'],f] for f in ['suffix_','serve_']],
        steps=[],files={})
    if plan['parent_sha256']!='90f24f49ea93e7ddbb359e817f05a9d39f87a54f66433c01a8d46f198f489bd4':
        raise RuntimeError('delivered baseline changed')
    for n,(head,on) in enumerate([('full',True),('full',False),('subset',False),('subset',True)],1):
        name=f'{n:02d}-{head}-'+('on' if on else 'off')
        selected=([qcase] if on else [])+cases+(queue if n==4 else [])
        plan['steps'].append(dict(name=name,kind='block',head=head,suffix=on,cases=selected,
            http_loads=1,http_requests=sum(c['count'] for c in selected)+3*on,timeout=1800,
            command=[sys.executable,'-B',str(ROOT/'tools/mtp_suffix_multi/run_stage.py'),
                '--plan',str(stage/'frozen-plan.json'),'--custom-step',name]))
        if n==1:
            for contract_head in ['full','subset']:
                plan['steps'].append(dict(name='contract-'+contract_head,kind='contract',
                    http_loads=0,http_requests=0,timeout=600,command=[tests,'mtp_suffix'],
                    env={} if contract_head=='full' else {'Q4T_TEST_SUBSET_HEAD':plan['artifact']}))
    validate(plan)
    paths={Path(p) for p in source['files']}
    paths.update([OLD,BASE/'build-1-result.json',BASE/'build-1.log',BASE/'build-source.json',
        BASE/'build-3-result.json',BASE/'build-3.log',BASE/'build-3-source.json',
        ROOT/'build/q4t',ROOT/'build/q4t.release.json',Path(binary),Path(tests),
        Path(build['q4t_host_tests']['path']),Path(plan['design']),ROOT/'AGENTS.md',
        BASE/'capacity-cpu-result.json',BASE/'capacity-cpu-v2-result.json',
        ROOT/'docs/MTP_SUFFIX_MULTI_REPAIR_2026-10-27.md',ROOT/'docs/EVALUATION.md',ROOT/'docs/PERFORMANCE_DECISION_PROTOCOL.md'])
    bdir=Path(build['q4t']['path']).parent
    for p in bdir.rglob('*.o.d'):
        paths.add(p)
        for name in p.read_text().replace('\\\n',' ').split(':',1)[1].split():
            dep=Path(name)
            if dep.is_absolute() and dep.is_file():paths.add(dep)
    paths.update([bdir/'CMakeCache.txt',bdir/'compile_commands.json',bdir/'CMakeFiles/q4t.dir/link.txt'])
    for folder in ['tools/mtp_suffix_multi','tools/mtp_suffix_lazy','tools/mtp_suffix_zero_hit',
                   'tools/mtp_suffix_admission','tools/mtp_suffix','tools/evalscope']:
        paths.update((ROOT/folder).glob('*.py'))
    paths.update(p for p in fixtures.rglob('*') if p.is_file())
    paths.update(p for p in (BASE/'capacity-cpu-v2').rglob('*') if p.is_file())
    paths.update(p for p in (BASE/'feasibility').rglob('*') if p.is_file())
    paths.update(Path(c['path']) for c in plan['development'])
    paths.add(Path(build['q4t']['path']))
    paths.add(Path(build['q4t_tests']['path']))
    for name,h in old['files'].items():
        p=Path(name)
        if p.is_relative_to(Path(old['model'])) or p.parent==Path(old['artifact']):
            if sha(p)!=h:raise RuntimeError('model/artifact identity changed')
            paths.add(p)
    libraries=subprocess.check_output(['ldd',binary],text=True)
    (stage/'ldd.txt').write_text(libraries);paths.add(stage/'ldd.txt')
    for name in re.findall(r'(/[^\s()]+)',libraries):
        p=Path(name)
        if p.is_file():paths.add(p.resolve())
    paths.add(Path(sys.executable).resolve())
    paths.add(ROOT/'tools/evalscope/.venv/bin/evalscope')
    for folder in (ROOT/'tools/evalscope/.venv/lib').glob('python*/site-packages/evalscope'):
        paths.update(folder.rglob('*.py'))
    patch=subprocess.check_output(['git','diff','--binary']);(stage/'tracked.patch').write_bytes(patch)
    paths.add(stage/'tracked.patch')
    snapshot=stage/'source';snapshot.mkdir()
    for p in sorted(paths):
        plan['files'][str(p)]=sha(p)
        if p.is_relative_to(ROOT) and not any(part in p.relative_to(ROOT).parts
                for part in ['.q4t-work','.venv','build']):
            dest=snapshot/p.relative_to(ROOT);dest.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(p,dest)
    save(stage/'frozen-plan.json',plan)
    print(json.dumps(dict(plan=str(stage/'frozen-plan.json'),sha256=sha(stage/'frozen-plan.json'),
        files=len(paths),http=136,starts=4,candidate=plan['candidate_sha256'])))

if __name__=='__main__':main()
