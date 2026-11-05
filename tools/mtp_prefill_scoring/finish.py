"""Complete only the explicitly authorized missing scoring measurements.

The two interrupted phases remain immutable. Reuse is per completed HTTP case,
never a fabricated successful stage or a splice of an unfinished client group.
"""
import argparse
import copy
import importlib.util
import json
import math
from pathlib import Path
import re
import signal
import subprocess
import sys
import time


def read(path):
    return json.loads(Path(path).read_text())


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def write(path, value):
    with Path(path).open('x') as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False)
        stream.write('\n')


def load(source):
    spec = importlib.util.spec_from_file_location(
        'finish_frozen_driver', source / 'tools/mtp_prefill_scoring/driver.py')
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def checked_cases(plan):
    """Validate every reusable case against its frozen inputs and server end."""
    cases = []
    for step in plan['steps']:
        if step['kind'] != 'matrix':
            continue
        folder = Path(plan['output']) / step['name']
        result = folder / 'results.json'
        if not result.exists():
            continue
        ended = read(folder / 'exit.json')
        require(ended['server'] == 0, 'service did not exit normally')
        partial = step['name'] == '10-single-plain-parent'
        require(ended['http_output_checks_passed'] or partial,
                'unexpected unfinished service')
        for case in read(result):
            original = (Path(case['reused_from']) if case.get('reused_from')
                        else folder / case['id'] / 'result.json')
            require(read(original) == {k: v for k, v in case.items()
                    if not k.startswith('reused_')}, 'case source changed')
            source = original.parents[1]
            definitions = (plan['protection'] + plan['suffix_cases'] +
                           [plan['quality_case']])
            definition = next(c for c in definitions if c['id'] == case['id'])
            require(len(case['rows']) == definition['count'], 'partial case')
            events = [read_line for read_line in
                      map(json.loads, (source / 'events.jsonl').read_text().splitlines())
                      if read_line.get('case') == case['id']]
            boundaries = {e['event']: e['monotonic_seconds'] for e in events}
            require(boundaries['client_after'] > boundaries['client_before'],
                    'missing complete client window')
            ends = [dict(re.findall(r'(\w+)=(\S+)', line)) for line in
                    (source / 'server.log').read_text().splitlines()
                    if line.startswith('[q4t][request] ') and 'event=end ' in line]
            require(len({r['response_id'] for r in case['rows']}) == len(case['rows']),
                    'duplicate response identity')
            for row in case['rows']:
                require(row['success'] and row['stream'] and row['response_id'] and
                        row['requested_max_tokens'] == definition['tokens'] and
                        definition['min'] <= row['input_tokens'] <= definition['max'] and
                        row['timing']['within_client_boundaries'] and
                        all(math.isfinite(row[k]) and row[k] > 0
                            for k in ('latency', 'ttft')), 'invalid HTTP result')
                released = [e for e in ends if e['id'] == row['response_id']]
                require(len(released) == 1 and released[0]['outcome'] == 'success'
                        and released[0]['released'] == '1', 'request not released')
                if case['id'] == 'quality':
                    expected = plan['quality'][row['prompt_sha256']]
                    require(row['text'].strip() == expected['expected'] and
                            row['input_tokens'] == expected['length'] and
                            row['finish'] == ['stop'], 'quality mismatch')
                else:
                    require(row['output_tokens'] == 256 and
                            row['finish'] == ['length'], 'output length/finish')
            cases.append(dict(step=step['name'], mode=step['mode'],
                arm=step['arm'], case=case['id'], requests=len(case['rows']),
                source=str(original), source_sha256=driver.legacy.sha(original),
                partial_service=partial, boundaries=boundaries))
    return cases


def prior_evidence(source):
    path = source / '.q4t-work/scoring-continuation/plan.json'
    plan = read(path)
    driver.legacy.verify_files(plan)
    driver.verify_reuse(plan)
    base = source.parent
    require(read(base / 'archive-receipt.json')['passed'], 'prior archive receipt')
    require(read(base / 'decision.json')['status'] ==
            'NOT_ACCEPTED_INCOMPLETE_MEASUREMENTS', 'prior decision changed')
    for phase in ('scoring-acceptance', 'scoring-continuation'):
        folder = source / '.q4t-work' / phase
        guard, state = read(folder / 'lease/exit.json'), read(folder / 'run/state.json')
        post = read(folder / 'postflight.json')
        summary = read(folder / 'run/telemetry-summary.json')
        require(guard['status'] == 'COMMAND_FAILED' and state['status'] == 'STOPPED',
                'historical termination changed')
        require(not any(post[k] for k in ('monitor_failures', 'cleanup_errors',
                                         'final_monitor_errors')) and
                summary['unflushed_attempts'] == 0 and
                summary['sample_attempts'] == summary['flushed_samples'] and
                summary['sampler_failure'] is None and
                not summary['failures_at_exit'] and
                summary['exit_pending']['active'] == 0, 'invalid historical window')
        for name, digest in read(folder / 'run/files.json').items():
            require(driver.legacy.sha(folder / 'run' / name) == digest,
                    'historical raw evidence changed: ' + name)
    cases = checked_cases(plan)
    require(sum(c['requests'] for c in cases) == 171 and len(cases) == 49,
            '171 complete HTTP results required')
    require({c['case'] for c in cases if c['partial_service']} ==
            {'context-1024', 'context-4096', 'context-8192', 'context-45056'},
            'unfinished 200K client group must not be reused')
    return plan, cases


def prepare(source, path):
    prior, cases = prior_evidence(source)
    path.parent.mkdir()
    reuse_path = path.parent / 'reuse.json'
    write(reuse_path, dict(requests=171, cases=cases,
        original_admissions=181, original_service_starts=9,
        prior_phase_status='STOPPED_NOT_RECLASSIFIED',
        prior_plan=str(source / '.q4t-work/scoring-continuation/plan.json')))
    plan = copy.deepcopy(prior)
    for key in ('reuse_record', 'amendment'):
        plan.pop(key, None)
    steps = [copy.deepcopy(prior['steps'][i]) for i in (10, 11, 12, 13, 14, 15)]
    steps[0]['cases'] = [c for c in steps[0]['cases'] if c['id'] == 'context-204800']
    steps[0]['http_requests'], steps[0]['timeout'] = 3, 720
    steps[1]['kind'], steps[1]['timeout'] = 'combined_gate', 60
    names = ('00-plain-parent-200k', '01-combined-http-gate',
             '02-full-state', '03-subset-state', '04-full-cache', '05-subset-cache')
    for step, name in zip(steps, names):
        step['name'] = name
        step['command'] = [sys.executable, '-B', str(Path(__file__).resolve()),
            'run', '--source-root', str(source), '--plan', str(path),
            '--custom-step', name]
    plan.update(stage='SCORING_FINISH', output=str(path.parent / 'run'), steps=steps,
        http_requests=27, http_loads=3, maximum_gpu_contracts=2,
        stage_timeout_seconds=1200, direct_contract_timeout_seconds=1200,
        completion_reuse=str(reuse_path),
        completion_authorization='User 2026-11-05: 继续吧, following the explicit '
            '20-minute proposal for 3 ordinary 200K + 24 cache HTTP and 2 fixtures.',
        prior_plan=str(source / '.q4t-work/scoring-continuation/plan.json'),
        completion_tool_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'],
            cwd=Path(__file__).resolve().parents[2], text=True).strip())
    inputs = [Path(__file__).resolve(), reuse_path, Path(plan['prior_plan']),
              Path(__file__).resolve().parents[2] /
              'docs/MTP_PREFILL_COMPLETION_PLAN_2026-11-05.md']
    for phase in ('scoring-acceptance', 'scoring-continuation'):
        folder = source / '.q4t-work' / phase
        inputs += [p for p in folder.rglob('*') if p.is_file()
                   and not p.is_relative_to(folder / 'frozen')]
    for name in ('review.json', 'decision.json', 'archive-receipt.json',
                 'protection-windows-partial.json', 'source-review.json'):
        inputs.append(source.parent / name)
    for item in inputs:
        plan['files'][str(item)] = driver.legacy.sha(item)
    # Freeze the new absolute deadline last, after all read-only preparation.
    plan['task_deadline_unix'] = time.time() + 1200
    validate(plan)
    write(path, plan)
    print(json.dumps(dict(plan=str(path), sha256=driver.legacy.sha(path),
        requests=27, services=3, contracts=2, seconds=1200, reused=171)))


def validate(plan):
    require(plan['stage'] == 'SCORING_FINISH' and
            (plan['http_requests'], plan['http_loads'],
             plan['maximum_gpu_contracts'], plan['stage_timeout_seconds']) ==
            (27, 3, 2, 1200), 'completion budget')
    require(time.time() < plan['task_deadline_unix'], 'completion absolute deadline')
    steps = plan['steps']
    require([s['kind'] for s in steps] == ['matrix', 'combined_gate',
            'contract', 'contract', 'lifecycle', 'lifecycle'], 'completion order')
    require([s['http_requests'] for s in steps] == [3, 0, 0, 0, 12, 12] and
            [s['http_loads'] for s in steps] == [1, 0, 0, 0, 1, 1] and
            [s['head'] for s in steps[2:]] == ['full', 'subset', 'full', 'subset'],
            'completion ledger')
    first = steps[0]
    require(first['arm'] == 'parent' and first['head'] == 'plain' and
            first['layout'] == 'single' and not first['suffix'] and
            first['binary'] == plan['parent'] and
            first['cases'] == [plan['protection'][-1]] and
            first['cases'][0]['count'] == 3 and
            first['cases'][0]['tokens'] == 256, '200K-only completion')


def paired(plan):
    prior = read(plan['prior_plan'])
    pairs = [driver.paired(prior, mode) for mode in
             ('agent-full', 'agent-subset', 'single-full')]
    original = Path(prior['output'])
    parent = read(original / '10-single-plain-parent/results.json')
    new_folder = Path(plan['output']) / plan['steps'][0]['name']
    end = read(new_folder / 'exit.json')
    require(end['server'] == 0 and end['http_output_checks_passed'],
            'new 200K service incomplete')
    parent += read(new_folder / 'results.json')
    candidate = read(original / '09-single-plain-candidate/results.json')
    require([c['id'] for c in parent] == [c['id'] for c in candidate] ==
            [c['id'] for c in plan['protection']], 'five-tier case coverage')
    rows = []
    for a, b in zip(parent, candidate):
        row = dict(mode='single-plain', case=a['id'], **driver.metrics.compare(a, b))
        def identities(value):
            return sorted((r['prompt_sha256'], r['input_tokens'],
                r['output_sha256'], r['output_tokens'], r['finish']) for r in value['rows'])
        row.update(output_same=identities(a) == identities(b), target_met=True,
            parent_ttft=[r['ttft'] for r in a['rows']],
            candidate_ttft=[r['ttft'] for r in b['rows']],
            parent_latency=[r['latency'] for r in a['rows']],
            candidate_latency=[r['latency'] for r in b['rows']],
            parent_new_start=a['id'] == 'context-204800')
        rows.append(row)
    pairs.append(dict(mode='single-plain', results=rows,
        passed=all(r['output_same'] for r in rows),
        negative_observations=[r['case'] for r in rows if r['ttft_percent'] > 0
                               or r['decode_percent'] < 0 or r['e2e_percent'] < 0],
        statistics='DESCRIPTIVE_NOT_NONINFERIORITY',
        limitation='Parent first four tiers reused; 200K is a separate cold service '
                   'start, without warmup. No cross-start makespan aggregation.'))
    return pairs


def custom_step(plan, name):
    step = next(s for s in plan['steps'] if s['name'] == name)
    if step['kind'] != 'combined_gate':
        return driver.custom_step(plan, name)
    driver.legacy.require_guard()
    folder = Path(plan['output']) / name
    folder.mkdir()
    write(folder / 'step.json', step)
    pairs = paired(plan)
    passed = all(p['passed'] for p in pairs)
    write(folder / 'gate.json', dict(passed=passed, pairs=pairs))
    require(passed, 'combined output or performance target failed')


def review(source, path):
    plan = read(path)
    prior, reused = prior_evidence(source)
    driver.legacy.verify_files(plan)
    out = Path(plan['output'])
    state, guard = read(out / 'state.json'), read(path.parent / 'lease/exit.json')
    post, telemetry = read(path.parent / 'postflight.json'), read(out / 'telemetry-summary.json')
    require(state['status'] == post['status'] == 'MEASURED_REQUIRES_REVIEW' and
            guard['status'] == 'COMPLETED' and guard['child_returncode'] == 0,
            'completion stage did not pass')
    require(state['requests_completed'] == 27 and state['loads_started'] == 3 and
            state['completed'] == [s['name'] for s in plan['steps']] and
            state['finished'] <= plan['task_deadline_unix'] and
            state['elapsed_seconds'] <= 1200, 'completion ledger/deadline')
    require(post['final_monitor_barrier'] and not any(post[k] for k in
            ('monitor_failures', 'cleanup_errors', 'final_monitor_errors')) and
            not telemetry['unflushed_attempts'] and not telemetry['failures_at_exit']
            and telemetry['sampler_failure'] is None and
            telemetry['sample_attempts'] == telemetry['flushed_samples'] and
            telemetry['exit_pending']['active'] == 0, 'completion telemetry')
    seal = read(out / 'files.json')
    require(all(driver.legacy.sha(out / n) == d for n, d in seal.items()),
            'completion raw evidence changed')
    pairs = paired(plan)
    require(all(p['passed'] for p in pairs), 'HTTP output/target gate')
    new_cases = checked_cases(plan)
    require(len(new_cases) == 1 and new_cases[0]['requests'] == 3, 'new case coverage')
    contracts, cache = [], []
    for step in plan['steps'][2:]:
        folder = out / step['name']
        if step['kind'] == 'contract':
            item = read(folder / 'result.json')
            log = (folder / 'contract.log').read_text()
            require(item['passed'] and item['returncode'] == 0 and
                    log.count('PASS scoring_reference ') == 4 and
                    log.count('PASS prefix_state=1 ') == 1 and 'FAIL ' not in log,
                    'scoring/state fixture failed')
            contracts.append(item)
        else:
            item = read(folder / 'lifecycle.json')
            require(item['passed'] and item['requests'] == 12 and
                    item['rejected_http'] == 0, 'cache lifecycle failed')
            driver.audit(folder, 12)
            cache.append(dict(head=step['head'], result=item))
    original = Path(plan['execution_tools_root'])
    protected = read(source.parent / 'protected-worktree.json')
    baseline = read(source.parent / 'baseline.json')
    require(all(driver.legacy.sha(original / n) == d for n, d in
                protected['worktree_files'].items()), 'client WIP changed')
    require(driver.legacy.sha(original / 'build/q4t') == plan['parent_sha256'] and
            driver.legacy.sha(original / 'build/q4t.release.json') == baseline['sidecar'],
            'default changed before acceptance')
    result = dict(measurement_passed=True,
        engineering_decision='PENDING_PROTECTION_REVIEW',
        evidence_validity='COMPLETED_WINDOWS_WITH_DISCLOSED_INTERRUPTED_PRIOR_PHASES',
        statistics='DESCRIPTIVE_NOT_STATISTICAL_NONINFERIORITY',
        requests=dict(valid=198, reused=171, new=27, admissions=208,
            services=12, leases=3, wrong_budget_attempts=8,
            unfinished_client_attempts=2, direct_gpu_contracts=2),
        candidate_sha256=plan['candidate_sha256'], parent_sha256=plan['parent_sha256'],
        source_commit=plan['source_commit'], plan_sha256=driver.legacy.sha(path),
        raw_seal_sha256=driver.legacy.sha(out / 'files.json'), raw_files=len(seal),
        pairs=pairs, quality=[c for c in reused if c['case'] == 'quality'],
        contracts=contracts, lifecycle=cache, reused_cases=reused, new_cases=new_cases,
        guard=guard, state=state, postflight=post, telemetry=telemetry,
        limitations=['Two prior stages remain STOPPED/COMMAND_FAILED without final '
            'barriers; only sealed complete client windows are reused.',
            'Plain parent 200K comes from another cold start. No statistical '
            'noninferiority or cross-start five-tier block claim.',
            'Polling cannot exclude sub-sample or invisible interference.',
            'State fixture covers four representative main layers plus draft; '
            'full-model behavior is assessed separately by HTTP.'],
        review_source_sha256=driver.legacy.sha(Path(__file__)))
    write(path.parent / 'review.json', result)
    print(json.dumps(dict(measurement_passed=True, requests=result['requests'],
                         elapsed_seconds=state['elapsed_seconds'])))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'run', 'review'))
    parser.add_argument('--source-root', required=True, type=Path)
    parser.add_argument('--plan', required=True, type=Path)
    parser.add_argument('--custom-step')
    args = parser.parse_args()
    source, path = args.source_root.resolve(), args.plan.resolve()
    driver = load(source)
    if args.action == 'prepare':
        prepare(source, path)
    elif args.action == 'review':
        review(source, path)
    else:
        def interrupted(*_):
            raise KeyboardInterrupt('owned scoring completion interrupted')
        signal.signal(signal.SIGTERM, interrupted)
        driver.reuse.validate = validate
        driver.reuse.custom_step = custom_step
        driver.reuse.admission.audit_server = driver.audit
        sys.argv = [sys.argv[0], '--plan', str(path)]
        if args.custom_step:
            sys.argv += ['--custom-step', args.custom_step]
        driver.reuse.main()
