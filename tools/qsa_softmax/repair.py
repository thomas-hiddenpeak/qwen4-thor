"""One affected-arm HTTP retest after the concrete FMA contract failure."""
import argparse
import copy
import importlib.util
import json
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time

spec = importlib.util.spec_from_file_location('qsa_stage',
    Path(__file__).with_name('stage.py'))
stage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stage)
ROOT, sha, save, require = stage.ROOT, stage.sha, stage.save, stage.require
BASE = ROOT / '.q4t-work/qsa-softmax-repair-20261106'
OLD = ROOT / '.q4t-work/qsa-softmax-20261106'
PLAN = BASE / 'plan.json'


def review_parent():
    """Accept only the completed arm, never promote the failed whole stage."""
    old = json.loads((OLD / 'plan.json').read_text())
    require(sha(OLD / 'plan.json') == sha(OLD / 'run/plan.json'), 'old plan changed')
    seal = json.loads((OLD / 'run/files.json').read_text())
    require(all(sha(OLD / 'run' / n) == h for n, h in seal.items()), 'old raw seal')
    state = json.loads((OLD / 'run/state.json').read_text())
    post = json.loads((OLD / 'postflight.json').read_text())
    guard = json.loads((OLD / 'lease/exit.json').read_text())
    summary = json.loads((OLD / 'run/telemetry-summary.json').read_text())
    require(state['status'] == post['status'] == 'STOPPED' and
        state['completed'] == ['00-plain-parent'] and state['requests_completed'] == 17 and
        state['loads_started'] == 2 and guard['status'] == 'COMMAND_FAILED' and
        guard['child_returncode'] == 1 and not state['monitor_failures'] and
        not post['monitor_failures'] and not post['cleanup_errors'] and
        not post['final_monitor_errors'] and post['state_sha256'] == sha(OLD / 'run/state.json') and
        post['final_summary_sha256'] == sha(OLD / 'run/telemetry-summary.json') and
        not stage.base.monitored_stage._summary_errors(summary, {}, False),
        'failed-stage environment/accounting is not reusable')
    failed = json.loads((OLD / 'run/01-plain-candidate/exit.json').read_text())
    require(failed['server'] == 0 and failed['failure'] ==
            "ValueError('sparse softmax final output mismatch')", 'unexpected first failure')
    folder = OLD / 'run/00-plain-parent'
    audit = stage.audit_server(folder, 17)
    cases = [stage.raw_case(old, c, folder / c['id']) for c in old['steps'][0]['cases']]
    require({r['response_id'] for c in cases for r in c['rows']} ==
            set(audit['request_ids']), 'reused HTTP/server IDs')
    events = [json.loads(line) for line in (folder / 'events.jsonl').read_text().splitlines()]
    stopped = [e for e in events if e['event'] == 'server_after_shutdown']
    require(len(stopped) == 1 and stopped[0]['server_returncode'] == 0 and
        summary['last_flushed_heartbeat'] - summary['final_successful_cycle_duration_seconds'] >
        stopped[0]['monotonic_seconds'], 'no monitor cycle after reused parent exit')
    require(sha(Path(old['parent'])) ==
        'aae93a734be67d1695f4c6f3d06aae35de55ed9126994d65ad4bb79946b074b4', 'parent ELF')
    return dict(reused_arm_only=True, whole_stage_status='STOPPED',
        whole_lease_status='COMMAND_FAILED', reason='Candidate output failure after parent completed.',
        raw_seal_verified=len(seal), sampler_flushed=summary['flushed_samples'],
        parent_after_shutdown=stopped[0], audit=audit, cases=cases)


def validate(plan, executing=True):
    require(plan['stage'] == 'QSA_SOFTMAX_FMA_REPAIR' and len(plan['steps']) == 1 and
        (plan['http_requests'], plan['http_loads'], plan['stage_timeout_seconds']) ==
        (17, 1, 540) and plan['minimum_ttft_gain'] == .03, 'bounded repair scope')
    step = plan['steps'][0]
    require(step['arm'] == 'repaired' and step['timeout'] == 540 and
        step['http_requests'] == 17 and step['http_loads'] == 1 and not step.get('env') and
        [(c['id'], c['count'], c['tokens'], c['parallel']) for c in step['cases']] == stage.CASES and
        step['server_command'] == stage.server_command(plan, 'candidate'), 'repair arm')
    if executing and time.time() >= plan['task_deadline_unix']:
        raise TimeoutError('repair absolute deadline')


def prepare():
    require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT),
            'commit repair first')
    reviewed = review_parent()
    old = json.loads((OLD / 'plan.json').read_text())
    original = Path(old['execution_tools_root'])
    build = BASE / 'engine'
    receipt = json.loads((BASE / 'build-exit.json').read_text())
    require(receipt['returncode'] == 0 and receipt['source_sha256'] ==
            sha(ROOT / 'src/model/full_attention.cu'), 'repair build identity')
    require(not any(re.search(r'\bwarning:', (BASE / n).read_text(), re.I)
                    for n in ('configure.log', 'build.log')), 'build warnings')
    contract = json.loads((BASE / 'contract/result.json').read_text())
    require(contract['passed'] and contract['elapsed_seconds'] < 120 and
        json.loads((BASE / 'contract-lease/exit.json').read_text())['status'] == 'COMPLETED',
        'targeted numerical repair not verified')
    require(sha(ROOT / 'src/model/full_attention.cu') ==
            sha(BASE / 'contract/repaired.cu.txt'), 'tested repair changed')
    release = OLD / 'frozen/installed-default.release.json'
    default_sources = json.loads((OLD / 'frozen/installed-default-runtime-source.json').read_text())
    require(sha(original / 'build/q4t.release.json') == sha(release) and
        sha(original / 'build/q4t') == old['files'][str(original / 'build/q4t')] and
        all(sha(original / n) == h for n, h in default_sources.items()), 'default changed')
    parent_sources = json.loads((OLD / 'frozen/parent-runtime-source.json').read_text())
    changed = [n for n, h in parent_sources.items() if sha(ROOT / n) != h]
    require(changed == ['src/model/full_attention.cu'], 'repair runtime scope')
    require(sha(build / 'generated/q4t/server/chat_protocol_identity.h') ==
        sha(OLD / 'engine/generated/q4t/server/chat_protocol_identity.h'), 'protocol changed')
    frozen = BASE / 'frozen'
    frozen.mkdir(exist_ok=False)
    shutil.copy2(build / 'q4t', frozen / 'candidate')
    require(sha(build / 'q4t') == sha(frozen / 'candidate'), 'candidate copy')
    save(frozen / 'candidate-runtime-source.json', {n: sha(ROOT / n) for n in parent_sources})
    save(frozen / 'parent-reuse.json', reviewed)
    cases = copy.deepcopy(old['steps'][0]['cases'])
    for c in cases:
        source = Path(c['path'])
        require(sha(source) == old['files'][str(source)], 'fixture changed')
        target = frozen / (c['id'] + '.jsonl')
        shutil.copy2(source, target)
        c['path'] = str(target)
    plan = dict(stage='QSA_SOFTMAX_FMA_REPAIR', output=str(BASE / 'run'),
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'],
            cwd=ROOT, text=True).strip(), runtime_delta=changed,
        execution_tools_root=str(original), candidate=str(frozen / 'candidate'),
        parent=old['parent'], parent_reuse=str(frozen / 'parent-reuse.json'),
        model=old['model'], model_file_metadata=old['model_file_metadata'],
        quality=old['quality'], port=8762, http_requests=17, http_loads=1,
        minimum_ttft_gain=.03, stage_timeout_seconds=540, approved_total_wall_seconds=600,
        task_deadline_unix=time.time() + 540, direct_contract_timeout_seconds=0,
        resource_sample_max_age_seconds=10, telemetry_sources=stage.telemetry.discover_sources(),
        steps=[], files={})
    name = '00-plain-repaired'
    plan['steps'] = [dict(name=name, head='plain', arm='repaired', kind='matrix',
        http_requests=17, http_loads=1, timeout=540, cases=cases,
        server_command=stage.server_command(plan, 'candidate'), command=[sys.executable,
            '-B', str(Path(__file__).resolve()), '--plan', str(PLAN), '--step', name])]
    tracked = subprocess.check_output(['git', 'ls-files'], cwd=ROOT, text=True).splitlines()
    files = {Path(n) for n in old['files']}
    files.update(ROOT / n for n in tracked if n.startswith(('tools/', 'src/', 'include/', 'cmake/')))
    for directory in (frozen, build / 'generated', BASE / 'contract', BASE / 'contract-lease',
                      OLD / 'run', OLD / 'lease'):
        files.update(p for p in directory.rglob('*') if p.is_file())
    files.update([OLD / 'plan.json', OLD / 'postflight.json', OLD / 'failure-review.json',
        OLD / 'input-archive.json', OLD / 'archives/inputs.tar.gz', BASE / 'build-command.json',
        BASE / 'build-exit.json', BASE / 'build.log', BASE / 'configure.log', build / 'q4t',
        build / 'CMakeCache.txt', build / 'compile_commands.json',
        ROOT / 'docs/QSA_SOFTMAX_PLAN_2026-11-06.md'])
    plan['files'] = {str(p): sha(p) for p in sorted(files) if p.is_file()}
    validate(plan)
    stage.legacy.verify_files(plan)
    save(PLAN, plan)
    print(json.dumps(dict(plan=str(PLAN), files=len(plan['files']),
        candidate_sha256=sha(frozen / 'candidate'), reused_parent_requests=17)))


def custom_step(plan, name):
    parent = json.loads(Path(plan['parent_reuse']).read_text())
    reference = {c['id']: c for c in parent['cases']}
    original = stage.base.multi.case

    def compare(p, step, c, out, env, check):
        result = original(p, step, c, out, env, check)
        same = stage.base.identity(result) == stage.base.identity(reference[c['id']])
        save(out / (c['id'] + '-comparison.json'), dict(parent_candidate_outputs_identical=same))
        require(same, 'repaired candidate output differs from original parent')
        return result

    stage.base.multi.case = compare
    try:
        stage.custom_step(plan, name)
    finally:
        stage.base.multi.case = original


def analyze(plan, lease):
    validate(plan, executing=False)
    stage.base.monitored_stage.require_complete_monitor(plan)
    stage.legacy.verify_files(plan)
    require(json.loads((lease / 'exit.json').read_text())['status'] == 'COMPLETED', 'repair lease')
    output = Path(plan['output'])
    seal = json.loads((output / 'files.json').read_text())
    require(all(sha(output / n) == h for n, h in seal.items()), 'repair raw seal')
    parent = review_parent()
    require(parent == json.loads(Path(plan['parent_reuse']).read_text()), 'reused parent changed')
    step = plan['steps'][0]
    folder = output / step['name']
    audit = stage.audit_server(folder, 17)
    cases = [stage.raw_case(plan, c, folder / c['id']) for c in step['cases']]
    require({r['response_id'] for c in cases for r in c['rows']} == set(audit['request_ids']),
            'repaired HTTP/server identities')
    require(all(stage.base.identity(a) == stage.base.identity(b) for a, b in
                zip(parent['cases'], cases)), 'repaired outputs changed')
    contexts = {}
    for a, b in zip(parent['cases'][:2], cases[:2]):
        p, c = stage.metrics(a['rows']), stage.metrics(b['rows'])
        contexts[a['id']] = dict(parent=p, candidate=c,
            ttft_gain=1-c['mean_ttft']/p['mean_ttft'],
            decode_gain=c['aggregate_decode']/p['aggregate_decode']-1,
            e2e_change=c['aggregate_e2e']/p['aggregate_e2e']-1)
    target = contexts['context-45056']
    passed = target['ttft_gain'] >= .03 and target['e2e_change'] < 0
    result = dict(decision='GO_FOR_FULL_EVALUATION' if passed else 'NO_GO_FEASIBILITY_TARGET',
        performance_accepted=False, outputs_identical=True, quality_passed=22,
        newly_executed_http=17, reused_parent_http=17, first_failure_http=20,
        total_http=37, total_model_loads=3, contexts=contexts, candidate_cases=cases,
        audit=audit, parent_reuse=parent, statistics='DESCRIPTIVE_SINGLE_START_WITH_PARENT_REUSE',
        first_request_start_gap_seconds=cases[0]['rows'][0]['start']-parent['cases'][0]['rows'][0]['start'],
        limitations=['Original whole stage remains STOPPED/COMMAND_FAILED.',
                     'Parent was reused after a concrete numerical repair; time/cold/order effects remain.',
                     'No statistical noninferiority or full five-tier/mode acceptance.'])
    save(BASE / 'decision.json', result)
    print(json.dumps({k: result[k] for k in ('decision', 'contexts', 'first_request_start_gap_seconds')}, indent=2))


if __name__ == '__main__':
    def interrupted(*_):
        raise KeyboardInterrupt('owned softmax repair interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare', action='store_true')
    parser.add_argument('--plan', type=Path, default=PLAN)
    parser.add_argument('--step')
    parser.add_argument('--analyze-only', action='store_true')
    parser.add_argument('--lease', type=Path)
    args = parser.parse_args()
    if args.prepare:
        prepare()
    else:
        plan = json.loads(args.plan.read_text())
        if args.analyze_only:
            require(args.lease is not None, 'completed lease required')
            analyze(plan, args.lease)
        else:
            validate(plan)
            if args.step:
                custom_step(plan, args.step)
            else:
                stage.base.monitored_stage.run_stage(stage.legacy, stage.telemetry, plan,
                    args.plan, audit_server=stage.audit_server)
