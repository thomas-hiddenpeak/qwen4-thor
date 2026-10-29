"""Offline review of the frozen incremental snapshot experiment."""
import datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import statistics

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT/'.q4t-work/session-incremental-20261029'


def read(path):
    return json.loads(path.read_text())


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def fields(raw, prefix):
    return [dict(re.findall(r'(\w+)=(\S+)', l)) for l in raw.splitlines()
            if l.startswith(prefix)]


def main():
    plan, state = read(BASE/'plan.json'), read(BASE/'run/state.json')
    if state['status'] == 'RUNNING':
        raise ValueError('Only stopped measurements')
    errors = [f'changed input: {p}' for p, digest in plan['files'].items()
              if sha(Path(p)) != digest]
    post, guard = read(BASE/'postflight.json'), read(Path(plan['lease_directory'])/'exit.json')
    environment_valid = (post['status'] != 'ENV_ABORT' and
        not post['final_monitor_errors'] and not post['monitor_failures'] and
        not post['cleanup_errors'] and guard['status'] != 'ENV_ABORT')
    ended = datetime.datetime.fromisoformat(guard['finished']['utc'].replace('Z', '+00:00')).timestamp()
    if ended > plan['task_deadline_unix']:
        errors.append('absolute deadline exceeded')
    services = {}
    for step in plan['steps']:
        if step['kind'] != 'block' or step['name'] not in state['completed']:
            continue
        path = BASE/'run'/step['name']
        audit = read(path/'timing-audit.json')
        timings = {(r['request'], r['pool']):r for r in audit['records']}
        raw = (path/'server.log').read_text()
        saves = {r['request']:r for r in fields(raw, '[session-cache] request=')
                 if r['event'] == 'save'}
        cases = []
        for case in read(path/'results.json'):
            main = case['rows'][0]
            cases.append(dict(shared=case['shared'], makespan=case['makespan'],
                output_tokens=case['output_tokens'], main_ttft=main['ttft'],
                main_save_seconds=float(saves[main['response_id']]['ms'])/1000,
                timings=[timings[r['response_id'], 'private'] for r in case['rows']],
                outputs=[{k:r[k] for k in ['prompt_sha256', 'output_sha256',
                    'input_tokens', 'output_tokens', 'finish']} for r in case['rows']]))
        resumes = []
        for r in read(path/'restores.json'):
            if (r['cached_tokens'] != (131323 if r['generation'] == 1 else 131350)
                    or r['text'].strip() != '44'):
                errors.append('incremental restore mismatch: '+step['name'])
            resumes.append(dict(shared=r['shared'], generation=r['generation'],
                latency=r['latency'], ttft=r['ttft'], cached_tokens=r['cached_tokens'],
                output_sha256=r['output_sha256'], input_tokens=r['input_tokens'],
                output_tokens=r['output_tokens'], finish=r['finish'],
                save_seconds=float(saves[r['response_id']]['ms'])/1000,
                timing=timings[r['response_id'], 'private']))
        services[step['name']] = dict(head=step['head'], incremental=step['incremental'],
            cases=cases, resumes=resumes, logical_write_bytes=audit['logical_write_bytes'])
    comparisons = []
    for head in ['full', 'subset']:
        arms = {v['incremental']:v for v in services.values() if v['head'] == head}
        if set(arms) != {0, 1}:
            continue
        old, new = arms[0], arms[1]
        cold, exact = [], True
        for shared in [False, True]:
            a, b = [next(c for c in arm['cases'] if c['shared'] == shared)
                    for arm in [old, new]]
            bytes_same = [int(t['disk_bytes']) for t in a['timings']] == \
                         [int(t['disk_bytes']) for t in b['timings']]
            exact &= bytes_same and a['outputs'] == b['outputs']
            cold.append(dict(shared=shared, control_seconds=a['makespan'],
                candidate_seconds=b['makespan'], seconds_saved=a['makespan']-b['makespan'],
                throughput_change=a['makespan']/b['makespan']-1,
                main_ttft_change_seconds=b['main_ttft']-a['main_ttft'],
                main_save_control_seconds=a['main_save_seconds'],
                main_save_candidate_seconds=b['main_save_seconds'],
                cold_write_bytes_same=bytes_same))
        order = lambda rows: sorted(rows, key=lambda r:(r['generation'], r['shared']))
        for a,b in zip(order(old['resumes']), order(new['resumes'])):
            exact &= all(a[k] == b[k] for k in ['input_tokens', 'output_sha256',
                                             'output_tokens', 'finish', 'cached_tokens'])
        resave = [statistics.median(r['save_seconds'] for r in arm['resumes'])
                  for arm in [old, new]]
        resume_seconds = [sum(r['latency'] for r in arm['resumes']) for arm in [old,new]]
        lifecycle = [sum(c['makespan'] for c in arm['cases'])+resume_seconds[i]
                     for i,arm in enumerate([old,new])]
        writes = [sum(int(r['timing']['disk_bytes']) for r in arm['resumes'])
                  for arm in [old,new]]
        public = {c['shared']:c['makespan'] for c in new['cases']}
        reduction = 1-writes[1]/writes[0]
        passed = (exact and len(old['resumes']) == len(new['resumes']) == 4 and
            reduction >= .5 and resave[0]-resave[1] >= 1 and
            resume_seconds[0] > resume_seconds[1] and lifecycle[0] > lifecycle[1]
            and public[False] > public[True])
        comparisons.append(dict(head=head, passed=passed, output_and_cold_bytes_exact=exact,
            cold=cold, resave_median_seconds=resave, resave_seconds_saved=resave[0]-resave[1],
            resave_write_bytes=writes, resave_write_reduction=reduction,
            resume_http_seconds=resume_seconds, resume_seconds_saved=resume_seconds[0]-resume_seconds[1],
            lifecycle_http_seconds=lifecycle, lifecycle_seconds_saved=lifecycle[0]-lifecycle[1],
            lifecycle_throughput_change=lifecycle[0]/lifecycle[1]-1,
            candidate_public_seconds_saved=public[False]-public[True]))
    cp = BASE/'run/04-cpu-contract/result.json'
    cpu = read(cp) if cp.exists() else dict(passed=False, not_executed=True)
    prep = read(BASE/'io-preparation.json')
    writes = sum(v['logical_write_bytes'] for v in services.values())+prep['bytes']+cpu.get('logical_write_upper_bound',0)
    if writes > plan['budget']['write_limit']:
        errors.append('write budget')
    measured = (state['status'] == 'MEASURED_REQUIRES_REVIEW' and
        state['requests_completed'] == 48 and state['loads_started'] == 4 and len(comparisons) == 2)
    spec = importlib.util.spec_from_file_location('resources', ROOT/'tools/session_cache/analyze_resources.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    resources = module.summarize(BASE/'run')
    (BASE/'resources.json').write_text(json.dumps(resources,indent=2)+'\n')
    passed = environment_valid and measured and not errors and cpu['passed'] and all(c['passed'] for c in comparisons)
    decision = dict(passed=passed, engineering_decision='LOCAL_GO_REQUIRES_DELIVERY_REVIEW' if passed else
        ('ENV_ABORT' if not environment_valid else ('NO_GO' if measured else 'INCOMPLETE')),
        evidence_valid=environment_valid and not errors, measurement_complete=measured,
        statistical_noninferiority=False, errors=errors, comparisons=comparisons,
        services=services, cpu_contract=cpu, logical_writes_with_cpu_upper_bound=writes,
        execution_finished_unix=ended, remaining_budget_seconds=plan['task_deadline_unix']-ended,
        candidate_sha256=plan['candidate_sha256'], state=state, postflight=post, guard=guard,
        limits=['Single fixed pair per head; order and SSD state confounded.',
                'First-save wall negatives are retained; no cold-save acceleration claim.',
                'Existing formal release NO_GO is unchanged.'])
    (BASE/'decision.json').write_text(json.dumps(decision,indent=2)+'\n')
    print(json.dumps({k:decision[k] for k in ['engineering_decision','evidence_valid','measurement_complete','errors','comparisons']},indent=2))


if __name__ == '__main__':
    main()
