"""Frozen A/C gates and separate B/C description; never run more requests."""
import argparse
import statistics
import sys
import time

from phase_common import R, W, frozen, prerequisite, read, save, sha

parser = argparse.ArgumentParser()
parser.add_argument('kind', choices=['history', 'matrix', 'aggregate'])
parser.add_argument('--plan-sha256', required=True)
args = parser.parse_args()
plan = frozen(args.plan_sha256)
phase = read(R / 'plan.json')
sys.path.insert(0, str(W / 'tools/evalscope'))
from request_policy_protocol import sequence_metrics, sequence_plan

assert plan['matrix_lengths'] == phase['matrix']['lengths']
assert sequence_plan()['sequence'] == phase['history']['sequence']
assert sequence_plan()['rounds'] == phase['history']['rounds']


def merged_sources(*reports):
    sources = {}
    for report in reports:
        for path, digest in report['source_sha256'].items():
            assert path not in sources or sources[path] == digest, path
            sources[path] = digest
    sources[__file__] = sha(__file__)
    return sources


def bind_decisions(sources, *names):
    for name in names:
        path = R / (name + '-decision.json')
        sources[str(path)] = sha(path)


def speed_decision(name):
    """A speed NO_GO is valid evidence, never a prerequisite PASS."""
    record = read(R / (name + '-decision.json'))
    assert record['evidence_contracts_passed'] is True
    assert record['acceptance_comparison'] == ['A', 'C']
    assert type(record['passed']) is bool
    assert record['decision'] == (
        'PASS_SCREENING' if record['passed'] else 'NO_GO')
    assert record['runtime_binary_sha256'] == plan['runtime_binary_sha256']
    assert record['runtime_source_commit'] == plan['runtime_source_commit']
    assert record['plan_sha256'] == args.plan_sha256
    assert record['ended_t'] <= record['recorded_t'] <= time.time()
    for path, digest in record['source_sha256'].items():
        assert sha(path) == digest, path
    return record


def metric_difference(before, after):
    return {'B': before, 'C': after, 'C_minus_B': after - before,
            'percent_change': (after / before - 1) * 100}


if args.kind == 'aggregate':
    for name in ('quality', 'host', 'numerical'):
        prerequisite(name, plan)
    groups = [prerequisite(name, plan) for name in
              ('history-a', 'history-b', 'history-c', 'matrix-a', 'matrix-c')]
    history = speed_decision('history')
    matrix = speed_decision('matrix')
    assert history['ended_t'] < matrix['ended_t']
    passed = history['passed'] and matrix['passed']
    sources = merged_sources(history, matrix, *groups)
    bind_decisions(sources, 'history', 'matrix', 'quality', 'host', 'numerical',
                   'history-a', 'history-b', 'history-c', 'matrix-a', 'matrix-c')
    result = {
        'schema': 1, 'passed': passed,
        'decision': 'PASS_SCREENING' if passed else 'NO_GO_PERFORMANCE',
        'scope': 'A/C history AND six-tier matrix',
        'acceptance_comparison': ['A', 'C'],
        'history_passed': history['passed'], 'matrix_passed': matrix['passed'],
        'evidence_contracts_passed': True,
        'history_failure_is_permanent_veto': True,
        'diagnostic_B_C_cannot_override_acceptance': True,
        'conditional_acceptance_checks_eligible': passed,
        'automatic_conditional_execution': False,
        'second_candidate_allowed': False,
        'runtime_acceptance': False,
        'performance_acceptance': passed,
        'ended_t': matrix['ended_t'], 'recorded_t': time.time(),
        'runtime_binary_sha256': plan['runtime_binary_sha256'],
        'runtime_source_commit': plan['runtime_source_commit'],
        'plan_sha256': args.plan_sha256, 'source_sha256': sources,
        'prior_full_NO_GO_unchanged': True,
        'whole_physical_RAM_54GB': 'INDETERMINATE'}
    save(R / 'performance-decision.json', result)
else:
    before = prerequisite(args.kind + '-a', plan)
    after = prerequisite(args.kind + '-c', plan)
    assert before['ended_t'] < after['ended_t']
    sources = merged_sources(before, after)
    bind_decisions(sources, args.kind + '-a', args.kind + '-c')
    if args.kind == 'history':
        middle = prerequisite('history-b', plan)
        assert before['ended_t'] < middle['ended_t'] < after['ended_t']
        result = sequence_metrics(before['metrics'], after['metrics'])
        # Validate B ordering with the canonical sequence contract. Its gates
        # are deliberately not used or exported as an acceptance decision.
        described = sequence_metrics(middle['metrics'], after['metrics'])
        descriptions = []
        for item in described['positions']:
            b, c = item['baseline'], item['candidate']
            descriptions.append({
                'position': item['position'], 'input_tokens': item['input_tokens'],
                'B_samples': b, 'C_samples': c,
                'mean_ttft_seconds': metric_difference(
                    statistics.mean(r['ttft'] for r in b),
                    statistics.mean(r['ttft'] for r in c)),
                'first_ttft_seconds': metric_difference(b[0]['ttft'], c[0]['ttft']),
                'later_max_ttft_seconds': metric_difference(
                    max(r['ttft'] for r in b[1:]), max(r['ttft'] for r in c[1:])),
                'mean_decode_tps': metric_difference(
                    statistics.mean(r['decode_tps'] for r in b),
                    statistics.mean(r['decode_tps'] for r in c)),
                'minimum_decode_tps': metric_difference(
                    min(r['decode_tps'] for r in b), min(r['decode_tps'] for r in c))})
        diagnostic_sources = merged_sources(middle, after)
        bind_decisions(diagnostic_sources, 'history-b', 'history-c')
        diagnostic = {
            'schema': 1, 'comparison': ['B', 'C'], 'decision': 'DESCRIPTIVE_ONLY',
            'positions': descriptions, 'performance_acceptance': False,
            'causal_limit': phase['history']['causal_limit'],
            'pure_fprintf_time_estimate': None, 'log_walltime_upper_bound': None,
            'ended_t': after['ended_t'], 'recorded_t': time.time(),
            'runtime_binary_sha256': plan['runtime_binary_sha256'],
            'runtime_source_commit': plan['runtime_source_commit'],
            'plan_sha256': args.plan_sha256, 'source_sha256': diagnostic_sources}
        diagnostic_path = R / 'history-bc-diagnostic.json'
        save(diagnostic_path, diagnostic)
        sources = merged_sources(before, middle, after)
        bind_decisions(sources, 'history-a', 'history-b', 'history-c')
        sources[str(diagnostic_path)] = sha(diagnostic_path)
    else:
        # Matrix is frozen coverage even if the valid history speed gate failed.
        history = speed_decision('history')
        assert history['ended_t'] < before['ended_t']
        sources = merged_sources(before, after, history)
        bind_decisions(sources, 'matrix-a', 'matrix-c', 'history')
        tiers = []
        for length in plan['matrix_lengths']:
            old = [r for r in before['metrics'] if r['input_tokens'] == length]
            new = [r for r in after['metrics'] if r['input_tokens'] == length]
            assert len(old) == len(new) == phase['matrix']['repeats'] == 3
            checks = {
                'first_ttft': new[0]['ttft'] <= old[0]['ttft'],
                'later_ttft': max(r['ttft'] for r in new[1:]) <=
                              max(r['ttft'] for r in old[1:]),
                'decode': min(r['decode_tps'] for r in new) >=
                          min(r['decode_tps'] for r in old)}
            tiers.append({'input_tokens': length, 'checks': checks,
                          'passed': all(checks.values()), 'baseline': old,
                          'candidate': new})
        result = {'passed': all(r['passed'] for r in tiers), 'tiers': tiers,
                  'scope': 'six_tier', 'performance_acceptance': False}
    result.update(
        schema=1, decision='PASS_SCREENING' if result['passed'] else 'NO_GO',
        acceptance_comparison=['A', 'C'],
        ended_t=after['ended_t'], recorded_t=time.time(),
        runtime_binary_sha256=plan['runtime_binary_sha256'],
        runtime_source_commit=plan['runtime_source_commit'],
        plan_sha256=args.plan_sha256, source_sha256=sources,
        evidence_contracts_passed=True, prior_full_NO_GO_unchanged=True,
        matrix_coverage_allowed=args.kind == 'history',
        history_failure_is_permanent_veto=True,
        diagnostic_B_C_cannot_override_acceptance=True,
        automatic_conditional_execution=False, second_candidate_allowed=False,
        whole_physical_RAM_54GB='INDETERMINATE')
    save(R / (args.kind + '-decision.json'), result)
print({k: v for k, v in result.items() if k != 'source_sha256'})
raise SystemExit(0 if result['passed'] else 3)
