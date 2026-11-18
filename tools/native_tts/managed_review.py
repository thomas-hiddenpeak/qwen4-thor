"""Read-only correction of a completed voice run's lazy-encoder assertion.

The original FAILED result and COMMAND_FAILED lease remain immutable. Only
this exact late assertion, complete work, healthy monitoring and verified
cleanup can produce a separate reviewed receipt. No model or HTTP is run.
"""
import argparse
import fcntl
import hashlib
import json
from pathlib import Path
import socket
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
CORRECTED = ('tools/native_tts/managed_voice.py',
             'tools/native_tts/test_managed_voice.py')
FAILURE = 'RuntimeError: managed voice lost healthy isolated backend\n'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read(path):
    path = Path(path)
    require(path.is_file() and not path.is_symlink() and
            path.stat().st_size < 16*1024**2, 'review input is not bounded regular metadata')
    return json.loads(path.read_bytes())


def sha(path):
    with Path(path).open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def environment(plan, result, guard, queries, events):
    limits = plan['limits']
    require(plan['scenario'] == 'tool_version' and plan.get('native_voice') and
        limits == dict(starts=2, chats=24, probes=1, controls=2500, seconds=600),
        'review is limited to the complete registered voice recovery checklist')
    failure = result.get('failure')
    require(result.get('status') == 'FAILED' and result.get('cleanup_failure') is None and
        type(failure) is str and failure.endswith(FAILURE) and
        failure.count('Traceback (most recent call last):') == 1 and
        "save(self.output/'media-usage.json', check_business(self.output))" in failure and
        "managed_voice.check(out, plan," in failure and
        result['package'] == plan['package'] and result['scenario'] == plan['scenario'],
        'failure is not the exact final lazy-encoder validator assertion')
    require(type(result['seconds']) in (int,float) and 0 < result['seconds'] < 600,
            'original run exceeded its frozen duration')
    counts = result['budget']['counts']
    require(set(counts) == set(limits)-{'seconds'} and all(type(v) is int and
        0 <= v <= limits[k] for k,v in counts.items()) and
        all(counts[k] == limits[k] for k in ('starts','chats','probes')),
        'original work is incomplete or exceeds the bound')
    require(guard.get('status') == 'COMMAND_FAILED' and
        type(guard.get('child_returncode')) is int and guard['child_returncode'] == 1 and
        guard.get('compute_backend') == 'nvml' and
        guard.get('lease_inherited_by_child') is True and
        guard.get('external_lease') is False and guard.get('command') == plan['command'],
        'environment abort or different execution cannot be reviewed')
    require(queries and all(q.get('status') == 'OK' and
        type(q.get('returncode')) is int and q['returncode'] == 0 for q in queries),
        'original monitoring is incomplete')
    acquired = [e for e in events if e.get('event') == 'lease_acquired']
    released = [e for e in events if e.get('event') == 'lease_released']
    require(len(acquired) == len(released) == 1 and
        released[0].get('status') == 'COMMAND_FAILED' and
        acquired[0]['pid'] == released[0]['pid'], 'original continuous lease is absent')
    seconds = released[0]['monotonic']-acquired[0]['monotonic']
    require(0 < seconds < limits['seconds'], 'original lease exceeded frozen duration')
    return dict(strict_lease_seconds=seconds, nvml_queries=len(queries), env_abort=False,
        original_driver_status='FAILED', original_lease_status='COMMAND_FAILED',
        original_continuous_driver_passed=False, reviewed_complete_records=True)


def review(directory, output, snapshot):
    directory = Path(directory).resolve(strict=True)
    output = Path(output).absolute()
    snapshot = Path(snapshot).resolve(strict=True)
    require(directory.is_relative_to(ROOT/'.q4t-work') and
        output.is_relative_to(ROOT/'.q4t-work') and not output.exists(),
        'separate new private review directory required')
    plan = read(directory/'plan.json')
    result = read(directory/'execution/result.json')
    lease = directory/'lease';execution = directory/'execution'
    lines = lambda path: [json.loads(s) for s in path.read_text().splitlines()]
    observed = environment(plan,result,read(lease/'exit.json'),
                           lines(lease/'nvml-queries.jsonl'),lines(lease/'events.jsonl'))
    original = read(snapshot)
    require(original['status'] == 'ORIGINAL_FROZEN_SOURCES_UNCHANGED_AFTER_FAILED_RUN' and
        original['plan_sha256'] == sha(directory/'plan.json') and
        original['source_commit'] == plan['source_commit'] and
        original['files'] == plan['sources'], 'pre-correction source snapshot differs')
    changes = {}
    for name,digest in plan['sources'].items():
        current = sha(ROOT/name)
        if current != digest:
            require(name in CORRECTED and
                sha(snapshot.parent/'original-sources'/name) == digest,
                'unreviewed source drift: '+name)
            changes[name] = dict(original=digest,corrected=current)
    require(set(changes) == set(CORRECTED), 'exact two-file correction required')
    from tools.native_asr import service_acceptance as service
    service.configure_plan_limits(plan)
    bundle,_,_,journal,_,engine = service.LEGACY['load_modules'](plan)
    manifest = bundle.verify(plan['package']['package'],plan['package']['manifest_sha256'])
    for item in plan['protected']:
        require(sha(item['path']) == item['sha256'], 'protected default changed')
    package = Path(plan['package']['package'])
    metadata = read(package/'engine/q4t.release.json')
    runtime = read(metadata['tested_source_manifest'])
    require(all(sha(ROOT/n) == h for n,h in runtime.items()), 'qualified runtime changed')
    require(result['budget'] == read(execution/'counts.json'), 'counts changed')
    stopped = read(execution/'stop.json');state = read(execution/'state/ledger.json')['state']
    initialized = read(execution/'initialized.json')
    require(stopped['cleanup_confirmed'] and stopped['stop_durable'] and
        state['desired'] == 'STOPPED' and all(v is None for v in state['active'].values()) and
        state['service_id'] == initialized['service_id'], 'durable stop is incomplete')
    identities = {(v['pid'],v['start_ticks']) for v in result['models'].values()}
    require(len(identities) == 2, 'two actual models required')
    for row in lines(execution/'status.jsonl'):
        for role in ('guardian','controller','model','gateway'):
            if row.get(role+'_pid') is not None:
                identities.add((row[role+'_pid'],row[role+'_start_ticks']))
    for pid,ticks in identities:
        try:require(journal.process_start(pid) != ticks,'owned process survived')
        except (FileNotFoundError,ProcessLookupError):pass
    policy = read(package/'gateway-policy.json')
    for port in (policy['listen_port'],policy['upstream_port'],policy['upstream_operations_port']):
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            sock.bind(('127.0.0.1',port))
    with Path(manifest['lock_path']).open('r+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    from tools.serve_recovery import version_contract
    version_contract.verify(plan['registration'])
    frozen = {str(p.relative_to(directory)):sha(p) for p in directory.rglob('*')
              if p.is_file() and not p.is_symlink()}
    business = service.check_business(execution)
    require(business['version_recovery'] and business['actual_core_text'] == '42',
            'complete business check failed')
    require(all(sha(directory/n) == h for n,h in frozen.items()),'original record changed')
    output.mkdir(mode=0o700)
    engine.save(output/'reviewed-business.json',business)
    engine.save(output/'original-records.json',frozen)
    acceptance = 'ACCEPT_REVIEWED_NATIVE_VOICE_VERSION_RECORDS'
    decision = dict(status='PASS_REVIEWED_NATIVE_VOICE_VERSION_RECORDS',acceptance=acceptance,
        manifest_sha256=plan['package']['manifest_sha256'],engine_binary_sha256=sha(package/'engine/q4t'),
        actual_business=dict(status=acceptance,**observed,counts=result['budget']['counts'],
            source_commit=plan['source_commit'],plan_sha256=sha(directory/'plan.json'),
            result_sha256=sha(execution/'result.json'),lease_result_sha256=sha(lease/'exit.json'),
            postflight=dict(sources_unchanged=True,owned_processes_gone=True,
                ports_free=True,lock_released=True,default_unchanged=True),
            source_scope='All original frozen sources verified unchanged after run and before correction; only the two declared acceptance files changed afterward; runtime source and sealed payload unchanged',
            source_snapshot=dict(path=str(snapshot),sha256=sha(snapshot)),validator_changes=changes,
            reviewed_business_sha256=sha(output/'reviewed-business.json'),
            original_records_sha256=sha(output/'original-records.json')),
        original_directory=str(directory),original_records_unchanged=True,
        original_failure_relabelled=False,new_gpu_starts=0,new_http_requests=0,
        default_replaced=False,performance_acceptance=False,long_stability_accepted=False)
    engine.save(output/'decision.json',decision)
    engine.save(output/'service-release.json',dict(version=4,profile='q4t-service-recovery-v4',
        acceptance=acceptance,recovery_accepted=True,acceptance_record=str(output/'decision.json'),
        acceptance_sha256=sha(output/'decision.json'),**plan['package'],
        engine_binary_sha256=sha(package/'engine/q4t'),
        engine_release_sha256=sha(package/'engine/q4t.release.json'),ingress_mode='native',
        state=str(execution/'state'),service_id=initialized['service_id'],
        initial_desired='STOPPED',automatically_started=False,
        scope='Actual complete registered voice version recovery; separate offline lazy-encoder validator correction; original FAILED and COMMAND_FAILED retained',
        default_replaced=False))
    print(json.dumps(dict(acceptance=acceptance,**observed,counts=result['budget']['counts'])))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--source-snapshot',required=True,type=Path)
    args=parser.parse_args()
    review(args.directory,args.output,args.source_snapshot)


if __name__ == '__main__':main()
