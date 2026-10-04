"""Run the original six-tier commands under the prospective coverage addendum.

A valid history speed NO_GO still rejects the candidate. These runs complete
the Goal's requested coverage and can never cancel that failure.
"""
import argparse
import sys
import time

from phase_common import (R, W, cleanup_http_unit, frozen, prerequisite, read,
                          run, save, sha)

parser = argparse.ArgumentParser()
parser.add_argument('stage', choices=['matrix-off', 'matrix-on'])
parser.add_argument('--plan-sha256', required=True)
parser.add_argument('--amendment-sha256', required=True)
args = parser.parse_args()
plan = frozen(args.plan_sha256)
amendment_path = R / 'coverage-amendment.json'
def checked_amendment():
    assert sha(amendment_path) == args.amendment_sha256
    amendment = read(amendment_path)
    assert amendment['status'] == 'FROZEN_ACTIVE'
    assert amendment['original_execution_plan_sha256'] == args.plan_sha256
    assert amendment['original_phase_plan_sha256'] == plan['phase_plan_sha256']
    assert amendment['controller_sha256'] == sha(__file__)
    assert amendment['samples_thresholds_resources_unchanged'] is True
    assert amendment['history_failure_permanently_vetoes_overall_GO'] is True
    phase = read(R / 'plan.json')
    assert amendment['matrix_lengths'] == phase['full_matrix']['lengths']
    assert amendment['repeats'] == phase['full_matrix']['repeats'] == 3
    assert amendment['group_order'] == ['matrix-off', 'matrix-on']
    for sources in ('original_files', 'source_sha256'):
        for path, digest in amendment[sources].items():
            assert sha(path) == digest, path
    return amendment


amendment = checked_amendment()
assert amendment['proposed_at'] <= amendment['frozen_at']
assert read(R / 'quality-decision.json')['ended_t'] < amendment['frozen_at']
assert read(R / 'inheritance-on-controller-start.json')['started_t'] > amendment['frozen_at']
for name in ['quality', 'host', 'numerical', 'inheritance-off', 'inheritance-on']:
    prerequisite(name, plan)
history_path = R / 'inheritance-decision.json'
history_sha = sha(history_path)
history = read(history_path)
assert type(history['passed']) is bool
assert history['evidence_contracts_passed'] is True
assert history['runtime_binary_sha256'] == plan['runtime_binary_sha256']
assert history['runtime_source_commit'] == plan['runtime_source_commit']
assert history['plan_sha256'] == args.plan_sha256
for path, digest in history['source_sha256'].items():
    assert sha(path) == digest, path
sys.path.insert(0, str(W / 'tools/evalscope'))
from request_policy_protocol import sequence_metrics
recomputed = sequence_metrics(
    read(R / 'inheritance-off-decision.json')['metrics'],
    read(R / 'inheritance-on-decision.json')['metrics'])
assert recomputed['passed'] == history['passed']
assert recomputed['positions'] == history['positions']
assert history['decision'] == ('PASS_SCREENING' if history['passed'] else 'NO_GO')
stage = args.stage
if stage == 'matrix-on':
    prerequisite('matrix-off', plan)
    previous = read(R / 'matrix-off-coverage-admission.json')
    assert previous['amendment_sha256'] == args.amendment_sha256
    assert previous['history_decision_sha256'] == history_sha
    prior_stage = read(R / 'matrix-off-stage.json')
    assert prior_stage['amendment_sha256'] == args.amendment_sha256
    assert prior_stage['returncode'] == 0 and prior_stage['failure'] is None
else:
    assert not (R / 'matrix-on-controller-start.json').exists()
    assert not (R / 'matrix-on-coverage-admission.json').exists()
command_path = R / (stage + '-command.json')
assert sha(command_path) == plan['command_sha256'][stage]
for suffix in ['-controller-start.json', '-controller-exit.json', '-stage.json']:
    assert not (R / (stage + suffix)).exists()
save(R / (stage + '-coverage-admission.json'), {
    'admitted_t': time.time(), 'plan_sha256': args.plan_sha256,
    'amendment_sha256': args.amendment_sha256,
    'history_decision_sha256': history_sha,
    'history_decision': history['decision'],
    'overall_GO_still_possible': history['passed'],
    'history_failure_permanently_vetoes_overall_GO': True,
    'history_failure_present': not history['passed'],
    'authorizes_business_lifecycle_or_next_candidate': False,
    'purpose': 'original six-tier coverage; does not override history failure',
    'automatic_retry': False})
command = read(command_path)
record = {'started_t': time.time(), 'command': command, 'returncode': None,
          'failure': None}
try:
    record = run(command, R, stage + '-controller', interrupt_grace=120)
    frozen(args.plan_sha256)
    checked_amendment()
    assert sha(history_path) == history_sha
except BaseException as error:
    path = R / (stage + '-controller-exit.json')
    if path.exists():
        record = read(path)
    record['failure'] = type(error).__name__ + ': ' + str(error)
    if record.get('pid') is not None:
        try:
            record['interrupt_unit_cleanup'] = cleanup_http_unit(command, record, plan)
        except BaseException as cleanup_error:
            record['interrupt_cleanup_failure'] = type(cleanup_error).__name__ + ': ' + str(cleanup_error)
    raise
finally:
    record.update(ended_t=time.time(), plan_sha256=args.plan_sha256,
        amendment_sha256=args.amendment_sha256, first_test=False,
        runtime_binary_sha256=plan['runtime_binary_sha256'],
        runtime_source_commit=plan['runtime_source_commit'],
        history_decision_sha256=history_sha,
        history_failure_permanently_vetoes_overall_GO=True,
        history_failure_present=not history['passed'],
        authorizes_business_lifecycle_or_next_candidate=False)
    save(R / (stage + '-stage.json'), record)
    print(record, flush=True)
raise SystemExit(record['returncode'] or 0)
