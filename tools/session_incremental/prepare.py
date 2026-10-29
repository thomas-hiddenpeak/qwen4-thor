"""Freeze the isolated main-based delivery. Never rerun an expired plan."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT/'.q4t-work/session-incremental-delivery-20261029'


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    spec = importlib.util.spec_from_file_location('incremental_run', Path(__file__).with_name('run_stage.py'))
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    old = json.loads((ROOT/'.q4t-work/session-incremental-20261029/plan.json').read_text())
    frozen = BASE/'frozen'
    frozen.mkdir()
    for source, name in [(BASE/'build/q4t','q4t'), (BASE/'build/CMakeCache.txt','CMakeCache.txt'),
            (BASE/'storage_contract','storage_contract'), (ROOT/'build/q4t','installed-parent'),
            (ROOT/'build/q4t.release.json','installed-parent.release.json')]:
        shutil.copy2(source, frozen/name)
    plan = {k:old[k] for k in ['model','artifact','model_file_metadata','scenario','references',
        'resume_case','resume_second_case','acceptance']}
    for i, case in enumerate([*plan['scenario']['cases'],plan['resume_case'],plan['resume_second_case']]):
        path = frozen/f'case-{i}.jsonl'
        shutil.copy2(case['path'],path)
        case['path'] = str(path)
    prior = json.loads((ROOT/'.q4t-work/prefix-seed-20261028/phase-a/plan.json').read_text())
    shutil.copy2(prior['development_prompt'], frozen/'health-prompt.txt')
    start = time.time()
    budget = dict(start_unix=start, deadline_unix=start+5400, seconds=5400,
        http=54, starts=6, measurement_http=48, measurement_starts=4,
        health_http=6, health_starts=2, candidates=1, cpu_programs=1,
        cpu_write_limit=512<<20, write_limit=224<<30)
    runner.save(BASE/'budget.json',budget)
    plan.update(stage='INCREMENTAL_DELIVERY', output=str(BASE/'run'), lease_directory=str(BASE/'lease'),
        candidate=str(frozen/'q4t'), candidate_sha256=sha(frozen/'q4t'),
        storage_contract=str(frozen/'storage_contract'), parent=str(frozen/'installed-parent'),
        parent_sha256=sha(frozen/'installed-parent'), source_commit=subprocess.check_output(
            ['git','rev-parse','HEAD'],text=True).strip(),
        http_requests=48, http_loads=4, maximum_http_requests=54, maximum_starts=6,
        maximum_gpu_contracts=0, stage_timeout_seconds=5400, task_deadline_unix=budget['deadline_unix'],
        direct_contract_timeout_seconds=180, resource_sample_interval_seconds=1,
        resource_sample_max_age_seconds=10, telemetry_sources=runner.reuse.telemetry.discover_sources(),
        port=8762, budget=budget, steps=[], files={},
        development_prompt=str(frozen/'health-prompt.txt'),
        reuse=['main installed 6f37: uncached five-tier/quality, suffix and model math/state',
               'unchanged public prefix policy / complete session range contracts'],
        limits=['Research scoring and rejected preallocation/pipeline not included.',
                'One pair per head, descriptive not statistical noninferiority.'])
    for i,(head,enabled,order) in enumerate([('full',0,[False,True]),('full',1,[True,False]),
            ('subset',1,[False,True]),('subset',0,[True,False])]):
        name=f'{i:02d}-{head}-incremental-{enabled}'
        plan['steps'].append(dict(name=name,head=head,incremental=enabled,unset_incremental=i==0,
            shared_order=order,suffix=True,kind='block',http_loads=1,http_requests=12,timeout=1500,
            command=[sys.executable,'-B',str(ROOT/'tools/session_incremental/run_stage.py'),
                     '--plan',str(BASE/'plan.json'),'--custom-step',name]))
    plan['steps'].insert(1,dict(name='04-cpu-contract',incremental=0,kind='contract',http_loads=0,
        http_requests=0,timeout=180,command=[sys.executable,'-B',str(ROOT/'tools/session_incremental/run_stage.py'),
            '--plan',str(BASE/'plan.json'),'--custom-step','04-cpu-contract']))
    files=[p for group in ['src','include'] for p in (ROOT/group).rglob('*') if p.is_file()]
    files += [p for p in (ROOT/'tools').rglob('*.py') if '.venv' not in p.parts and '__pycache__' not in p.parts]
    files += list((ROOT/'tools/session_incremental').glob('*.cpp'))
    files += list(frozen.iterdir())+[BASE/'budget.json',BASE/'dependency-review.json',ROOT/'CMakeLists.txt',
        ROOT/'docs/SESSION_INCREMENTAL_DELIVERY_PLAN_2026-10-29.md',BASE/'configure.log',BASE/'build.log',BASE/'cpu-build.log']
    files += [Path(plan['artifact'])/n for n in ['head.bf16','token_ids.bin','manifest.json']]
    plan['files']={str(p):sha(p) for p in files}
    runner.validate(plan)
    runner.save(BASE/'plan.json',plan)
    runner.save(BASE/'identity.json',dict(candidate_sha256=plan['candidate_sha256'],
        plan_sha256=sha(BASE/'plan.json'),files=len(plan['files']),budget=budget))
    print((BASE/'identity.json').read_text())


if __name__ == '__main__':
    main()
