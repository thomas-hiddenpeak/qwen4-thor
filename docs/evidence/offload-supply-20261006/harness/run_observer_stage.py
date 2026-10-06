"""One fixed observer service, no retries, original owned HTTP cleanup."""
import argparse
from pathlib import Path
import shutil
import time
from observer_common import R, O, owned, frozen, admitted, passed, read, save, run

parser = argparse.ArgumentParser()
parser.add_argument('group')
parser.add_argument('--plan-sha256', required=True)
args = parser.parse_args()
plan = frozen(args.plan_sha256)
admitted(plan, args.plan_sha256)
assert args.group in plan['group_ids']
index = plan['group_ids'].index(args.group)
if index == 0:
    assert not list(R.glob('observer-contracts*-start.json'))
    assert not (R / 'observer-contracts-decision.json').exists()
    assert not list(R.glob('s0*-controller-start.json'))
    save(R / 'observer-first-runtime-test-admission.json', {
        'plan_sha256':args.plan_sha256, 'group':args.group,
        'runtime_binary_sha256':plan['runtime_binary_sha256'],
        'no_new_contracts_or_diagnostics_started':True, 'recorded_t':time.time()})
if index:
    passed(plan['group_ids'][index-1], plan, args.plan_sha256)
    contracts = read(R / 'observer-contracts-decision.json')
    assert contracts['passed'] and contracts['plan_sha256'] == args.plan_sha256
    for source,digest in contracts['source_sha256'].items():
        assert owned.sha(source)==digest, source
command = read(R / (args.group + '-command.json'))
output = Path(command[command.index('--output')+1])
assert output == O / '.q4t-work/evidence' / args.group
assert not output.exists()
assert shutil.disk_usage(R).free >= 2 * 1024**3
assert not (R / (args.group + '-stage.json')).exists()
record = {'started_t':time.time(),'command':command,'returncode':None,
          'failure':None,'cleanup_complete':False}
try:
    record = run(command, R, args.group+'-controller', cwd=O,
                 interrupt_grace=120)
    frozen(args.plan_sha256)
except BaseException as error:
    exit_path = R / (args.group+'-controller-exit.json')
    if exit_path.exists():
        record=read(exit_path)
    record['failure']=type(error).__name__+': '+str(error)
    if record.get('pid') is not None:
        try:
            record['interrupt_unit_cleanup']=owned.cleanup_http_unit(command,record,plan)
        except BaseException as cleanup_error:
            record['interrupt_cleanup_failure']=str(cleanup_error)
    raise
finally:
    record.update(ended_t=time.time(),group_id=args.group,group_index=index,
        runtime_binary_sha256=plan['runtime_binary_sha256'],
        runtime_source_commit=plan['runtime_source_commit'],
        plan_sha256=args.plan_sha256,first_runtime_test=index==0,
        automatic_retry=False,performance_acceptance=False)
    save(R/(args.group+'-stage.json'),record)
    print({k:record[k] for k in ('group_id','returncode','failure','cleanup_complete')},flush=True)
raise SystemExit(record['returncode'] or 0)
