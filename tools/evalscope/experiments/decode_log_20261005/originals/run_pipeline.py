"""Run the prospectively frozen phase once; valid speed NO_GO is terminal data."""
import argparse
import time

from phase_common import R, frozen, read, run, save, sha

parser = argparse.ArgumentParser()
parser.add_argument('--plan-sha256', required=True)
args = parser.parse_args()
plan = frozen(args.plan_sha256)
directory = R / 'pipeline-execution'
assert not directory.exists()
directory.mkdir()
save(directory / 'start.json', {
    'started_t': time.time(), 'plan_sha256': args.plan_sha256,
    'controller_sha256': sha(__file__), 'automatic_retry': False,
    'conditional_model_requests_automatic': False})

steps = [('quality-c-run', 'run_stage.py', 'quality-c', (0,)),
         ('quality-c-audit', 'audit_group.py', 'quality-c', (0,)),
         ('host', 'run_direct_contracts.py', 'host', (0,)),
         ('numerical', 'run_direct_contracts.py', 'numerical', (0,))]
for stage in ('history-a', 'history-b', 'history-c'):
    steps += [(stage + '-run', 'run_stage.py', stage, (0,)),
              (stage + '-audit', 'audit_group.py', stage, (0,))]
steps += [('history-compare', 'compare_groups.py', 'history', (0, 3))]
for stage in ('matrix-a', 'matrix-c'):
    steps += [(stage + '-run', 'run_stage.py', stage, (0,)),
              (stage + '-audit', 'audit_group.py', stage, (0,))]
steps += [('matrix-compare', 'compare_groups.py', 'matrix', (0, 3)),
          ('aggregate', 'compare_groups.py', 'aggregate', (0, 3))]

completed = []
failure = None
decision = None
try:
    for index, (label, script, argument, allowed_codes) in enumerate(steps, 1):
        command = ['python3', '-B', str(R / script), argument,
                   '--plan-sha256', args.plan_sha256]
        print(f'Start {index}/{len(steps)}: {label}', flush=True)
        record = run(command, directory, f'step-{index:02d}-{label}',
                     interrupt_grace=180)
        assert record['returncode'] in allowed_codes, label + ' failed'
        assert record['failure'] is None and record['cleanup_complete'], label
        completed.append({'step': index, 'label': label,
                          'returncode': record['returncode'],
                          'ended_t': record['ended_t']})
        print(f'Completed {label}: rc={record["returncode"]}', flush=True)
    decision = read(R / 'performance-decision.json')
    assert decision['evidence_contracts_passed'] is True
    assert decision['decision'] in ('PASS_SCREENING', 'NO_GO_PERFORMANCE')
    assert decision['automatic_conditional_execution'] is False
    assert decision['second_candidate_allowed'] is False
except BaseException as error:
    failure = type(error).__name__ + ': ' + str(error)
    raise
finally:
    save(directory / 'exit.json', {
        'ended_t': time.time(), 'completed': len(completed) == len(steps),
        'completed_steps': completed, 'failure': failure,
        'decision': None if decision is None else decision['decision'],
        'plan_sha256': args.plan_sha256,
        'runtime_source_commit': plan['runtime_source_commit'],
        'runtime_binary_sha256': plan['runtime_binary_sha256'],
        'next_model_stage_automatically_allowed': False,
        'scope': 'Pipeline success means frozen collection and decisions '
                 'completed, not candidate acceptance.'})
print('Frozen HTTP/direct/performance collection complete: ' +
      decision['decision'], flush=True)
