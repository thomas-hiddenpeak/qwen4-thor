"""Run one fixed HTTP service, preserving first outcomes and owned cleanup."""
import argparse
from pathlib import Path
import shutil
import time

from recycle_common import (R, W, owned, read, save, sha, run, frozen, admitted,
                            passed)

parser = argparse.ArgumentParser()
parser.add_argument('group')
parser.add_argument('--plan-sha256', required=True)
args = parser.parse_args()
plan = frozen(args.plan_sha256)
admitted(plan, args.plan_sha256)
assert args.group in plan['group_ids']
index = plan['group_ids'].index(args.group)
if index == 0:
    assert not list(R.glob('*-controller-start.json'))
    assert not (R / 'host-01').exists() and not (R / 'numerical-01').exists()
    save(R / 'first-test-admission.json', {
        'plan_sha256': args.plan_sha256, 'group': args.group,
        'runtime_binary_sha256': plan['runtime_binary_sha256'],
        'scope': 'Owned candidate execution entrypoints; not whole-machine absence',
        'known_prior_non_HTTP_tests': [], 'recorded_t': time.time()})
else:
    passed(plan['group_ids'][index - 1], plan, args.plan_sha256)
    passed('host', plan, args.plan_sha256)
    passed('numerical', plan, args.plan_sha256)
    if plan['groups'][index]['kind'] == 'matrix':
        history = read(R / 'history-decision.json')
        assert history['plan_sha256'] == args.plan_sha256
        assert history['evidence_contracts_passed']
        assert history['matrix_coverage_allowed']
        assert type(history['passed']) is bool
        assert history['runtime_source_commit'] == plan['runtime_source_commit']
        assert history['runtime_binary_sha256'] == plan['runtime_binary_sha256']
        assert history['decision'] == ('PASS_SCREENING' if history['passed'] else 'NO_GO')
        assert history['ended_t'] <= history['recorded_t'] <= time.time()
        for path, digest in history['source_sha256'].items():
            assert sha(path) == digest, path
assert sha(R / (args.group + '-command.json')) == plan['command_sha256'][args.group]
command = read(R / (args.group + '-command.json'))
output = Path(command[command.index('--output') + 1])
assert output == W / '.q4t-work/evidence' / args.group
assert not output.exists()
assert shutil.disk_usage(R).free >= plan['min_free_disk_bytes']
record = {'started_t': time.time(), 'command': command, 'returncode': None,
          'failure': None, 'cleanup_complete': False}
try:
    record = run(command, R, args.group + '-controller', cwd=W,
                 interrupt_grace=120)
    frozen(args.plan_sha256)
except BaseException as error:
    exit_path = R / (args.group + '-controller-exit.json')
    if exit_path.exists():
        record = read(exit_path)
    record['failure'] = type(error).__name__ + ': ' + str(error)
    if record.get('pid') is not None:
        try:
            record['interrupt_unit_cleanup'] = owned.cleanup_http_unit(
                command, record, plan)
        except BaseException as cleanup_error:
            record['interrupt_cleanup_failure'] = str(cleanup_error)
    raise
finally:
    record.update(ended_t=time.time(), group_id=args.group, group_index=index,
        runtime_binary_sha256=plan['runtime_binary_sha256'],
        runtime_source_commit=plan['runtime_source_commit'],
        plan_sha256=args.plan_sha256, first_runtime_test=index == 0,
        automatic_retry=False, performance_acceptance=False)
    save(R / (args.group + '-stage.json'), record)
    print({k: record[k] for k in ('group_id', 'returncode', 'failure',
                                 'cleanup_complete')}, flush=True)
raise SystemExit(record['returncode'] or 0)
