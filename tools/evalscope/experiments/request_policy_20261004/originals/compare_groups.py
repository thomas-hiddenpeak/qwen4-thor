"""Apply the already frozen gates to completed off/on groups, without reruns."""
import argparse
import sys
import time

from phase_common import R, W, frozen, prerequisite, save, sha

parser = argparse.ArgumentParser()
parser.add_argument('kind', choices=['inheritance', 'matrix'])
parser.add_argument('--plan-sha256', required=True)
args = parser.parse_args()
plan = frozen(args.plan_sha256)
before = prerequisite(args.kind + '-off', plan)
after = prerequisite(args.kind + '-on', plan)
assert before['ended_t'] < after['ended_t']
sys.path.insert(0, str(W / 'tools/evalscope'))
from request_policy_protocol import sequence_metrics

if args.kind == 'inheritance':
    result = sequence_metrics(before['metrics'], after['metrics'])
else:
    tiers = []
    for length in plan['matrix_lengths']:
        old = [r for r in before['metrics'] if r['input_tokens'] == length]
        new = [r for r in after['metrics'] if r['input_tokens'] == length]
        assert len(old) == len(new) == 3
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
sources = {**before['source_sha256'], **after['source_sha256']}
for label in ('off', 'on'):
    path = R / (args.kind + '-' + label + '-decision.json')
    sources[str(path)] = sha(path)
sources[__file__] = sha(__file__)
result.update(schema=1, decision='PASS_SCREENING' if result['passed'] else 'NO_GO',
    ended_t=after['ended_t'], recorded_t=time.time(),
    runtime_binary_sha256=plan['runtime_binary_sha256'],
    runtime_source_commit=plan['runtime_source_commit'],
    plan_sha256=args.plan_sha256, source_sha256=sources,
    evidence_contracts_passed=True, prior_full_NO_GO_unchanged=True,
    later_model_requests_allowed=result['passed'],
    whole_physical_RAM_54GB='INDETERMINATE')
save(R / (args.kind + '-decision.json'), result)
print({k: v for k, v in result.items() if k not in ('source_sha256',)})
raise SystemExit(0 if result['passed'] else 3)
