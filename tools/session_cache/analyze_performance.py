"""Inspect the frozen C pairs; never infer population noninferiority."""
import hashlib
import json
from pathlib import Path

from run_stage import ROOT, save


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    base = ROOT / '.q4t-work/session-cache-20261027'
    source = base / 'phase-c-fixed-cap/plan.json'
    plan = json.loads(source.read_text())
    out = Path(plan['output'])
    state = json.loads((out / 'state.json').read_text())
    post = json.loads((out.parent / 'postflight.json').read_text())
    complete = (state['completed'] == [s['name'] for s in plan['steps']] and
                state['requests_completed'] == 90 and
                post['status'] == 'MEASURED_REQUIRES_REVIEW')
    pairs = []
    for head in ['full', 'subset']:
        arms = {s['cache']: s for s in plan['steps'] if s['head'] == head}
        for scenario in plan['scenarios']:
            rounds = {}
            for cached, step in arms.items():
                directory = out / step['name']
                exit_record = json.loads((directory / 'exit.json').read_text())
                audit = json.loads((directory / 'session-audit.json').read_text())
                complete &= exit_record['http_output_checks_passed'] and audit['passed']
                rounds[cached] = {r: json.loads((directory /
                    (scenario['id'] + '-' + r) / 'result.json').read_text())
                    for r in ['first', 'interference', 'second']}
            totals = {cached: sum(r['makespan'] for r in group.values())
                      for cached, group in rounds.items()}
            outputs = {cached: sum(r['total_output'] for r in group.values())
                       for cached, group in rounds.items()}
            if outputs != {False: 10240, True: 10240}:
                raise ValueError('unequal transaction work')
            reduction = 1 - totals[True] / totals[False]
            ttft = [dict(slot=i, off=rounds[False]['second']['rows'][i]['ttft'],
                         on=rounds[True]['second']['rows'][i]['ttft'])
                    for i in range(4)]
            for row in ttft:
                row['reduction'] = 1 - row['on'] / row['off']
            passed = (reduction >= plan['acceptance']['transaction_latency_reduction_min']
                      and all(r['reduction'] >=
                              plan['acceptance']['resume_ttft_reduction_min'] for r in ttft))
            pairs.append(dict(head=head, scenario=scenario['id'], passed=passed,
                off_seconds=totals[False], on_seconds=totals[True],
                latency_reduction=reduction, total_output_tokens=10240,
                off_tokens_per_second=10240/totals[False],
                on_tokens_per_second=10240/totals[True], resume_ttft=ttft,
                round_makespan={str(k):{r:v['makespan'] for r,v in g.items()}
                                for k,g in rounds.items()},
                reused_first_interference=head == 'full' and scenario['id']=='repeat-code'))
    result = dict(passed=bool(complete and all(p['passed'] for p in pairs)),
        complete=bool(complete), candidate_sha256=plan['candidate_sha256'],
        plan_sha256=sha(source), pairs=pairs,
        statistical_scope='One descriptive pair per head/scenario, no CI or '
            'population noninferiority claim. Full code OFF second is from a '
            'fresh service; its first/interference are reused with provenance.',
        protection_pending=True, release_authorized_by_this_gate=False)
    save(base / 'performance-gate.json', result)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
