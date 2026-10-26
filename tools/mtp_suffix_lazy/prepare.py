"""Freeze the one selected lazy-classifier candidate; no model/test execution."""
import hashlib,json,re,shutil,subprocess,sys,time
from pathlib import Path
from run_stage import ROOT,reuse,validate
from object_identity import compare
BASE=ROOT/'.q4t-work/suffix-causal-20261026'
OLD=ROOT/'.q4t-work/suffix-admission-20261024'

def sha(p):
    with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def save(p,value):
    with p.open('x') as f:json.dump(value,f,indent=2,ensure_ascii=False);f.write('\n')

def main():
    stage=BASE/'acceptance-b';stage.mkdir(exist_ok=False)
    selection=json.loads((BASE/'selection-b.json').read_text());build=json.loads((BASE/'build-lazy-result.json').read_text())
    source=json.loads((BASE/'build-lazy-source.json').read_text());old=json.loads((OLD/'acceptance-v2/frozen-plan.json').read_text());analysis=json.loads((OLD/'analysis.json').read_text())
    if not build['passed'] or time.time()+14400+1500>=selection['task_deadline_unix']:raise RuntimeError('build/global remaining budget')
    changed_tools=[]
    for name,h in source['files'].items():
        p=Path(name)
        if sha(p)!=h:
            if p.suffix=='.py':changed_tools.append(name)
            else:raise RuntimeError('compiled input changed: '+name)
    if sha(ROOT/'build/q4t')!=old['parent_sha256']:raise RuntimeError('default changed')
    fixtures=stage/'fixtures';fixtures.mkdir();shutil.copytree(OLD/'acceptance-v1/fixtures/quality',fixtures/'quality')
    performance=[]
    for c in old['performance']+old['scenarios']:
        if c['id'] not in ['context-1024','context-4096','context-8192','context-45056','context-204800','repeat-code','repeat-text']:continue
        r=dict(c);target=fixtures/(c['id']+'.jsonl');target.write_text((Path(c['path']).read_text().splitlines()[0]+'\n')*3)
        r.update(path=str(target),source_path=c['path'],source_sha256=sha(Path(c['path'])),repeats=3,
                 role='reused_acceptance_fixture',novel_holdout=False)
        performance.append(r)
    development=[dict(c) for c in old['development'] if c['id']=='old-repeat-code']
    plan=dict(version=1,classification='performance optimization; single lazy classification candidate',selection=selection,
        candidate=build['q4t']['path'],candidate_sha256=build['q4t']['sha256'],candidate_source_commit=source['source_commit'],
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
        uncompiled_tool_changes_after_build=changed_tools,model=old['model'],artifact=old['artifact'],
        subset_manifest_sha256=old['subset_manifest_sha256'],parent=str(ROOT/'build/q4t'),parent_sha256=old['parent_sha256'],
        output=str(stage/'run'),lease=str(stage/'lease'),port=8762,http_requests=226,http_loads=10,
        stage_timeout_seconds=14400,direct_contract_timeout_seconds=1200,contract_seconds=1200,
        resource_sample_interval_seconds=1,resource_sample_max_age_seconds=5,retries=0,warmups=0,
        task_deadline_unix=selection['task_deadline_unix'],telemetry_sources=reuse.telemetry.discover_sources(),
        quality_fixtures=str(fixtures/'quality'),development=development,performance=performance,
        admission='complete-three,d0,min32,numeric; longest/latest unchanged, no alternative search',
        goals='each head/code/text paired ratio positive in both blocks; paired geometric mean >=1.05',
        protections='each five-tier TTFT/decode, ordinary decode, quality, lifecycle, exact output/work and unified RAM; no offsetting confirmed regression',
        statistics='two startup pairs/head, 3 within-service repeats; descriptive medians, first requests separately; no inferred margin or population noninferiority claim',
        cache='natural inherited; no added warmup, cache drop, affinity or power changes',
        quality_prelude='first fullON and first subsetON include11quality before their performance; preserve this asymmetry and first-request markers explicitly',
        first_test='fullON11 evalscope HTTP quality including release/actual suffix gate before affected CPU contracts',
        gpu_contract_reuse='target/draft/scheduler numerical behavior unchanged; compare compute object identity against valid A build; retain original two-GDN/captured-draft oracle limits, not universal equality',
        model_file_metadata=old['model_file_metadata'],files={},steps=[],contracts=[])
    plan['contracts']=[dict(name='affected-suffix-host',command=[build['q4t_host_tests']['path'],'suffix_'],timeout=120),
        dict(name='exhaustive-cache-and-recorded-match-replay',command=[build['cache_replay']['path'],str(Path(old['model'])/'tokenizer.json'),
         str(OLD/'diagnosis-index/index-replay.json'),str(OLD/'diagnosis-index/token-burst-replay.json')],timeout=120),
        dict(name='lazy-audit-contracts',command=[sys.executable,'-B',str(ROOT/'tools/mtp_suffix_lazy/test_audit.py')],timeout=120)]
    choices=[('full',True,1),('full',False,1),('subset',False,1),('subset',True,1),
             ('full',False,2),('full',True,2),('subset',True,2),('subset',False,2),('plain',False,0),('plain',False,0)]
    for i,(head,on,block) in enumerate(choices):
        name=f'{i+1:02d}-{head}-'+('on' if on else 'off')+f'-b{block}'
        if head=='plain':name=f'{i+1:02d}-plain-'+('parent' if i==8 else 'candidate')
        qa=i in (0,3);life=qa;cases=[]
        for c in performance[:5] if head=='plain' else performance:
            group=analysis['groups']['performance-parent-plain' if head=='plain' else f'block-1-{head}-'+('on' if on else 'off')]
            ref=next(x for x in group['cases'] if x['id']==c['id'])
            hashes={r['output_sha256'] for r in ref['raw']}
            if len(hashes)!=1:raise RuntimeError('reference output not stable')
            hist=None
            if head!='plain':
                hs={tuple(r['accepted_a0_a1_a2_a3']) for r in ref['raw']}
                if len(hs)!=1:raise RuntimeError('reference work not stable')
                hist=list(next(iter(hs)))
            cases.append(dict(c,expected_output_sha256=next(iter(hashes)),expected_histogram=hist))
        plan['steps'].append(dict(name=name,kind='block',head=head,suffix=on,block=block,quality=qa,lifecycle=life,cases=cases,
            binary=plan['parent'] if i==8 else plan['candidate'],http_loads=1,http_requests=sum(c['repeats'] for c in cases)+11*qa+3*life,
            timeout=2400,command=[sys.executable,'-B',str(ROOT/'tools/mtp_suffix_lazy/run_stage.py'),'--plan',str(stage/'frozen-plan.json'),'--custom-step',name]))
    paths=set(map(Path,source['files']))
    bdir=Path(plan['candidate']).parent
    for p in bdir.rglob('*.o.d'):
        paths.add(p)
        for name in p.read_text().replace('\\\n',' ').split(':',1)[1].split():
            dep=Path(name)
            if dep.is_absolute() and dep.is_file():paths.add(dep)
    paths.update([bdir/'CMakeCache.txt',bdir/'compile_commands.json',bdir/'CMakeFiles/q4t.dir/link.txt',bdir/'libq4t_text.a',bdir/'libq4t_io.a'])
    for r in ['q4t','q4t_host_tests','cache_replay']:paths.add(Path(build[r]['path']))
    paths.update([ROOT/'build/q4t',ROOT/'build/q4t.release.json',BASE/'scope.json',BASE/'selection-b.json',BASE/'build-lazy-source.json',BASE/'build-lazy-result.json',BASE/'build-lazy.log',BASE/'audit-a.json',BASE/'analysis-a.json',OLD/'analysis.json',OLD/'acceptance-v2/frozen-plan.json',OLD/'diagnosis-index/index-replay.json',OLD/'diagnosis-index/token-burst-replay.json'])
    for folder in ['tools/mtp_suffix_lazy','tools/mtp_suffix_zero_hit','tools/mtp_suffix','tools/mtp_suffix_admission','tools/mtp_suffix_numeric','tools/evalscope']:
        paths.update((ROOT/folder).glob('*.py'))
    for name in ['AGENTS.md','docs/EVALUATION.md','docs/PERFORMANCE_DECISION_PROTOCOL.md','docs/MTP_SUFFIX_CAUSAL_PLAN_2026-10-26.md','docs/MTP_SUFFIX_CAUSAL_RESULTS_2026-10-26.md']:paths.add(ROOT/name)
    paths.update(p for p in fixtures.rglob('*') if p.is_file());paths.update(Path(c['path']) for c in development)
    for name,h in old['files'].items():
        p=Path(name)
        if p.is_relative_to(Path(old['model'])) or p.parent==Path(old['artifact']):
            if sha(p)!=h:raise RuntimeError('model/subset identity changed: '+name)
            paths.add(p)
    linked=subprocess.check_output(['ldd',plan['candidate']],text=True);(stage/'ldd.txt').write_text(linked);paths.add(stage/'ldd.txt')
    for name in re.findall(r'(/[^\s()]+)',linked):
        p=Path(name)
        if p.is_file():paths.add(p.resolve())
    import glob
    for pattern in ['/opt/nvidia/l4t-gpu-libs/openrm/libcuda.so*','/usr/lib/aarch64-linux-gnu/nvidia/libnvidia-ml.so*']:
        paths.update(Path(p).resolve() for p in glob.glob(pattern) if Path(p).is_file())
    paths.update([Path(sys.executable).resolve(),ROOT/'tools/evalscope/.venv/bin/evalscope'])
    for folder in (ROOT/'tools/evalscope/.venv/lib').glob('python*/site-packages/evalscope'):paths.update(folder.rglob('*.py'))
    objects=[]
    for p in (bdir/'CMakeFiles').rglob('*.cu.o'):
        oldobj=BASE/'build-b'/p.relative_to(bdir)
        objects.append(dict(path=str(p.relative_to(bdir)),**compare(p,oldobj)));paths.update([p,oldobj])
    if not objects or not all(o['equal'] for o in objects):raise RuntimeError('GPU compute object identity changed; reuse not eligible')
    plan['unchanged_compute_objects']=objects
    snapshot=stage/'source-snapshot';snapshot.mkdir()
    for p in sorted(paths):
        plan['files'][str(p)]=sha(p)
        if p.is_relative_to(ROOT) and '.q4t-work' not in p.relative_to(ROOT).parts and '.venv' not in p.parts:
            target=snapshot/p.relative_to(ROOT);target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,target)
    plan['source_snapshot']=str(snapshot);validate(plan);save(stage/'frozen-plan.json',plan)
    print(json.dumps(dict(plan=str(stage/'frozen-plan.json'),sha256=sha(stage/'frozen-plan.json'),requests=226,starts=10,files=len(paths),unchanged_compute_objects=len(objects))))

if __name__=='__main__':main()
