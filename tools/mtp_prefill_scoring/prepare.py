"""Freeze mainline-only sources, existing inputs and one bounded acceptance."""
import argparse
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('scoring_driver',
    Path(__file__).with_name('driver.py'))
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)
sha, save = driver.legacy.sha, driver.save


def prepare(original, build):
    base = ROOT / '.q4t-work/scoring-acceptance'
    base.mkdir(parents=True)
    frozen = base / 'frozen'
    frozen.mkdir()
    if subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT):
        raise RuntimeError('commit the isolated source before freezing')
    old_base = original / '.q4t-work/mtp-prefill-adoption-20261104'
    protected = json.loads((old_base / 'protected-worktree.json').read_text())
    for name, digest in protected['worktree_files'].items():
        if sha(original / name) != digest:
            raise RuntimeError('client WIP changed: ' + name)
    for log in ('configure.log', 'build.log'):
        text = (build.parent / log).read_text()
        if re.search(r'\bwarning:', text, re.I):
            raise RuntimeError('build warnings; preserve logs')
    baseline = json.loads((old_base / 'baseline.json').read_text())
    if sha(original / 'build/q4t') != baseline['binary'] or sha(
            original / 'build/q4t.release.json') != baseline['sidecar']:
        raise RuntimeError('default identity changed')
    for source, target in [
            (build / 'q4t', 'q4t'),
            (build / 'q4t_prefill_scoring_contract', 'state-contract'),
            (build / 'CMakeCache.txt', 'CMakeCache.txt'),
            (old_base / 'parent/q4t', 'parent'),
            (old_base / 'parent/q4t.release.json', 'parent.release.json')]:
        shutil.copy2(source, frozen / target)
        if sha(source) != sha(frozen / target):
            raise RuntimeError('frozen copy changed')
    old_path = original / '.q4t-work/mtp-prefill-release-20261029/phase-a/plan.json'
    old = json.loads(old_path.read_text())

    def material(source, name):
        source = Path(source)
        if sha(source) != old['files'][str(source)]:
            raise RuntimeError('historical input changed: ' + str(source))
        dest = frozen / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        return str(dest)

    protection = copy.deepcopy(old['protection'])
    for case in protection:
        case['path'] = material(case['path'], case['id'] + '.jsonl')
    quality = copy.deepcopy(old['quality_case'])
    quality['path'] = material(quality['path'], 'quality.jsonl')
    repeats = copy.deepcopy(old['suffix_cases'])
    for case in repeats:
        case['path'] = material(case['path'], case['id'] + '.jsonl')
        case['tokens'] = 256
    release = json.loads((frozen / 'parent.release.json').read_text())
    plan_path = base / 'plan.json'
    plan = dict(stage='SCORING_ADOPTION', output=str(base / 'run'),
        execution_tools_root=str(original), candidate=str(frozen / 'q4t'),
        parent=str(frozen / 'parent'), state_contract=str(frozen / 'state-contract'),
        candidate_sha256=sha(frozen / 'q4t'), parent_sha256=sha(frozen / 'parent'),
        state_contract_sha256=sha(frozen / 'state-contract'),
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'],
            cwd=ROOT, text=True).strip(), baseline_commit=protected['base_commit'],
        research_commit='c9d4475d1169fa653a092f22f970253fd4bcf043',
        model=old['model'], artifact=release['subset_artifact'],
        model_file_metadata=old['model_file_metadata'], quality=old['quality'],
        protection=protection, quality_case=quality, suffix_cases=repeats,
        development_prompt=material(old['development_prompt'], 'development.txt'),
        short_prompt=old['short_prompt'], port=8762, http_requests=198,
        http_loads=10, maximum_gpu_contracts=2, stage_timeout_seconds=10800,
        task_deadline_unix=time.time() + 10800, direct_contract_timeout_seconds=1200,
        resource_sample_max_age_seconds=10,
        telemetry_sources=driver.reuse.telemetry.discover_sources(), steps=[], files={})

    def step(kind, label, **values):
        name = f'{len(plan["steps"]):02d}-' + label
        record = dict(name=name, kind=kind, http_requests=0, http_loads=0,
            timeout=600, **values)
        record['command'] = [sys.executable, '-B',
            str(ROOT / 'tools/mtp_prefill_scoring/driver.py'), '--plan',
            str(plan_path), '--custom-step', name]
        plan['steps'].append(record)
        return record

    for mode, head, layout, arms in [
            ('agent-full', 'full', 'agent', ['candidate', 'parent']),
            ('agent-subset', 'subset', 'agent', ['parent', 'candidate']),
            ('single-full', 'full', 'single', ['parent', 'candidate']),
            ('single-plain', 'plain', 'single', ['candidate', 'parent'])]:
        for arm in arms:
            cases = copy.deepcopy(protection)
            if layout == 'agent':
                if arm == 'candidate':
                    cases.append(copy.deepcopy(quality))
                cases.extend(copy.deepcopy(repeats))
            s = step('matrix', mode + '-' + arm, mode=mode, head=head,
                layout=layout, arm=arm, suffix=layout == 'agent',
                binary=plan[arm], cases=cases)
            s.update(http_requests=sum(c['count'] for c in cases), http_loads=1,
                     timeout=2400)
        step('gate', mode + '-gate', mode=mode)
    for head in ('full', 'subset'):
        step('contract', head + '-state', head=head)
    for head in ('full', 'subset'):
        s = step('lifecycle', head + '-cache', head=head, layout='agent',
                 suffix=True, binary=plan['candidate'])
        s.update(http_requests=12, http_loads=1, timeout=900)
    tracked = subprocess.check_output(['git', 'ls-files'], cwd=ROOT,
                                     text=True).splitlines()
    inputs = [ROOT / n for n in tracked if n.startswith(('src/', 'include/',
        'tools/', 'cmake/')) or n in ('CMakeLists.txt',
        'docs/MTP_PREFILL_ADOPTION_PLAN_2026-11-04.md')]
    inputs += [p for p in frozen.rglob('*') if p.is_file()]
    inputs += [Path(plan['artifact']) / n for n in
               ('head.bf16', 'token_ids.bin', 'manifest.json')]
    inputs += [old_path, old_base / 'protected-worktree.json',
               original / 'tools/evalscope/.venv/bin/evalscope']
    plan['files'] = {str(p): sha(p) for p in inputs}
    driver.validate(plan)
    driver.legacy.verify_files(plan)
    save(plan_path, plan)
    expected = json.loads((old_base / 'parent/runtime-source.json').read_text())
    changed = [n for n, digest in expected.items() if sha(ROOT / n) != digest]
    if set(changed) != {'CMakeLists.txt', 'include/q4t/model/full_attention.h',
            'include/q4t/mtp/mtp.h', 'src/model/full_attention.cu', 'src/mtp/mtp.cu'}:
        raise RuntimeError('unreviewed runtime changes: ' + repr(changed))
    save(base / 'dependency-review.json', dict(passed=True, changed=changed,
        unchanged=len(expected) - len(changed), protected_client_files=10,
        runtime_manifest={n: sha(ROOT / n) for n in expected},
        source_commit=plan['source_commit'], model_or_reference_written=False))
    print(json.dumps(dict(plan=str(plan_path), candidate=plan['candidate_sha256'],
        requests=plan['http_requests'], starts=plan['http_loads'],
        frozen_files=len(plan['files']), runtime_changes=changed)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--original', required=True, type=Path)
    parser.add_argument('--build', required=True, type=Path)
    args = parser.parse_args()
    prepare(args.original.resolve(), args.build.resolve())
