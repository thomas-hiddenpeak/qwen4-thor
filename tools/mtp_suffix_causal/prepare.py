"""Freeze A identity/order/fixtures. No tests, model loading or sensor sampling."""
import hashlib
import json
from pathlib import Path
import random
import re
import shutil
import subprocess
import sys
from run_stage import ROOT, rerun, validate

BASE=ROOT/'.q4t-work/suffix-causal-20261026'
OLD=ROOT/'.q4t-work/suffix-admission-20261024'

def sha(path):
    with path.open('rb') as f: return hashlib.file_digest(f,'sha256').hexdigest()

def save(path,value):
    with path.open('x') as f: f.write(json.dumps(value,ensure_ascii=False,indent=2)+'\n')

def main():
    stage=BASE/'diagnostic-a'
    stage.mkdir(exist_ok=False)
    scope=json.loads((BASE/'scope.json').read_text())
    build=json.loads((BASE/'build-result-v2.json').read_text())
    if not build['passed']: raise RuntimeError('build failed')
    source=json.loads((BASE/'build-source-v2.json').read_text())
    for name,h in source['files'].items():
        if sha(Path(name))!=h: raise RuntimeError('compiled source changed: '+name)
    old=json.loads((OLD/'acceptance-v2/frozen-plan.json').read_text())
    analysis=json.loads((OLD/'analysis.json').read_text())
    paths=set()
    contexts=['context-8192','context-45056','context-204800']
    cases=[]
    for context in contexts:
        c=dict(next(c for c in old['performance'] if c['id']==context))
        original=Path(c['path']);target=stage/(context+'.jsonl')
        target.write_text(original.read_text().splitlines()[0]+'\n')
        row=next(c for c in analysis['groups']['block-1-subset-on']['cases'] if c['id']==context)['raw'][0]
        work=row['whole_generation_work']
        c.update(path=str(target),context=context,repeats=1,role='development_diagnostic',
                 source_path=str(original),source_sha256=sha(original),expected_output_sha256=row['output_sha256'],
                 expected_work=dict(verify=work['verify_calls'],neural=work['neural_proposal_T1'],
                     restore=work['target_restore_calls'],extend=work['draft_extend_calls'],
                     extend_rows=work['mtp_emitted_tokens'],tail=work['ordinary_tail_target_T1']))
        cases.append(c);paths.update([original,target])
    rng=random.Random(202610262019)
    labels=['N1','N2','Q','R'];rng.shuffle(labels)
    templates=[[1,2,4,3],[2,3,1,4],[3,4,2,1],[4,1,3,2]]
    row_order=list(range(4));rng.shuffle(row_order)
    plan=dict(version=1,classification='development causal diagnosis, not acceptance',contexts=contexts,
              candidate=build['q4t']['path'],candidate_sha256=build['q4t']['sha256'],
              contract_binary=build['causal_contracts']['path'],candidate_source_commit=source['source_commit'],
              source_commit=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
              model=old['model'],artifact=old['artifact'],subset_manifest_sha256=old['subset_manifest_sha256'],
              parent=str(ROOT/'build/q4t'),parent_sha256=old['parent_sha256'],
              output=str(stage/'run'),lease=str(stage/'lease'),port=8762,
              http_requests=60,http_loads=6,stage_timeout_seconds=7200,direct_contract_timeout_seconds=0,
              resource_sample_interval_seconds=1,resource_sample_max_age_seconds=5,retries=0,warmups=0,
              task_deadline_unix=scope['started_unix']+scope['total_wall_seconds'],
              telemetry_sources=rerun.telemetry.discover_sources(),
              seed=202610262019,labels_1_to_4=labels,template_row_order=row_order,
              analysis='Report every startup-pair and within-quartet difference, N2-N1, Q-mean(N1,N2), R-Q; by service, context, position, predecessor. No inferred tolerance or request-bootstrap. No significance=equivalence claim.',
              cache='natural inherited; no warmup/drop-cache/affinity/power changes',
              first_test='real evalscope HTTP; bounded CPU after first output/work/finish/release gate',
              model_file_metadata=old['model_file_metadata'],files={},steps=[])
    for i in range(6):
        suffix=i not in (0,3); name=f'{i+1:02d}-'+('startup-S'+str(int(suffix)) if i<4 else 'mixed')
        chosen=[]
        if i<4:
            chosen=[dict(c,mode='N1',quartet=None,position=j,predecessor='N1' if j else None) for j,c in enumerate(cases)]
        else:
            for c in (cases if i==4 else list(reversed(cases))):
                for q in range((i-4)*2,(i-4)*2+2):
                    for pos,label_id in enumerate(templates[row_order[q]]):
                        chosen.append(dict(c,mode=labels[label_id-1],quartet=q,position=pos,
                                           predecessor=chosen[-1]['mode'] if chosen else None))
        for j,c in enumerate(chosen): c['id']=f"{j+1:02d}-{c['context']}-{c['mode']}"
        modes=stage/(name+'.txt');modes.write_text('suffix-causal-v1 S'+str(int(suffix))+'\n'+
                             ''.join(f"{c['input_tokens']} {c['mode']}\n" for c in chosen));paths.add(modes)
        plan['steps'].append(dict(name=name,kind='timing',head='subset',suffix=suffix,block=i,
            cases=chosen,mode_plan=str(modes),http_requests=len(chosen),http_loads=1,timeout=1200 if i<4 else 2700,
            command=[sys.executable,'-B',str(ROOT/'tools/mtp_suffix_causal/run_stage.py'),
                     '--plan',str(stage/'frozen-plan.json'),'--custom-step',name]))
    # Full current compilation inputs and actual linked DSOs, including driver
    # dlopen libraries not visible to ldd. Old model/subset hashes must still match.
    paths.update(map(Path,source['files']))
    bdir=Path(plan['candidate']).parent
    for p in bdir.rglob('*.o.d'):
        paths.add(p)
        content=p.read_text().replace('\\\n',' ')
        for name in content.split(':',1)[1].split():
            dep=Path(name)
            if dep.is_absolute() and dep.is_file(): paths.add(dep)
    paths.update([bdir/'CMakeCache.txt',bdir/'compile_commands.json',bdir/'CMakeFiles/q4t.dir/link.txt',
                  Path(plan['candidate']),Path(plan['contract_binary']),ROOT/'build/q4t',ROOT/'build/q4t.release.json',
                  BASE/'scope.json',BASE/'build-source-v2.json',BASE/'build-result-v2.json',BASE/'build-b.log',
                  OLD/'analysis.json',OLD/'acceptance-v2/frozen-plan.json'])
    for folder in ['tools/mtp_suffix_causal','tools/mtp_suffix_zero_hit','tools/mtp_suffix','tools/mtp_suffix_admission','tools/mtp_suffix_numeric','tools/evalscope']:
        paths.update((ROOT/folder).glob('*.py'))
    for name in ['AGENTS.md','docs/EVALUATION.md','docs/PERFORMANCE_DECISION_PROTOCOL.md','docs/MTP_SUFFIX_CAUSAL_PLAN_2026-10-26.md']:
        paths.add(ROOT/name)
    for name,h in old['files'].items():
        p=Path(name)
        if p.is_relative_to(Path(old['model'])) or p.parent==Path(old['artifact']):
            if sha(p)!=h: raise RuntimeError('immutable model/subset changed: '+name)
            paths.add(p)
    linked=subprocess.check_output(['ldd',plan['candidate']],text=True)
    (stage/'ldd.txt').write_text(linked);paths.add(stage/'ldd.txt')
    for name in re.findall(r'(/[^\s()]+)',linked):
        p=Path(name)
        if p.is_file(): paths.add(p.resolve())
    for pattern in ['/opt/nvidia/l4t-gpu-libs/openrm/libcuda.so*','/usr/lib/aarch64-linux-gnu/nvidia/libcuda.so*','/usr/lib/aarch64-linux-gnu/nvidia/libnvidia-ml.so*']:
        import glob
        paths.update(Path(p).resolve() for p in glob.glob(pattern) if Path(p).is_file())
    paths.update([Path(sys.executable).resolve(),ROOT/'tools/evalscope/.venv/bin/evalscope'])
    site=ROOT/'tools/evalscope/.venv/lib'
    for folder in site.glob('python*/site-packages/evalscope'):
        paths.update(folder.rglob('*.py'))
    snapshot=stage/'source-snapshot';snapshot.mkdir()
    for p in sorted(paths):
        plan['files'][str(p)]=sha(p)
        if p.is_relative_to(ROOT) and '.q4t-work' not in p.relative_to(ROOT).parts and '.venv' not in p.parts:
            target=snapshot/p.relative_to(ROOT);target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,target)
    plan['source_snapshot']=str(snapshot)
    validate(plan)
    save(stage/'frozen-plan.json',plan)
    print(json.dumps(dict(plan=str(stage/'frozen-plan.json'),sha256=sha(stage/'frozen-plan.json'),files=len(paths),requests=60,starts=6)))

if __name__=='__main__': main()
