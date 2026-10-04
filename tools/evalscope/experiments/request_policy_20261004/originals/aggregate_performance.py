"""Overall speed gate: history AND six-tier; neither can rescue the other."""
import argparse
import sys
import time

from phase_common import R, W, frozen, prerequisite, read, save, sha

parser = argparse.ArgumentParser()
parser.add_argument('--plan-sha256', required=True)
parser.add_argument('--amendment-sha256', required=True)
args = parser.parse_args()
plan = frozen(args.plan_sha256)
assert sha(R / 'coverage-amendment.json') == args.amendment_sha256
amendment = read(R / 'coverage-amendment.json')
assert amendment['status'] == 'FROZEN_ACTIVE'
assert amendment['original_execution_plan_sha256'] == args.plan_sha256
assert amendment['original_phase_plan_sha256'] == plan['phase_plan_sha256']
assert amendment['samples_thresholds_resources_unchanged'] is True
assert amendment['controller_sha256'] == sha(R / 'run_coverage_stage.py')
assert amendment['source_sha256'][str(R / 'aggregate_performance.py')] == sha(__file__)
assert amendment['history_failure_permanently_vetoes_overall_GO'] is True
for mapping in ['original_files', 'source_sha256']:
    for path, digest in amendment[mapping].items():
        assert sha(path) == digest, path
groups = {name: prerequisite(name, plan) for name in
    ['quality', 'host', 'numerical', 'inheritance-off', 'inheritance-on',
     'matrix-off', 'matrix-on']}
sources = {str(R / 'coverage-amendment.json'): args.amendment_sha256,
           __file__: sha(__file__)}
for name, group in groups.items():
    sources[str(R / (name + '-decision.json'))] = sha(
        R / (name + '-decision.json'))
    sources.update(group.get('source_sha256', {}))
    sources.update(group.get('log_sha256', {}))
decisions = {}
for name in ['inheritance', 'matrix']:
    path = R / (name + '-decision.json')
    decision = read(path)
    assert type(decision['passed']) is bool
    assert decision['evidence_contracts_passed'] is True
    assert decision['plan_sha256'] == args.plan_sha256
    assert decision['runtime_binary_sha256'] == plan['runtime_binary_sha256']
    assert decision['runtime_source_commit'] == plan['runtime_source_commit']
    assert decision['decision'] == ('PASS_SCREENING' if decision['passed'] else 'NO_GO')
    for evidence, digest in decision['source_sha256'].items():
        assert sha(evidence) == digest, evidence
    sources.update(decision['source_sha256'])
    sources[str(path)] = sha(path)
    decisions[name] = decision
for stage in ['matrix-off', 'matrix-on']:
    path = R / (stage + '-coverage-admission.json')
    admission = read(path)
    assert admission['amendment_sha256'] == args.amendment_sha256
    assert admission['plan_sha256'] == args.plan_sha256
    assert admission['history_decision_sha256'] == sha(R / 'inheritance-decision.json')
    stage_path = R / (stage + '-stage.json')
    record = read(stage_path)
    assert record['amendment_sha256'] == args.amendment_sha256
    assert record['returncode'] == 0 and record['failure'] is None
    assert record['history_failure_permanently_vetoes_overall_GO'] is True
    assert record['history_failure_present'] is (not decisions['inheritance']['passed'])
    assert record['authorizes_business_lifecycle_or_next_candidate'] is False
    sources[str(path)] = sha(path)
    sources[str(stage_path)] = sha(stage_path)
sys.path.insert(0, str(W / 'tools/evalscope'))
from request_policy_protocol import sequence_metrics
history = sequence_metrics(groups['inheritance-off']['metrics'],
                           groups['inheritance-on']['metrics'])
assert history['positions'] == decisions['inheritance']['positions']
assert history['passed'] == decisions['inheritance']['passed']
tiers = []
for length in plan['matrix_lengths']:
    old = [r for r in groups['matrix-off']['metrics'] if r['input_tokens'] == length]
    new = [r for r in groups['matrix-on']['metrics'] if r['input_tokens'] == length]
    assert len(old) == len(new) == 3
    checks = {'first_ttft': new[0]['ttft'] <= old[0]['ttft'],
        'later_ttft': max(r['ttft'] for r in new[1:]) <= max(r['ttft'] for r in old[1:]),
        'decode': min(r['decode_tps'] for r in new) >= min(r['decode_tps'] for r in old)}
    tiers.append({'input_tokens': length, 'checks': checks,
                  'passed': all(checks.values()), 'baseline': old, 'candidate': new})
assert tiers == decisions['matrix']['tiers']
assert all(r['passed'] for r in tiers) == decisions['matrix']['passed']
passed = history['passed'] and all(r['passed'] for r in tiers)
report = {'schema': 1, 'passed': passed,
    'decision': 'PASS_ALL_SPEED_GATES' if passed else 'NO_GO_PERFORMANCE',
    'history_passed': history['passed'], 'six_tier_passed': all(r['passed'] for r in tiers),
    'history_positions': history['positions'], 'six_tier': tiers,
    'ended_t': groups['matrix-on']['ended_t'], 'recorded_t': time.time(),
    'runtime_binary_sha256': plan['runtime_binary_sha256'],
    'runtime_source_commit': plan['runtime_source_commit'],
    'plan_sha256': args.plan_sha256, 'amendment_sha256': args.amendment_sha256,
    'source_sha256': sources, 'evidence_contracts_passed': True,
    'business_lifecycle_allowed': passed, 'next_optimization_allowed': False,
    'history_failure_permanently_vetoes_overall_GO': True,
    'history_failure_present': not history['passed'],
    'runtime_acceptance': False, 'whole_physical_RAM_54GB': 'INDETERMINATE',
    'local_legacy_later_model_requests_flags_are_not_authorization': True}
save(R / 'performance-decision.json', report)
print({k: v for k, v in report.items()
       if k not in ['source_sha256', 'history_positions', 'six_tier']})
raise SystemExit(0 if passed else 3)
