"""Execute exactly one predeclared HTTP stage, without automatic retries."""
import argparse
import time

from phase_common import (R, cleanup_http_unit, frozen, prerequisite, read,
                          run, save, sha)

parser = argparse.ArgumentParser()
parser.add_argument('stage', choices=['quality-c', 'history-a',
    'history-b', 'history-c', 'matrix-a', 'matrix-c'])
parser.add_argument('--plan-sha256', required=True)
args = parser.parse_args()
plan = frozen(args.plan_sha256)
stage = args.stage
if stage != 'quality-c':
    prerequisite('quality', plan)
    prerequisite('host', plan)
    prerequisite('numerical', plan)
if stage in ('history-b', 'history-c'):
    prerequisite('history-a', plan)
if stage == 'history-c':
    prerequisite('history-b', plan)
if stage.startswith('matrix-'):
    # A valid speed rejection permits only the already frozen matrix coverage.
    # All three history arms must have passed non-speed contracts first.
    for label in ('a', 'b', 'c'):
        prerequisite('history-' + label, plan)
    decision_path = R / 'history-decision.json'
    decision = read(decision_path)
    assert decision['evidence_contracts_passed'] is True
    assert decision['acceptance_comparison'] == ['A', 'C']
    assert type(decision['passed']) is bool
    assert decision['decision'] == (
        'PASS_SCREENING' if decision['passed'] else 'NO_GO')
    assert decision['matrix_coverage_allowed'] is True
    assert decision['plan_sha256'] == args.plan_sha256
    assert decision['runtime_binary_sha256'] == plan['runtime_binary_sha256']
    assert decision['runtime_source_commit'] == plan['runtime_source_commit']
    assert decision['ended_t'] <= decision['recorded_t'] <= time.time()
    for path, digest in decision['source_sha256'].items():
        assert sha(path) == digest, path
if stage == 'matrix-c':
    prerequisite('matrix-a', plan)
command_path = R / (stage + '-command.json')
assert sha(command_path) == plan['command_sha256'][stage]
for suffix in ('-controller-start.json', '-controller-exit.json', '-stage.json'):
    assert not (R / (stage + suffix)).exists()
if stage == 'quality-c':
    # This controls this phase's entrypoints, not unobservable machine-wide work.
    forbidden = [R / 'host-01', R / 'numerical-01']
    forbidden += list(R.glob('*-controller-start.json'))
    forbidden += list(R.glob('*-first-attempt.json'))
    assert not any(p.exists() for p in forbidden)
    save(R / 'first-test-admission.json', {
        'scope': 'this phase controller artifacts; no machine-wide absence claim',
        'phase_created_t': read(R / 'plan.json')['frozen_at'],
        'admitted_t': time.time(), 'stage': stage,
        'plan_sha256': args.plan_sha256,
        'existing_parent_artifacts_mtime_ns': {
            p.name: p.stat().st_mtime_ns for p in sorted(R.glob('*.json'))},
        'known_non_http_test_attempts': [],
    })
command = read(command_path)
record = {'started_t': time.time(), 'command': command, 'returncode': None,
          'failure': None}
try:
    # The wrapper owns its timeout and exact unit. No competing outer timeout.
    record = run(command, R, stage + '-controller', interrupt_grace=120)
    frozen(args.plan_sha256)
except BaseException as error:
    exit_path = R / (stage + '-controller-exit.json')
    if exit_path.exists():
        record = read(exit_path)
    record['failure'] = type(error).__name__ + ': ' + str(error)
    if record.get('pid') is not None:
        try:
            record['interrupt_unit_cleanup'] = cleanup_http_unit(command, record, plan)
        except BaseException as cleanup_error:
            record['interrupt_cleanup_failure'] = (
                type(cleanup_error).__name__ + ': ' + str(cleanup_error))
    raise
finally:
    record.update(ended_t=time.time(),
        runtime_binary_sha256=plan['runtime_binary_sha256'],
        runtime_source_commit=plan['runtime_source_commit'],
        first_test=stage == 'quality-c', plan_sha256=args.plan_sha256)
    save(R / (stage + '-stage.json'), record)
    print(record, flush=True)
raise SystemExit(record['returncode'] or 0)
