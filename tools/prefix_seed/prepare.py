"""Freeze existing inputs, new program and bounded seed-validation plans."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('seed_driver',ROOT/'tools/prefix_seed/run_stage.py')
driver=importlib.util.module_from_spec(spec);spec.loader.exec_module(driver)
reuse,save=driver.reuse,driver.save
BASE=ROOT/'.q4t-work/prefix-seed-20261028'
PREVIOUS=ROOT/'.q4t-work/prefix-cache-20261028'
def sha(path):
    with Path(path).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()


def main():
    old=json.loads((PREVIOUS/'frozen/common.json').read_text())
    frozen=BASE/'frozen';frozen.mkdir();programs=frozen/'programs';programs.mkdir()
    for name in ['q4t','q4t_prefix_seed_contract','q4t_prefix_state_contract']:
        shutil.copy2(BASE/'build'/name,programs/name)
    shutil.copy2(ROOT/'build/q4t',programs/'parent')
    shutil.copy2(ROOT/'build/q4t.release.json',programs/'parent.release.json')
    if sha(programs/'parent')!=old['parent_sha256']:raise ValueError('delivered identity changed')
    def material(path):
        source=Path(path);dest=frozen/source.relative_to(PREVIOUS/'frozen')
        dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,dest)
        if sha(source)!=sha(dest):raise ValueError('input copy')
        return str(dest)
    quality=dict(old['quality_case'],path=material(old['quality_case']['path']))
    scenarios=copy.deepcopy(old['scenarios'])
    for s in scenarios:
        for c in s['cases']:c['path']=material(c['path'])
    protection=copy.deepcopy(old['protection'])
    for c in protection:c['path']=material(c['path'])
    common=dict(candidate=str(programs/'q4t'),candidate_sha256=sha(programs/'q4t'),
        parent=str(programs/'parent'),parent_sha256=sha(programs/'parent'),
        storage_contract=str(programs/'q4t_prefix_seed_contract'),
        state_contract=str(programs/'q4t_prefix_state_contract'),model=old['model'],artifact=old['artifact'],
        model_file_metadata=old['model_file_metadata'],quality=old['quality'],quality_case=quality,
        short_prompt=old['short_prompt'],development_prompt=material(old['development_prompt']),
        scenarios=scenarios,protection=protection,port=8762,
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        task_deadline_unix=time.time()+21600,maximum_http_requests=300,maximum_http_starts=20,
        maximum_gpu_contracts=2,maximum_rejected_http=12,telemetry_sources=reuse.telemetry.discover_sources(),
        resource_sample_interval_seconds=1,resource_sample_max_age_seconds=10,direct_contract_timeout_seconds=1800,
        required_gates=[],files={})
    shutil.copy2(BASE/'build/CMakeCache.txt',frozen/'CMakeCache.txt')
    files=[p for group in ['src','include'] for p in (ROOT/group).rglob('*') if p.is_file()]
    groups=['prefix_seed','prefix_cache','session_cache','mtp_suffix_multi','mtp_suffix_lazy','mtp_suffix_zero_hit','mtp_suffix_admission','mtp_suffix','evalscope']
    files += [p for group in groups for p in (ROOT/'tools'/group).glob('*.py')]
    files += [p for group in ['prefix_seed','prefix_cache','session_cache'] for p in (ROOT/'tools'/group).glob('*.cpp')]
    files += [ROOT/'CMakeLists.txt',ROOT/'docs/PREFIX_SEED_PLAN_2026-10-28.md',ROOT/'tests/server_chat_contract_test.cpp',ROOT/'tests/test_main.cpp']
    files += [p for p in frozen.rglob('*') if p.is_file()]
    files += [Path(common['artifact'])/n for n in ['head.bf16','token_ids.bin','manifest.json']]
    common['files']={str(p):sha(p) for p in files}
    save(frozen/'common.json',common)
    (frozen/'source.diff').write_bytes(subprocess.check_output(['git','diff','HEAD'],cwd=ROOT))
    arm_c=[('full',False,0),('full',True,0),('subset',True,0),('subset',False,0),
           ('full',True,1),('full',False,1),('subset',False,1),('subset',True,1)]
    phases=[('A',3600,[('full',True,0),('subset',True,0)],23),
        ('B',1800,[('full',True,0),('subset',True,0)],0),('C',12000,arm_c,16),
        ('D',9000,[('full',False,0),('full',True,0),('subset',True,0),('subset',False,0),('plain',False,0),('plain',True,0)],15)]
    for phase,timeout,arms,requests in phases:
        directory=BASE/('phase-'+phase.lower());directory.mkdir();path=directory/'plan.json'
        plan=copy.deepcopy(common);plan.update(stage='PREFIX_'+phase,output=str(directory/'run'),steps=[],
            stage_timeout_seconds=timeout,http_requests=len(arms)*requests,http_loads=0 if phase=='B' else len(arms),
            rejected_http_requests=8 if phase=='A' else 0)
        if phase!='A':plan['required_gates']=[str(BASE/('gate-'+{'B':'a','C':'b','D':'c'}[phase]+'.json'))]
        for i,(head,on,block) in enumerate(arms):
            name=f'{i:02d}-{head}-'+('on' if on else 'off')
            candidate=phase!='D' or on
            step=dict(name=name,head=head,shared=on and head!='plain',session=head!='plain',block=block,
                binary=common['candidate'] if candidate else common['parent'],kind='contract' if phase=='B' else 'block',
                http_requests=requests,http_loads=0 if phase=='B' else 1,timeout=900 if phase=='B' else (1800 if phase in ['A','D'] else 1800))
            if phase=='B':
                step['command']=[common['state_contract'],common['model'],str(directory/('cache-'+head))]
                if head=='subset':step['command'].append(common['artifact'])
            else:step['command']=[sys.executable,'-B',str(ROOT/'tools/prefix_seed/run_stage.py'),'--plan',str(path),'--custom-step',name]
            plan['steps'].append(step)
        save(path,plan)
    identity=dict(candidate=common['candidate_sha256'],parent=common['parent_sha256'],source_commit=common['source_commit'],
        deadline=common['task_deadline_unix'],formal_generated_http=264,formal_rejected_http=8,formal_services=16,
        direct_fixtures=2,health_max_http=4,health_max_services=2,total_http_limit=300,total_service_limit=20,
        cpu_seed_contract_sha256=sha(programs/'q4t_prefix_seed_contract'),gpu_contract_sha256=sha(programs/'q4t_prefix_state_contract'),
        source_bindings=len(common['files']),io_precondition_bytes=64*1024**3,previous_inputs=str(PREVIOUS/'identity.json'))
    save(BASE/'identity.json',identity);print(json.dumps(identity,indent=2))


if __name__=='__main__':main()
