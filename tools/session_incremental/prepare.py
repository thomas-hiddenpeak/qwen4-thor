"""Freeze one candidate and the already authorized bounded request ledger."""
import datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT/'.q4t-work/session-incremental-20261029'


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    spec = importlib.util.spec_from_file_location('allocate_run', ROOT/'tools/session_incremental/run_stage.py')
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    oldbase = ROOT/'.q4t-work/mtp-prefill-release-20261029'
    old = json.loads((oldbase/'phase-c/plan.json').read_text())
    frozen = BASE/'frozen'
    frozen.mkdir(exist_ok=True)
    build = ROOT/'.q4t-work/mtp-prefill-scoring-20261029/build'
    for source, name in [(build/'q4t', 'q4t'), (build/'CMakeCache.txt', 'CMakeCache.txt'),
                         (BASE/'storage_contract', 'storage_contract'),
                         (ROOT/'build/q4t', 'installed-parent'),
                         (ROOT/'build/q4t.release.json', 'installed-parent.release.json')]:
        shutil.copy2(source, frozen/name)
    scenario = next(c for c in old['scenarios'] if c['id'] == 'repeat-code-8192')
    reference = json.loads((oldbase/'phase-c/run/00-full-off/repeat-code-8192/result.json').read_text())
    for i, case in enumerate(scenario['cases']):
        target = frozen/f'case-{i}.jsonl'
        shutil.copy2(case['path'], target)
        case['path'] = str(target)
    previous = json.loads((ROOT/'.q4t-work/session-preallocate-20261029/plan.json').read_text())
    resume = previous['resume_case']
    path = frozen/'resume.jsonl'
    shutil.copy2(resume['path'], path)
    resume['path'] = str(path)
    prompt = json.loads(path.read_text())['prompt']
    prompt += '44' + runner.seed.turn('Reply with exactly 44.')
    second_path = frozen/'resume-second.jsonl'
    second_path.write_text(json.dumps({'prompt': prompt})+'\n')
    script = """import json,sys
from transformers import AutoTokenizer
p=json.load(open(sys.argv[2]))['prompt']
t=AutoTokenizer.from_pretrained(sys.argv[1],local_files_only=True)
print(len(t.encode(p,add_special_tokens=False)))
"""
    tokens = int(subprocess.check_output([
        str(ROOT/'tools/evalscope/.venv/bin/python'), '-c', script,
        old['model'], str(second_path)], text=True))
    second = dict(path=str(second_path), tokens=16, input_tokens=tokens,
        prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest())
    budget = json.loads((BASE/'budget.json').read_text())
    deadline = budget['deadline_unix']
    plan = dict(stage='SESSION_INCREMENTAL', output=str(BASE/'run'),
        lease_directory=str(BASE/'lease'),
        candidate=str(frozen/'q4t'), candidate_sha256=sha(frozen/'q4t'),
        storage_contract=str(frozen/'storage_contract'),
        parent=str(frozen/'installed-parent'), parent_sha256=sha(frozen/'installed-parent'),
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
        model=old['model'], artifact=old['artifact'],
        model_file_metadata=old['model_file_metadata'],
        http_requests=48, http_loads=4, maximum_http_requests=48,
        maximum_starts=4, maximum_gpu_contracts=0, stage_timeout_seconds=int(deadline-time.time()),
        task_deadline_unix=deadline, direct_contract_timeout_seconds=180,
        resource_sample_interval_seconds=1, resource_sample_max_age_seconds=10,
        telemetry_sources=runner.reuse.telemetry.discover_sources(), port=8762,
        scenario=scenario, references=reference['rows'], resume_case=resume, resume_second_case=second,
        budget=budget, steps=[], files={},
        acceptance={'resave_write_reduction_per_head': 0.5,
            'resave_median_improvement_seconds_per_head': 1,
            'aggregate_resumes_must_improve_per_head': True,
            'complete_lifecycle_must_improve_per_head': True,
            'candidate_public_on_off_net_positive_per_head': True,
            'cold_save_bytes_unchanged': True,
            'all_outputs_and_two_restores_exact': True,
            'statistical_noninferiority_claim': False},
        reuse=['A112 five-tier/quality uncached', 'S32 suffix repeated uncached',
               'unchanged scoring and complete target/draft state contracts'],
        limits=['Original formal C NO_GO remains; no invented missing evidence.',
                'One pair per head; fixed order and SSD state remain confounded.'])
    for number, (head, incremental, order) in enumerate([
            ('full', 0, [False, True]), ('full', 1, [True, False]),
            ('subset', 1, [False, True]), ('subset', 0, [True, False])]):
        name = f'{number:02d}-{head}-incremental-{incremental}'
        plan['steps'].append(dict(name=name, head=head, preallocate=0, pipeline=0, incremental=incremental,
            shared_order=order, suffix=True, kind='block', http_loads=1,
            http_requests=12, timeout=1200,
            command=[sys.executable, '-B', str(ROOT/'tools/session_incremental/run_stage.py'),
                     '--plan', str(BASE/'plan.json'), '--custom-step', name]))
    plan['steps'].insert(1, dict(name='04-cpu-contract', preallocate=1, pipeline=0, incremental=0, kind='contract',
        http_loads=0, http_requests=0, timeout=180,
        command=[sys.executable, '-B', str(ROOT/'tools/session_incremental/run_stage.py'),
                 '--plan', str(BASE/'plan.json'), '--custom-step', '04-cpu-contract']))
    (frozen/'source.diff').write_bytes(subprocess.check_output(['git', 'diff', 'HEAD'], cwd=ROOT))
    files = [p for group in ['src', 'include'] for p in (ROOT/group).rglob('*') if p.is_file()]
    files += [p for p in (ROOT/'tools').rglob('*.py') if '.venv' not in p.parts and '__pycache__' not in p.parts]
    files += [ROOT/'tools/session_preallocate/storage_contract.cpp']
    files += list(frozen.iterdir()) + [BASE/'budget.json', ROOT/'CMakeLists.txt',
        ROOT/'docs/SESSION_INCREMENTAL_PLAN_2026-10-29.md',
        ROOT/'tools/session_incremental/storage_contract.cpp']
    files += [Path(old['artifact'])/n for n in ['head.bf16', 'token_ids.bin', 'manifest.json']]
    plan['files'] = {str(p):sha(p) for p in files}
    runner.save(BASE/'plan.json', plan)
    runner.save(BASE/'identity.json', dict(candidate_sha256=plan['candidate_sha256'],
        plan_sha256=sha(BASE/'plan.json'), files=len(plan['files']), deadline=deadline,
        resume_input_tokens=resume['input_tokens']))
    print((BASE/'identity.json').read_text())


if __name__ == '__main__':
    main()
