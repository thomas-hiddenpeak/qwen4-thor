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
BASE = ROOT/'.q4t-work/session-preallocate-20261029'


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    from transformers import AutoTokenizer
    spec = importlib.util.spec_from_file_location('allocate_run', ROOT/'tools/session_preallocate/run_stage.py')
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    oldbase = ROOT/'.q4t-work/mtp-prefill-release-20261029'
    old = json.loads((oldbase/'phase-c/plan.json').read_text())
    frozen = BASE/'frozen'
    frozen.mkdir()
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
    prompt = json.loads(Path(scenario['cases'][0]['path']).read_text().splitlines()[0])['prompt']
    prompt += reference['rows'][0]['text'] + runner.seed.turn('Reply with exactly 44.')
    path = frozen/'resume.jsonl'
    path.write_text(json.dumps({'prompt': prompt})+'\n')
    tokenizer = AutoTokenizer.from_pretrained(old['model'], local_files_only=True)
    resume = dict(path=str(path), tokens=16,
                  input_tokens=len(tokenizer.encode(prompt, add_special_tokens=False)),
                  prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest())
    budget = json.loads((BASE/'budget.json').read_text())
    deadline = datetime.datetime.fromisoformat(budget['deadline_utc'].replace('Z', '+00:00')).timestamp()
    plan = dict(stage='SESSION_PREALLOCATE', output=str(BASE/'run'),
        candidate=str(frozen/'q4t'), candidate_sha256=sha(frozen/'q4t'),
        storage_contract=str(frozen/'storage_contract'),
        parent=str(frozen/'installed-parent'), parent_sha256=sha(frozen/'installed-parent'),
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
        model=old['model'], artifact=old['artifact'],
        model_file_metadata=old['model_file_metadata'],
        http_requests=40, http_loads=4, maximum_http_requests=40,
        maximum_starts=4, maximum_gpu_contracts=0, stage_timeout_seconds=int(deadline-time.time()),
        task_deadline_unix=deadline, direct_contract_timeout_seconds=90,
        resource_sample_interval_seconds=1, resource_sample_max_age_seconds=10,
        telemetry_sources=runner.reuse.telemetry.discover_sources(), port=8762,
        scenario=scenario, references=reference['rows'], resume_case=resume,
        budget=budget, steps=[], files={},
        acceptance={'main_private_save_median_improvement_seconds_per_head': 1,
            'each_shared_mode_transaction_must_not_slow': True,
            'candidate_public_on_off_net_positive_per_head': True,
            'same_fixed_output_and_exact_private_restore': True,
            'full_storage_contract_required': True,
            'statistical_noninferiority_claim': False},
        reuse=['A112 five-tier/quality uncached', 'S32 suffix repeated uncached',
               'unchanged scoring and complete target/draft state contracts'],
        limits=['Original formal C NO_GO remains; no invented missing evidence.',
                'One pair per head; fixed order and SSD state remain confounded.'])
    for number, (head, preallocate, order) in enumerate([
            ('full', 0, [False, True]), ('full', 1, [True, False]),
            ('subset', 1, [False, True]), ('subset', 0, [True, False])]):
        name = f'{number:02d}-{head}-preallocate-{preallocate}'
        plan['steps'].append(dict(name=name, head=head, preallocate=preallocate,
            shared_order=order, suffix=True, kind='block', http_loads=1,
            http_requests=10, timeout=900,
            command=[sys.executable, '-B', str(ROOT/'tools/session_preallocate/run_stage.py'),
                     '--plan', str(BASE/'plan.json'), '--custom-step', name]))
    plan['steps'].append(dict(name='04-cpu-contract', preallocate=1, kind='contract',
        http_loads=0, http_requests=0, timeout=90,
        command=[sys.executable, '-B', str(ROOT/'tools/session_preallocate/run_stage.py'),
                 '--plan', str(BASE/'plan.json'), '--custom-step', '04-cpu-contract']))
    files = [p for group in ['src', 'include'] for p in (ROOT/group).rglob('*') if p.is_file()]
    files += [p for p in (ROOT/'tools').rglob('*.py') if '.venv' not in p.parts and '__pycache__' not in p.parts]
    files += list(frozen.iterdir()) + [BASE/'budget.json', ROOT/'CMakeLists.txt',
        ROOT/'docs/SESSION_PREALLOCATE_PLAN_2026-10-29.md',
        ROOT/'tools/session_preallocate/storage_contract.cpp']
    files += [Path(old['artifact'])/n for n in ['head.bf16', 'token_ids.bin', 'manifest.json']]
    plan['files'] = {str(p):sha(p) for p in files}
    runner.save(BASE/'plan.json', plan)
    (frozen/'source.diff').write_bytes(subprocess.check_output(['git', 'diff', 'HEAD'], cwd=ROOT))
    runner.save(BASE/'identity.json', dict(candidate_sha256=plan['candidate_sha256'],
        plan_sha256=sha(BASE/'plan.json'), files=len(plan['files']), deadline=deadline,
        resume_input_tokens=resume['input_tokens']))
    print((BASE/'identity.json').read_text())


if __name__ == '__main__':
    main()
