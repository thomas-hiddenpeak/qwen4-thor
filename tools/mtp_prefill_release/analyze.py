"""Offline frozen targets and integrity; no retries or inferred loss tolerance."""
import argparse
import importlib.util
import json
import math
from pathlib import Path
import re
import statistics

spec = importlib.util.spec_from_file_location('release_analyze_driver',
    Path(__file__).with_name('run_stage.py'))
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)

ROOT = driver.ROOT
BASE = ROOT/'.q4t-work/mtp-prefill-release-20261029'
read = lambda path: json.loads(Path(path).read_text())
sha, save = driver.legacy.sha, driver.save
compare = driver.module('release_compare', 'tools/mtp_suffix_agent/analyze.py').compare


def c_results(plan, blocks):
    arms, slots = {}, {}
    for step in plan['steps']:
        if not step['http_loads'] or step['block'] not in blocks:
            continue
        folder = Path(plan['output'])/step['name']
        key = (step['head'], step['block'], step['shared'])
        arms[key] = {case['id']: case for case in read(folder/'results.json')}
        slots[key] = {rid: int(slot) for rid, slot in re.findall(
            r'\[q4t\]\[slot-admission\] request=(\S+) slot=(\d+) ',
            (folder/'server.log').read_text())}
        if len(arms[key]) != 4:
            raise RuntimeError('four scenarios required')
    rows = []
    for head in ['full', 'subset']:
        for scenario in plan['scenarios']:
            pairs = []
            for block in blocks:
                off, on = [arms[head, block, enabled][scenario['id']]
                           for enabled in [False, True]]
                if len(off['rows']) != 4 or len(on['rows']) != 4:
                    raise RuntimeError('four agents required')
                same = all(all(a[k] == b[k] for k in ['prompt_sha256', 'input_tokens',
                    'output_sha256', 'output_tokens', 'finish']) and a['output_tokens'] == 256
                    for a, b in zip(off['rows'], on['rows']))
                cache = ([r['cached_tokens'] for r in off['rows']] == [0]*4 and
                         [r['cached_tokens'] for r in on['rows']] == [0]+[scenario['prefix']]*3 and
                         all(r['cache_source'] == 'shared' for r in on['rows'][1:]))
                for enabled, case in [(False, off), (True, on)]:
                    actual = [slots[head, block, enabled][r['response_id']] for r in case['rows']]
                    if actual[0] != 0 or set(actual[1:]) != {1, 2, 3}:
                        raise RuntimeError('long/short slots')
                pairs.append(dict(block=block, off_seconds=off['makespan'],
                    on_seconds=on['makespan'], reduction=1-on['makespan']/off['makespan'],
                    output_and_input_equal=same, cache_contract=cache,
                    off_child_ttft=[r['ttft'] for r in off['rows'][1:]],
                    on_child_ttft=[r['ttft'] for r in on['rows'][1:]], seed_writes=on['seed_writes']))
            ratio = math.exp(statistics.mean(math.log(b['on_seconds']/b['off_seconds']) for b in pairs))
            target = .03 if scenario['prefix'] == 8192 else .10
            rows.append(dict(head=head, scenario=scenario['id'], blocks=pairs,
                gm_time_reduction=1-ratio, target=target,
                target_met=1-ratio >= target and all(b['reduction'] > 0 for b in pairs),
                correctness=all(b['output_and_input_equal'] and b['cache_contract'] for b in pairs)))
    return rows


def first_c_block(plan):
    rows = c_results(plan, [0])
    passed = all(r['correctness'] and r['blocks'][0]['reduction'] > 0 for r in rows)
    save(Path(plan['output'])/'first-block-decision.json', dict(passed=passed, results=rows))
    if not passed:
        raise RuntimeError('NO_GO_FIRST_SHARED_BLOCK; stop without expansion')


def phase_review(phase):
    folder = BASE/('phase-'+phase.lower())
    plan = read(folder/'plan.json')
    state = read(folder/'run/state.json')
    post = read(folder/'postflight.json')
    guard = read(folder/'lease/exit.json')
    telemetry = read(folder/'run/telemetry-summary.json')
    if (state['status'] != 'MEASURED_REQUIRES_REVIEW' or post['status'] != state['status'] or
            guard['status'] != 'COMPLETED' or state['finished'] >= plan['task_deadline_unix']):
        raise RuntimeError('incomplete/invalid stage')
    if (post['final_monitor_errors'] or post['cleanup_errors'] or not post['final_monitor_barrier'] or
            telemetry['unflushed_attempts'] or telemetry['sampler_failure'] or
            telemetry['failures_at_exit'] or telemetry['exit_pending']['active'] or
            telemetry['sample_attempts'] != telemetry['flushed_samples']):
        raise RuntimeError('monitor/cleanup incomplete')
    driver.legacy.verify_files(plan)
    for name, digest in read(folder/'run/files.json').items():
        if sha(folder/'run'/name) != digest:
            raise RuntimeError('raw seal changed: '+name)
    result = dict(phase=phase, passed=True, candidate_sha256=plan['candidate_sha256'],
        requests=plan['http_requests'], starts=plan['http_loads'],
        elapsed_seconds=state['elapsed_seconds'], statistics='DESCRIPTIVE_NOT_NONINFERIORITY',
        state_sha256=sha(folder/'run/state.json'), postflight_sha256=sha(folder/'postflight.json'),
        guard_sha256=sha(folder/'lease/exit.json'), telemetry=telemetry, results=[])
    if phase == 'A':
        arms = {}
        for step in plan['steps']:
            d = folder/'run'/step['name']
            arms[step['head'], step['arm']] = {c['id']: c for c in read(d/'results.json')}
            if '[session-cache] request=' in (d/'server.log').read_text() or '[prefix-cache] request=' in (d/'server.log').read_text():
                raise RuntimeError('five-tier snapshot operation')
        for head in ['full', 'subset', 'plain']:
            for case in plan['protection']:
                off, on = [arms[head, arm][case['id']] for arm in ['parent', 'candidate']]
                if len(off['rows']) != 3 or len(on['rows']) != 3:
                    raise RuntimeError('three repetitions required')
                same = all(all(a[k] == b[k] for k in ['prompt_sha256', 'input_tokens',
                    'output_sha256', 'output_tokens', 'finish']) for a, b in zip(off['rows'], on['rows']))
                a, b = [statistics.median(r['ttft'] for r in x['rows']) for x in [off, on]]
                e0, e1 = [statistics.median(r['latency'] for r in x['rows']) for x in [off, on]]
                target_met = head == 'plain' or case['id'] != 'context-204800' or (1-b/a >= .2 and e1 < e0)
                result['results'].append(dict(head=head, tier=case['id'],
                    paired_output_equal=same, target_met=target_met,
                    median_ttft_parent=a, median_ttft_candidate=b, median_ttft_reduction=1-b/a,
                    median_e2e_parent=e0, median_e2e_candidate=e1, **compare(off, on)))
        result['passed'] = all(r['paired_output_equal'] and r['target_met'] and r['output_same'] for r in result['results'])
        result['negative_observations'] = [r for r in result['results']
            if r['ttft_percent'] > 0 or r['e2e_percent'] < 0 or r['decode_percent'] < 0]
        result['protection_review'] = 'ENGINEERING_REVIEW_REQUIRED_NO_AUTOMATIC_TOLERANCE'
    elif phase in ['B', 'H']:
        for step in plan['steps']:
            record = read(folder/'run'/step['name']/('lifecycle.json' if phase == 'B' else 'health-result.json'))
            if not record['passed']:
                raise RuntimeError('HTTP lifecycle/health failure')
            result['results'].append(dict(head=step['head'], record=record))
    elif phase == 'S':
        arms = {(step['head'], step['suffix']): {c['id']: c for c in read(folder/'run'/step['name']/'results.json')}
                for step in plan['steps']}
        for head in ['full', 'subset']:
            for name in ['repeat-code', 'repeat-text']:
                comparison = compare(arms[head, False][name], arms[head, True][name])
                result['results'].append(dict(head=head, scenario=name,
                    target_met=comparison['e2e_percent'] > 0, **comparison))
        result['passed'] = all(r['target_met'] and r['output_same'] for r in result['results'])
    elif phase == 'C':
        result['results'] = c_results(plan, [0, 1])
        result['passed'] = all(r['target_met'] and r['correctness'] for r in result['results'])
        if not read(folder/'io-release.json')['passed']:
            raise RuntimeError('IO preparation release')
    else:
        raise ValueError('unknown phase')
    save(BASE/('gate-'+phase.lower()+'.json'), result)
    print(json.dumps({k: v for k, v in result.items() if k != 'telemetry'}, indent=2))
    if not result['passed']:
        raise RuntimeError('NO_GO_'+phase+'; frozen target/contract failed, no expansion')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=['A', 'B', 'S', 'C', 'H'])
    phase_review(parser.parse_args().phase)
