"""Frozen HTTP feasibility gate for GDN BF16 input conversion reuse."""
import argparse
import importlib.util
import json
import math
from pathlib import Path
import re
import signal
import time

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    'gdn_http', ROOT / 'tools/mtp_default_profile/stage.py')
http_stage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(http_stage)
base = http_stage.base
legacy, telemetry = base.legacy, base.telemetry
sha, save = base.sha, base.save
ORDER = [('full', 'parent'), ('full', 'candidate'),
         ('subset', 'candidate'), ('subset', 'parent')]
CASES = [('context-1024', 3, 256, 1), ('context-45056', 3, 256, 1),
         ('quality', 11, 32, 1)]


def require(value, message):
    if not value:
        raise ValueError(message)


def validate(plan, *, executing=True):
    require(plan['stage'] == 'GDN_BF16_INPUT_FEASIBILITY' and
            [(s['head'], s['arm']) for s in plan['steps']] == ORDER and
            (plan['http_requests'], plan['http_loads']) == (68, 4) and
            plan['stage_timeout_seconds'] == 2400 and
            plan['minimum_decode_gain'] == .03, 'frozen four-arm scope')
    if executing and time.time() >= plan['task_deadline_unix']:
        raise TimeoutError('absolute deadline')
    for step in plan['steps']:
        require((step['http_requests'], step['http_loads'], step['timeout']) ==
                (17, 1, 600) and step['mode'] == 'normal', 'per-arm budget')
        require([(c['id'], c['count'], c['tokens'], c['parallel'])
                 for c in step['cases']] == CASES, 'case budget')
        command = [plan[step['arm']], 'serve', '--model-dir', plan['model'],
            '--host', '127.0.0.1', '--port', str(plan['port']), '--max-seq', '1',
            '--max-prefill', '8192', '--max-len', '208896', '--max-tokens', '256',
            '--startup-plan', 'off', '--mtp', '--mtp-k', '3']
        if step['head'] == 'subset':
            command += ['--mtp-subset-head', plan['artifact']]
        require(step['server_command'] == command, 'server configuration')


def acceptance(log):
    """Bind each histogram to its successful, single-slot request lifecycle."""
    active, result = None, {}
    for line in log.splitlines():
        fields = dict(re.findall(r'(\w+)=(\S+)', line))
        if line.startswith('[q4t][request] '):
            if fields.get('event') == 'admitted':
                require(active is None and fields['id'] not in result,
                        'overlapping or duplicate request')
                active = fields['id']
            elif fields.get('event') == 'end':
                require(active == fields['id'] and active in result and
                        fields['released'] == '1' and
                        fields['outcome'] == 'success', 'request completion')
                active = None
        if line.startswith('[mtp-accept] '):
            require(active == fields['request'] and active not in result,
                    'histogram identity')
            histogram = tuple(int(x) for x in fields['draft_counts'].split(','))
            require(len(histogram) == 4 and min(histogram) >= 0 and
                    sum(histogram) > 0, 'histogram values')
            result[active] = histogram
    require(active is None and len(result) == 17, 'histogram coverage')
    return result


def metrics(rows):
    intervals = [r['latency'] - r['ttft'] for r in rows]
    require(len(rows) == 3 and all(math.isfinite(t) and t > 0 for t in intervals),
            'decode intervals')
    return dict(aggregate_decode=sum(r['output_tokens'] - 1 for r in rows) /
                sum(intervals), aggregate_e2e=sum(r['latency'] for r in rows),
                ttft=[r['ttft'] for r in rows], latency=[r['latency'] for r in rows],
                decode=[(r['output_tokens'] - 1) / t for r, t in zip(rows, intervals)])


def analyze(plan, lease):
    validate(plan, executing=False)
    base.monitored_stage.require_complete_monitor(plan)
    legacy.verify_files(plan)
    guard = json.loads((lease / 'exit.json').read_text())
    require(guard['status'] == 'COMPLETED', 'outer GPU lease did not complete')
    output = Path(plan['output'])
    seal = json.loads((output / 'files.json').read_text())
    for name, digest in seal.items():
        require(sha(output / name) == digest, 'raw seal changed: ' + name)
    arms = {}
    for step in plan['steps']:
        folder = output / step['name']
        audit = http_stage.audit_server(folder, 17)
        hist = acceptance((folder / 'server.log').read_text())
        cases = json.loads((folder / 'results.json').read_text())
        require([c['id'] for c in cases] == [c[0] for c in CASES], 'case order')
        require({r['response_id'] for c in cases for r in c['rows']} == set(hist),
                'HTTP/server request coverage')
        trajectories = {}
        for case, (_, count, _, _) in zip(cases, CASES):
            require(len(case['rows']) == count, 'response count')
            trajectories[case['id']] = sorted(
                (r['prompt_sha256'], hist[r['response_id']]) for r in case['rows'])
        arms[(step['head'], step['arm'])] = dict(cases=cases, audit=audit,
            trajectories=trajectories, quality_passed=11,
            metrics={c['id']: metrics(c['rows']) for c in cases[:2]})
    comparisons = {}
    for head in ('full', 'subset'):
        parent, candidate = [arms[(head, arm)] for arm in ('parent', 'candidate')]
        same = all(base.identity(a) == base.identity(b) for a, b in
                   zip(parent['cases'], candidate['cases']))
        same_hist = parent['trajectories'] == candidate['trajectories']
        contexts = {}
        for name, *_ in CASES[:2]:
            p, c = [arm['metrics'][name] for arm in (parent, candidate)]
            gain = c['aggregate_decode'] / p['aggregate_decode'] - 1
            e2e = c['aggregate_e2e'] / p['aggregate_e2e'] - 1
            contexts[name] = dict(parent=p, candidate=c, decode_gain=gain,
                e2e_change=e2e, target_met=same and same_hist and
                gain >= plan['minimum_decode_gain'] and e2e < 0)
        comparisons[head] = dict(contexts=contexts, outputs_identical=same,
            acceptance_histograms_identical=same_hist,
            target_met=all(c['target_met'] for c in contexts.values()))
    require(all(c['outputs_identical'] and c['acceptance_histograms_identical']
                for c in comparisons.values()), 'numerical/output contract changed')
    result = dict(comparisons=comparisons, performance_accepted=False,
        arms={h + '-' + a: v for (h, a), v in arms.items()},
        statistics='DESCRIPTIVE_SINGLE_START_PER_HEAD_ARM_NOT_NONINFERIORITY',
        cold_request='Each arm starts at 1K; all three requests retained individually.',
        decision='GO_FOR_FULL_EVALUATION' if all(c['target_met'] for c in
            comparisons.values()) else 'NO_GO_FEASIBILITY_TARGET')
    save(output.parent / 'decision.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--step')
    parser.add_argument('--analyze-only', action='store_true')
    parser.add_argument('--lease', type=Path)
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text())
    if args.analyze_only:
        if args.lease is None:
            parser.error('--analyze-only requires completed --lease')
        result = analyze(plan, args.lease)
        print(json.dumps({k: result[k] for k in ('decision', 'comparisons')}, indent=2))
        return
    validate(plan)
    if args.step:
        http_stage.custom_step(plan, args.step)
    else:
        base.monitored_stage.run_stage(legacy, telemetry, plan, args.plan,
                                      audit_server=http_stage.audit_server)


if __name__ == '__main__':
    def interrupted(*_):
        raise KeyboardInterrupt('GDN input stage interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    main()
