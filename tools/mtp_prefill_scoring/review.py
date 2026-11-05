"""Offline acceptance audit; never starts a model or accepts a release."""
import argparse
import importlib.util
import json
from pathlib import Path
import re

spec = importlib.util.spec_from_file_location('scoring_review_driver',
    Path(__file__).with_name('driver.py'))
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)
sha = driver.legacy.sha


def read(path):
    return json.loads(Path(path).read_text())


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def counters(text):
    return {k: int(v) * 1024 for k, v in
            re.findall(r'^(\w+):\s+(\d+) kB', text, re.M)}


def review(path):
    plan = read(path)
    base, out = path.parent, Path(plan['output'])
    state, guard = read(out / 'state.json'), read(base / 'lease/exit.json')
    post, telemetry = read(base / 'postflight.json'), read(out / 'telemetry-summary.json')
    require(state['status'] == post['status'] == 'MEASURED_REQUIRES_REVIEW',
            'incomplete measured stage')
    require(guard['status'] == 'COMPLETED' and guard['child_returncode'] == 0,
            'invalid outer lease')
    expected_requests = 172 if plan.get('reuse_record') else 198
    require(state['requests_completed'] == plan['http_requests'] == expected_requests and
            state['loads_started'] == plan['http_loads'] == 10 and
            state['completed'] == [s['name'] for s in plan['steps']],
            'incomplete request/start/step ledger')
    require(state['finished'] <= plan['task_deadline_unix'] and
            state['elapsed_seconds'] <= plan['stage_timeout_seconds'],
            'stage exceeded frozen budget')
    require(not post['final_monitor_errors'] and not post['cleanup_errors'] and
            post['final_monitor_barrier'] and not telemetry['unflushed_attempts'] and
            telemetry['sampler_failure'] is None and not telemetry['failures_at_exit'] and
            telemetry['exit_pending']['active'] == 0 and
            telemetry['sample_attempts'] == telemetry['flushed_samples'],
            'monitor or final barrier incomplete')
    driver.legacy.verify_files(plan)
    seal = read(out / 'files.json')
    for name, digest in seal.items():
        require(sha(out / name) == digest, 'measured evidence changed: ' + name)
    pairs = [driver.paired(plan, mode) for mode in
             ('agent-full', 'agent-subset', 'single-full', 'single-plain')]
    require(all(p['passed'] for p in pairs), 'output or frozen target failed')
    quality, contracts, lifecycle = [], [], []
    for step in plan['steps']:
        folder = out / step['name']
        if step['kind'] == 'matrix':
            for case in read(folder / 'results.json'):
                if case['id'] == 'quality':
                    require(len(case['rows']) == 11, 'quality count')
                    for row in case['rows']:
                        expected = plan['quality'][row['prompt_sha256']]
                        require(row['text'].strip() == expected['expected'] and
                            row['input_tokens'] == expected['length'] and
                            row['finish'] == ['stop'], 'quality mismatch')
                    quality.append(dict(head=step['head'], requests=11))
        elif step['kind'] == 'contract':
            record = read(folder / 'result.json')
            require(record['passed'], 'direct contract failed')
            contracts.append(record)
        elif step['kind'] == 'lifecycle':
            record = read(folder / 'lifecycle.json')
            require(record['passed'] and record['requests'] == 12 and
                    record['rejected_http'] == 0, 'cache lifecycle incomplete')
            lifecycle.append(dict(head=step['head'], result=record))
    require(len(quality) == len(contracts) == len(lifecycle) == 2,
            'both heads required')
    resources = dict(samples=0, minimum_mem_available=None,
                     maximum_total_minus_available=0, maximum_owned_rss=0,
                     maximum_owned_swap=0, maximum_system_swap_used=0)
    for line in (out / 'unified-memory.jsonl').open():
        row = json.loads(line)
        mem = counters(row['meminfo'])
        resources['samples'] += 1
        available = mem['MemAvailable']
        resources['minimum_mem_available'] = min(available,
            resources['minimum_mem_available'] or available)
        for key, value in [
                ('maximum_total_minus_available', mem['MemTotal'] - available),
                ('maximum_system_swap_used', mem['SwapTotal'] - mem['SwapFree'])]:
            resources[key] = max(resources[key], value)
        for process in row['processes'].values():
            usage = counters(process.get('status', ''))
            for key, field in [('maximum_owned_rss', 'VmRSS'),
                               ('maximum_owned_swap', 'VmSwap')]:
                if field in usage:
                    resources[key] = max(resources[key], usage[field])
    original = Path(plan['execution_tools_root'])
    protected = read(original / '.q4t-work/mtp-prefill-adoption-20261104/protected-worktree.json')
    require(all(sha(original / name) == digest for name, digest in
                protected['worktree_files'].items()), 'client WIP changed')
    require(sha(original / 'build/q4t') == plan['parent_sha256'],
            'default changed during acceptance')
    return dict(measurement_passed=True, engineering_decision='PENDING_PROTECTION_REVIEW',
        evidence_validity='VALID_WITH_POLLING_LIMITATION',
        measurement_completeness=('198_VALID_HTTP_26_REUSED_172_NEW_11_SERVICES_2_LEASES_8_WRONG_BUDGET_ATTEMPTS'
            if plan.get('reuse_record') else '198_HTTP_10_SERVICES_2_DIRECT_CONTRACTS'),
        reuse=(driver.verify_reuse(plan) if plan.get('reuse_record') else None),
        statistics='DESCRIPTIVE_SINGLE_PAIR_PER_MODE_NOT_NONINFERIORITY',
        source_commit=plan['source_commit'], candidate_sha256=plan['candidate_sha256'],
        parent_sha256=plan['parent_sha256'], plan_sha256=sha(path),
        state_sha256=sha(out / 'state.json'), raw_seal_sha256=sha(out / 'files.json'),
        frozen_files=len(plan['files']), raw_files=len(seal),
        elapsed_seconds=state['elapsed_seconds'], pairs=pairs, quality=quality,
        contracts=contracts, lifecycle=lifecycle, resources=resources,
        protected_client_files=len(protected['worktree_files']),
        telemetry=telemetry, postflight=post, guard=guard,
        review_source_sha256=sha(Path(__file__)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    result = review(args.plan.resolve())
    with args.output.open('x') as stream:
        json.dump(result, stream, indent=2)
        stream.write('\n')
    print(json.dumps({k: result[k] for k in ('measurement_passed',
        'engineering_decision', 'elapsed_seconds', 'resources')}))
