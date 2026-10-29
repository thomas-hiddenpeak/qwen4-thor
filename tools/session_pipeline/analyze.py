"""Review the frozen local experiment without launching or repeating requests."""
import hashlib
import datetime
import importlib.util
import json
from pathlib import Path
import re
import statistics

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT/'.q4t-work/session-pipeline-20261029'


def read(path):
    return json.loads(path.read_text())


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    plan = read(BASE/'plan.json')
    state = read(BASE/'run/state.json')
    if state['status'] == 'RUNNING':
        raise ValueError('Review only completed/stopped evidence')
    errors = []
    for path, digest in plan['files'].items():
        if sha(Path(path)) != digest:
            errors.append('changed input: '+path)
    post = read(BASE/'postflight.json')
    guard = read(Path(plan['lease_directory'])/'exit.json')
    environment_valid = (post['status'] != 'ENV_ABORT' and
                         not post['final_monitor_errors'] and
                         not post['monitor_failures'] and
                         not post['cleanup_errors'] and
                         guard['status'] != 'ENV_ABORT')
    ended = datetime.datetime.fromisoformat(
        guard['finished']['utc'].replace('Z', '+00:00')).timestamp()
    if ended > plan['task_deadline_unix']:
        errors.append('absolute deadline exceeded')
    if state['requests_completed'] != 40 or state['loads_started'] != 4:
        errors.append('request/start ledger incomplete')
    services = {}
    for step in plan['steps']:
        if step['kind'] != 'block':
            continue
        path = BASE/'run'/step['name']
        if step['name'] not in state['completed']:
            errors.append('incomplete service: '+step['name'])
            continue
        exit_row = read(path/'exit.json')
        if exit_row['server'] != 0 or not exit_row['http_output_checks_passed']:
            errors.append('bad service exit: '+step['name'])
        audit = read(path/'timing-audit.json')
        timings = {(r['request'], r['pool']):r for r in audit['records']}
        raw = (path/'server.log').read_text()
        saves = {r['request']:r for r in
            (dict(re.findall(r'(\w+)=(\S+)', l)) for l in raw.splitlines()
             if l.startswith('[session-cache] request=') and 'event=save ' in l)}
        cases = []
        for case in read(path/'results.json'):
            main_row = case['rows'][0]
            rid = main_row['response_id']
            cases.append(dict(shared=case['shared'], makespan=case['makespan'],
                e2e_tps=case['output_tokens']/case['makespan'],
                main_save_seconds=float(saves[rid]['ms'])/1000,
                main_save=timings[rid, 'private'],
                children_save=[timings[r['response_id'], 'private'] for r in case['rows'][1:]],
                main_ttft=main_row['ttft'], main_latency=main_row['latency'],
                outputs=[{k:r[k] for k in ['prompt_sha256', 'output_sha256',
                    'input_tokens', 'output_tokens', 'finish']} for r in case['rows']]))
        restores = read(path/'restores.json')
        if len(restores) != 2 or any(r['cached_tokens'] != 131323 or r['text'].strip() != '44'
                                    for r in restores):
            errors.append('restore contract: '+step['name'])
        services[step['name']] = dict(head=step['head'], pipeline=step['pipeline'],
            cases=cases, restore_latency_seconds=[r['latency'] for r in restores],
            logical_write_bytes=audit['logical_write_bytes'])
    comparisons = []
    for head in ['full', 'subset']:
        arms = {s['pipeline']:s for s in services.values() if s['head'] == head}
        if set(arms) != {0, 1}:
            continue
        old, new = [{c['shared']:c for c in arms[i]['cases']} for i in (0, 1)]
        differences = []
        for shared in [False, True]:
            differences.append(dict(shared=shared,
                original_seconds=old[shared]['makespan'], candidate_seconds=new[shared]['makespan'],
                seconds_saved=old[shared]['makespan']-new[shared]['makespan'],
                e2e_throughput_change=old[shared]['makespan']/new[shared]['makespan']-1,
                output_exact=old[shared]['outputs'] == new[shared]['outputs']))
        saved = statistics.median(c['main_save_seconds'] for c in old.values()) - \
                statistics.median(c['main_save_seconds'] for c in new.values())
        public_gain = new[False]['makespan'] - new[True]['makespan']
        restore_saved = sum(arms[0]['restore_latency_seconds']) - sum(arms[1]['restore_latency_seconds'])
        passed = (saved >= 1 and public_gain > 0 and restore_saved >= 0 and
                  all(d['seconds_saved'] >= 0 and d['output_exact'] for d in differences))
        comparisons.append(dict(head=head, passed=passed, save_median_seconds_improved=saved,
            candidate_public_seconds_saved=public_gain, aggregate_restore_seconds_saved=restore_saved,
            candidate_public_e2e_throughput_change=new[False]['makespan']/new[True]['makespan']-1,
            comparisons=differences))
    cpu_path = BASE/'run/04-cpu-contract/result.json'
    cpu = read(cpu_path) if cpu_path.exists() else {'passed': False, 'not_executed': True}
    logical_writes = (sum(s['logical_write_bytes'] for s in services.values()) +
                      read(BASE/'io-preparation.json')['bytes'] +
                      cpu.get('logical_write_upper_bound', 0))
    if logical_writes > plan['budget']['write_limit']:
        errors.append('logical write budget')
    measured = state['status'] == 'MEASURED_REQUIRES_REVIEW' and len(comparisons) == 2
    passed = (environment_valid and measured and not errors and cpu['passed'] and
              all(c['passed'] for c in comparisons))
    decision = dict(passed=passed,
        engineering_decision='LOCAL_GO_REQUIRES_DELIVERY_REVIEW' if passed else
            ('ENV_ABORT' if not environment_valid else 'NO_GO'),
        candidate_sha256=plan['candidate_sha256'], evidence_valid=environment_valid and not errors,
        measurement_complete=measured, statistical_noninferiority=False,
        errors=errors, comparisons=comparisons, services=services,
        cpu_contract=cpu, logical_writes_with_cpu_upper_bound=logical_writes,
        execution_finished_unix=ended,
        remaining_budget_seconds=plan['task_deadline_unix']-ended,
        state=state, postflight=post, guard=guard,
        limits=['Host API wall time is not pure GPU duration.',
                'Concurrent child save walls include overlapping model-lock waits.',
                'One pair per head does not establish population noninferiority.',
                'Original failed release decision is preserved.'])
    (BASE/'decision.json').write_text(json.dumps(decision, indent=2)+'\n')
    spec = importlib.util.spec_from_file_location('save_resources', ROOT/'tools/session_cache/analyze_resources.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    resources = module.summarize(BASE/'run')
    (BASE/'resources.json').write_text(json.dumps(resources, indent=2)+'\n')
    print(json.dumps({k:decision[k] for k in ['passed', 'engineering_decision',
        'evidence_valid', 'measurement_complete', 'errors', 'comparisons']}, indent=2))


if __name__ == '__main__':
    main()
