"""Report all D measurements and negative observations without a noise band."""
import importlib.util
import json
from pathlib import Path

from run_stage import ROOT, save

spec = importlib.util.spec_from_file_location(
    'existing_comparisons', ROOT / 'tools/mtp_suffix_agent/analyze.py')
existing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(existing)


def main():
    base = ROOT / '.q4t-work/session-cache-20261027'
    plan = json.loads((base / 'phase-d/plan.json').read_text())
    root = Path(plan['output'])
    state = json.loads((root / 'state.json').read_text())
    post = json.loads((root.parent / 'postflight.json').read_text())
    complete = (state['completed'] == [s['name'] for s in plan['steps']] and
                state['requests_completed'] == 94 and
                post['status'] == 'MEASURED_REQUIRES_REVIEW')
    data = {}
    recalls = []
    for step in plan['steps']:
        directory = root / step['name']
        data[step['name']] = {c['id']: c for c in
            json.loads((directory / 'results.json').read_text())}
        exit_record = json.loads((directory / 'exit.json').read_text())
        audit = json.loads((directory / 'session-audit.json').read_text())
        complete &= exit_record['http_output_checks_passed'] and audit['passed']
        if step['cache']:
            recall = json.loads((directory / 'memory-quality.json').read_text())
            recalls.append(dict(head=step['head'], passed=recall['passed'],
                                cached_tokens=recall['cached_tokens']))
    pairs = []
    for head in ['full', 'subset', 'plain']:
        arms = {s['binary'] == plan['candidate']: s for s in plan['steps']
                if s['head'] == head}
        for case in plan['protection']:
            if case['id'] == 'session-memory':
                continue
            a = data[arms[False]['name']][case['id']]
            b = data[arms[True]['name']][case['id']]
            pairs.append(dict(head=head, scenario=case['id'],
                              **existing.compare(a, b)))
    negatives = [dict(head=r['head'], scenario=r['scenario'],
                     e2e_percent=r['e2e_percent'], decode_percent=r['decode_percent'],
                     ttft_percent=r['ttft_percent']) for r in pairs
                 if r['e2e_percent'] < 0 or r['decode_percent'] < 0 or r['ttft_percent'] > 0]
    result = dict(complete=bool(complete), pairs=pairs, recalls=recalls,
        all_outputs_identical=all(r['output_same'] for r in pairs),
        cross_turn_recall_passed=len(recalls)==2 and all(r['passed'] for r in recalls),
        negative_observations=negatives, statistical_noninferiority='NOT_ESTABLISHED',
        causal_regression='REQUIRES_REVIEW_OF_OBSERVATIONS_AND_EXECUTION_PATH',
        universal_tolerance=None, automatic_release_decision=False,
        metric_definition='E2E total output / group makespan; decode '
        'sum(output-1)/sum(latency-TTFT); TTFT arithmetic mean. Three requests '
        'per tier, one startup per identity/mode. Negative observations retained.')
    save(base / 'protection-comparisons.json', result)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
