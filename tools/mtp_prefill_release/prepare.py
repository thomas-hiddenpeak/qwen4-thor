"""Freeze one existing binary, inherited materials, and a 5.5-hour budget."""
import copy
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

spec = importlib.util.spec_from_file_location('release_prepare_driver',
    Path(__file__).with_name('run_stage.py'))
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)

ROOT = driver.ROOT
BASE = ROOT/'.q4t-work/mtp-prefill-release-20261029'
OLD = ROOT/'.q4t-work/mtp-prefill-scoring-20261029'
SEED = ROOT/'.q4t-work/prefix-seed-20261028'
save = driver.save


def sha(path):
    return driver.legacy.sha(Path(path))


def main():
    prior = json.loads((SEED/'frozen/common.json').read_text())
    research = json.loads((OLD/'review.json').read_text())
    if research['decision'] != 'RESEARCH_GO_REQUIRES_RELEASE_VALIDATION':
        raise ValueError('research prerequisite')
    frozen = BASE/'frozen'
    frozen.mkdir(parents=True)
    for source, name in [(ROOT/'build/q4t', 'parent'),
        (ROOT/'build/q4t.release.json', 'parent.release.json'),
        (OLD/'frozen/q4t', 'q4t'), (OLD/'frozen/CMakeCache.txt', 'CMakeCache.txt')]:
        shutil.copy2(source, frozen/name)
    if (sha(frozen/'q4t') != research['candidate_sha256'] or
            sha(frozen/'parent') != research['parent_sha256']):
        raise ValueError('candidate/default changed')
    measured = json.loads((OLD/'plan.json').read_text())
    for path, digest in measured['files'].items():
        if Path(path).is_relative_to(ROOT/'src') or Path(path).is_relative_to(ROOT/'include') or Path(path) == ROOT/'CMakeLists.txt':
            if sha(path) != digest:
                raise ValueError('measured runtime changed: '+path)
    def material(path, name):
        destination = frozen/name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        if sha(path) != sha(destination):
            raise ValueError('material copy')
        return str(destination)
    protection = copy.deepcopy(prior['protection'])
    for case in protection:
        case['path'] = material(case['path'], 'protection/'+case['id']+'.jsonl')
    quality_case = dict(prior['quality_case'], path=material(prior['quality_case']['path'], 'quality.jsonl'))
    scenarios = copy.deepcopy(prior['scenarios'])
    for scenario in scenarios:
        for i, case in enumerate(scenario['cases']):
            case['path'] = material(case['path'], f"shared/{scenario['id']}-{i}.jsonl")
    agent = json.loads((ROOT/'.q4t-work/suffix-agent-20261027/acceptance/frozen-plan.json').read_text())
    repeats = [copy.deepcopy(c) for c in agent['steps'][4]['cases']
               if c['id'] in ['repeat-code', 'repeat-text']]
    if len(repeats) != 2:
        raise ValueError('fixed repeat materials')
    for case in repeats:
        case['path'] = material(case['path'], 'suffix/'+case['id']+'.jsonl')
    references = {}
    for head, folder in [('full', '01-full-on'), ('subset', '02-subset-on'), ('plain', '05-plain-on')]:
        results = json.loads((SEED/'phase-d/run'/folder/'results.json').read_text())
        references[head] = {case['id']: {k: case['rows'][0][k]
            for k in ['input_tokens', 'prompt_sha256', 'output_sha256']} for case in results}
    common = dict(candidate=str(frozen/'q4t'), parent=str(frozen/'parent'),
        candidate_sha256=sha(frozen/'q4t'), parent_sha256=sha(frozen/'parent'),
        runtime_source_commit=measured['source_commit'],
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
        model=prior['model'], artifact=prior['artifact'], model_file_metadata=prior['model_file_metadata'],
        quality=prior['quality'], quality_case=quality_case, protection=protection,
        references=references, scenarios=scenarios, suffix_cases=repeats,
        development_prompt=material(prior['development_prompt'], 'development.txt'),
        short_prompt=prior['short_prompt'], port=8762, maximum_http_requests=302,
        maximum_starts=22, maximum_gpu_contracts=0, task_deadline_unix=time.time()+19800,
        direct_contract_timeout_seconds=0, resource_sample_max_age_seconds=10,
        telemetry_sources=driver.reuse.telemetry.discover_sources(), files={})
    files = [p for group in ['src', 'include', 'tools'] for p in (ROOT/group).rglob('*')
             if p.is_file() and '.venv' not in p.parts and p.suffix in ['.cpp', '.cu', '.h', '.py']]
    files += [ROOT/'CMakeLists.txt', ROOT/'docs/MTP_PREFILL_RELEASE_PLAN_2026-10-29.md',
        OLD/'review.json', OLD/'plan.json', OLD/'archive-receipt.json']
    files += [p for p in frozen.rglob('*') if p.is_file()]
    files += [Path(common['artifact'])/name for name in ['head.bf16', 'token_ids.bin', 'manifest.json']]
    common['files'] = {str(p): sha(p) for p in files}
    save(frozen/'common.json', common)
    for phase, seconds, requests, starts in [('A', 7200, 112, 6), ('B', 1800, 24, 2),
        ('S', 3600, 32, 4), ('C', 12000, 128, 8), ('H', 900, 6, 2)]:
        folder = BASE/('phase-'+phase.lower())
        folder.mkdir()
        path = folder/'plan.json'
        plan = dict(copy.deepcopy(common), stage='PREFILL_RELEASE_'+phase,
            output=str(folder/'run'), stage_timeout_seconds=seconds,
            http_requests=requests, http_loads=starts, steps=[], required_gates=[])
        previous = {'B': 'a', 'S': 'b', 'C': 's'}
        if phase in previous:
            plan['required_gates'] = [str(BASE/('gate-'+previous[phase]+'.json'))]
        if phase == 'H':
            plan['required_gates'] = [str(BASE/'final-decision.json')]
        def service(head, candidate=True, suffix=True, shared=True, block=0, cases=None, count=0):
            name = f'{len(plan["steps"]):02d}-{head}-' + (
                ('candidate' if candidate else 'parent') if phase == 'A' else
                ('on' if (suffix if phase == 'S' else shared) else 'off'))
            step = dict(name=name, head=head, arm='candidate' if candidate else 'parent',
                kind='block', layout='agent', block=block, suffix=suffix, shared=shared,
                suffix_performance=phase == 'S', session=head != 'plain',
                binary=str(ROOT/'build/q4t') if phase == 'H' else common['candidate' if candidate else 'parent'],
                http_requests=count, http_loads=1, cases=cases or [],
                timeout=1800 if phase in ['A', 'C'] else (1000 if phase == 'S' else 600),
                command=[sys.executable, '-B', str(ROOT/'tools/mtp_prefill_release/run_stage.py'),
                         '--plan', str(path), '--custom-step', name])
            plan['steps'].append(step)
        if phase == 'A':
            for head, candidate in [('full', True), ('full', False), ('subset', False),
                                    ('subset', True), ('plain', False), ('plain', True)]:
                cases = protection + ([quality_case] if candidate and head != 'plain' else [])
                service(head, candidate, head != 'plain', cases=cases, count=sum(c['count'] for c in cases))
        elif phase in ['B', 'H']:
            for head in ['full', 'subset']:
                service(head, count=12 if phase == 'B' else 3)
        elif phase == 'S':
            for head, on in [('full', False), ('full', True), ('subset', True), ('subset', False)]:
                service(head, suffix=on, block=1, cases=repeats, count=8)
        else:
            for i, (head, on, block) in enumerate([
                ('full', False, 0), ('full', True, 0), ('subset', True, 0), ('subset', False, 0),
                ('full', True, 1), ('full', False, 1), ('subset', False, 1), ('subset', True, 1)]):
                if i == 4:
                    name = '04-first-block-gate'
                    plan['steps'].append(dict(name=name, kind='analysis', http_requests=0,
                        http_loads=0, timeout=30, command=[sys.executable, '-B',
                        str(ROOT/'tools/mtp_prefill_release/run_stage.py'), '--plan', str(path), '--custom-step', name]))
                service(head, shared=on, block=block, count=16)
        # Structural validation only; prerequisites are intentionally future files.
        assert sum(s['http_requests'] for s in plan['steps']) == requests
        assert sum(s['http_loads'] for s in plan['steps']) == starts
        save(path, plan)
    identity = dict(candidate_sha256=common['candidate_sha256'], parent_sha256=common['parent_sha256'],
        runtime_source_commit=common['runtime_source_commit'], source_commit=common['source_commit'],
        deadline=common['task_deadline_unix'], maximum_http=302, maximum_services=22,
        new_gpu_fixtures=0, reused_direct_fixtures=2, files=len(common['files']))
    save(BASE/'identity.json', identity)
    print(json.dumps(identity, indent=2))


if __name__ == '__main__':
    main()
