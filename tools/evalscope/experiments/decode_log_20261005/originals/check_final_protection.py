"""One-shot, read-only final protection check; never launches or stops workloads.

Run after all started controllers finish and before editing the source worktree.
Only final-protection.json is created. Existing output is never overwritten.
No project modules are imported, no model weight payload is read, no process
is killed. Only two explicitly frozen JSON config/index files may be hashed.
Current entry.json owns the MAIN baseline; no preceding-phase binary fallback.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import time

R = Path('/home/rm01/models/dev/qwen4-thor/.q4t-work/'
         'offload-decode-log-20261005')
W = R / 'source'
MAIN = R.parent.parent
MODEL = Path('/home/rm01/models/dev/llm/garnermccloud/'
             'Qwen3.8-Flash-Next-NVFP4-SSD-Stream')
OUT = R / 'final-protection.json'
COMMIT = '6b5169355d7aa666653c3b7076676e64f697d313'
RUNTIME = '8447d8982ba705518be23f255ddf8dc402e4feb1dbe9f42536acd9634cbb3aa6'
TEST = '64ec1cba2458dea35782eacc9e4710b92a97c2298aec6461003d580e653d4541'
MAIN_BINARY = '46b3f977b0653c2f317c63df5b9f6669d1f3bb1de17892970b605676dd5434bc'
DIFF = '33375caff6dcb3e638e2d2f001428883cbc0e400c5f1b7403a081826e9cd6947'
PINS = {
    'execution-plan.json': '4814e3f1e0581fe18335f950a814b315e08711e277e764807f28d5afb1349fb2',
    'plan.json': 'ecfcd41ef3e6dd5ff9387f34a06c43e1c1f524cf01e3cc2f120ad8a519f0fd01',
    'entry.json': '92b96442efea8397008b330ebff6f1c94b94f3c2198b7edb30126255cab40c5f',
    'model-entry.json': '12619754ecba661f975921614d54b04df12a5b1bcb4bb2191e832b524cebd54b',
    'numerical-decision.json': 'aeabac23ff7c13215c4615fc9a0789c6ee66110846406ed5116160e5e46ecd71',
    'entry-main.diff': '33375caff6dcb3e638e2d2f001428883cbc0e400c5f1b7403a081826e9cd6947',
}
GROUPS = ('quality-c', 'history-a', 'history-b', 'history-c',
          'matrix-a', 'matrix-c')
MODEL_HASH_ALLOWLIST = {MODEL / 'config.json',
                        MODEL / 'model.safetensors.index.json'}
PORTS = set()
READ_SHA = {}
OWNED_PIDS = set()
OWNED_PGIDS = set()
UNITS = []


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def sha(path):
    path = Path(path)
    if path.resolve().is_relative_to(MODEL):
        require(path in MODEL_HASH_ALLOWLIST and path.resolve() == path
                and path.stat().st_size <= 64 * 1024 * 1024,
                'model weight/non-allowlisted content hashing prohibited')
    require(stat.S_ISREG(path.lstat().st_mode), 'not regular: ' + str(path))
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read(path):
    path = Path(path)
    require(path.stat().st_size < 2 * 1024 * 1024, 'metadata too large')
    digest = sha(path)
    value = json.loads(path.read_text())
    require(sha(path) == digest, 'metadata changed while reading')
    require(str(path) not in READ_SHA or READ_SHA[str(path)] == digest,
            'previously read metadata changed')
    READ_SHA[str(path)] = digest
    return value


def command(argv, cwd=None):
    return subprocess.run(argv, cwd=cwd, capture_output=True, timeout=30,
                          env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'})


def git(path, *args):
    result = command(['git', '--no-pager', *args], path)
    require(result.returncode == 0, 'git read failed: ' + str(args))
    return result.stdout


def ended(row):
    value = row.get('ended_t')
    require(isinstance(value, (int, float)) and math.isfinite(value)
            and 0 < value <= time.time(), 'missing or future terminal time')


def absent_group(row):
    require(row.get('absent') is True and row.get('live_pids') == []
            and row.get('zombie_pids') == [] and row.get('errors') == [],
            'recorded process group cleanup not proven')


def process_record(row):
    ended(row)
    require(row.get('failure') is None and row.get('cleanup_complete') is True,
            'process terminal cleanup failed or unknown')
    absent_group(row['group_after_cleanup'])
    require(type(row.get('pid')) is int and row['pid'] > 1
            and row.get('pgid') == row['pid'], 'invalid owned PID/PGID')
    OWNED_PIDS.add(row['pid'])
    OWNED_PGIDS.add(row['pgid'])


def terminal_gate():
    # Refuse before process/model/source checks unless all frozen work is closed.
    required = [R / 'pipeline-execution/exit.json']
    required += [R / (g + suffix) for g in GROUPS for suffix in
                 ('-controller-start.json', '-controller-exit.json', '-stage.json')]
    required += [R / (n + '-decision.json') for n in
                 ('quality', 'host', 'numerical', 'history', 'matrix', 'performance')]
    for path in required:
        require(path.is_file() and not path.is_symlink(),
                'not released; terminal missing: ' + str(path))
    plan = read(R / 'execution-plan.json')
    plan_sha = PINS['execution-plan.json']
    start = read(R / 'pipeline-execution/start.json')
    final = read(R / 'pipeline-execution/exit.json')
    ended(final)
    require(start['plan_sha256'] == final['plan_sha256'] == plan_sha
            and final['completed'] is True and final['failure'] is None
            and final['runtime_source_commit'] == COMMIT
            and final['runtime_binary_sha256'] == RUNTIME
            and final['next_model_stage_automatically_allowed'] is False,
            'frozen pipeline not cleanly terminal')
    labels = ['quality-c-run', 'quality-c-audit', 'host', 'numerical']
    for group in ('history-a', 'history-b', 'history-c'):
        labels += [group + '-run', group + '-audit']
    labels += ['history-compare', 'matrix-a-run', 'matrix-a-audit',
               'matrix-c-run', 'matrix-c-audit', 'matrix-compare', 'aggregate']
    require(len(final['completed_steps']) == len(labels) == 17,
            'pipeline step count changed')
    expected_starts = {f'step-{i:02d}-{label}-start.json'
                       for i, label in enumerate(labels, 1)}
    actual_starts = {p.name for p in
                     (R / 'pipeline-execution').glob('step-*-start.json')}
    require(actual_starts == expected_starts, 'pipeline start set changed')
    for index, label in enumerate(labels, 1):
        stem = f'step-{index:02d}-{label}'
        first = read(R / 'pipeline-execution' / (stem + '-start.json'))
        last = read(R / 'pipeline-execution' / (stem + '-exit.json'))
        require(first['started_t'] == last['started_t']
                and first['command'] == last['command'], 'step identity mismatch')
        allowed = (0, 3) if label in ('history-compare', 'matrix-compare',
                                      'aggregate') else (0,)
        require(last['returncode'] in allowed, 'step execution failed')
        process_record(last)
        require(final['completed_steps'][index - 1] == {
            'step': index, 'label': label, 'returncode': last['returncode'],
            'ended_t': last['ended_t']}, 'pipeline summary mismatch')
    for name, count in [('host', len(plan['host_python_groups']) + 3),
                        ('numerical', 2)]:
        decision = read(R / (name + '-decision.json'))
        require(decision['passed'] is True
                and decision['runtime_source_commit'] == COMMIT
                and decision['runtime_binary_sha256'] == RUNTIME
                and decision['plan_sha256'] == plan_sha, 'direct identity mismatch')
        require(len(decision['records']) == count,
                'direct-contract process record count changed')
        starts = sorted((R / (name + '-01')).glob('*-start.json'))
        require(len(starts) == count, 'direct start inventory incomplete')
        for first_path in starts:
            last_path = first_path.with_name(
                first_path.name.replace('-start.json', '-exit.json'))
            first, last = read(first_path), read(last_path)
            require(last in decision['records']
                    and first['started_t'] == last['started_t']
                    and first['command'] == last['command']
                    and last['returncode'] == 0, 'direct receipt mismatch')
            process_record(last)
    actual = {p.name for p in (W / '.q4t-work/evidence').iterdir() if p.is_dir()}
    require(actual == set(GROUPS), 'unexpected/missing evidence group')
    require({p.name for p in R.glob('*-controller-start.json')} ==
            {g + '-controller-start.json' for g in GROUPS},
            'unexpected/missing HTTP controller start')
    # Clear before repeated end-of-audit snapshot; PID sets are deduplicated.
    UNITS.clear()
    for group in GROUPS:
        base = W / '.q4t-work/evidence' / group
        controller = read(R / (group + '-controller-exit.json'))
        first = read(R / (group + '-controller-start.json'))
        stage = read(R / (group + '-stage.json'))
        require(first['started_t'] == controller['started_t']
                and first['command'] == controller['command']
                and controller['returncode'] == 0, 'HTTP controller mismatch')
        process_record(controller)
        require(stage.get('cleanup_complete') is True
                and stage.get('failure') is None
                and stage['runtime_source_commit'] == COMMIT
                and stage['runtime_binary_sha256'] == RUNTIME
                and stage['plan_sha256'] == plan_sha, 'stage not closed/identity')
        ended(stage)
        frozen_command = read(R / (group + '-command.json'))
        require(frozen_command == controller['command'], 'HTTP command changed')
        port = int(frozen_command[frozen_command.index('--port') + 1])
        require(0 < port < 65536, 'invalid owned port')
        PORTS.add(port)
        wrapper = read(base / 'wrapper-exit.json')
        ended(wrapper)
        require(wrapper.get('cleanup_failed') is False
                and wrapper.get('failure') is None
                and wrapper.get('runner_rc') == wrapper.get('monitor_rc') == 0,
                'wrapper/monitor did not finish cleanly')
        runner = read(base / 'runner-process-group.json')
        ended(runner)
        require(runner.get('runner_reaped') is True
                and runner.get('cleanup_complete') is True
                and runner.get('failure') is None, 'runner cleanup unknown')
        absent_group(runner['after_cleanup'])
        pid = runner['runner_pid']
        require(type(pid) is int and pid == runner['pgid'] and pid > 1,
                'runner identity invalid')
        OWNED_PIDS.add(pid)
        OWNED_PGIDS.add(runner['pgid'])
        http = read(base / 'http/exit.json')
        require(http.get('cleanup_failure') is None
                and http.get('failure') is None and http.get('server') == 0,
                'HTTP did not close cleanly')
        protocol = read(base / 'protocol.json')
        launch = read(base / 'http/isolation/launch.json')
        identity = read(base / 'http/isolation/identity.json')
        cleanup = read(base / 'http/isolation/cleanup.json')
        unit = protocol['unit']
        require(protocol['binary_sha256'] == RUNTIME
                and protocol['binary'] == plan['runtime_binary_path'],
                'HTTP binary identity changed')
        require(re.fullmatch(r'q4t-ram-\d+-' + str(controller['pid'])
                             + r'\.service', unit), 'unit/wrapper mismatch')
        require(launch['unit'] == unit and '--unit=' + unit in launch['argv'],
                'unit launch ownership mismatch')
        cgroup = '/system.slice/' + unit
        service_pid = identity['pid']
        require(type(service_pid) is int and service_pid > 1
                and identity['properties']['MainPID'] == str(service_pid)
                and identity['properties']['ControlGroup'] == cgroup
                and '0::' + cgroup in identity['membership'], 'service identity mismatch')
        require(cleanup.get('unit_removed') is True and cleanup['stop_rc'] == 0
                and cleanup['properties_after'].get('LoadState') == 'not-found'
                and wrapper['unit_after_cleanup'].get('LoadState') == 'not-found',
                'unit cleanup receipt incomplete')
        OWNED_PIDS.add(service_pid)
        UNITS.append({'group': group, 'unit': unit, 'cgroup': cgroup,
                      'pid': service_pid})
    return {'pipeline_steps': len(labels), 'groups': list(GROUPS),
            'pipeline_terminal': True, 'ports': sorted(PORTS),
            'scope': 'Frozen HTTP/direct pipeline only; other offline agents must '
                     'also be idle before current process snapshot.'}


def current_cleanup():
    # Never signal a PID. Existing numeric IDs may have been reused; fail closed.
    present = [pid for pid in sorted(OWNED_PIDS) if Path('/proc', str(pid)).exists()]
    matches, errors, foreign_exe_not_inspected = [], [], 0
    exe_fallbacks = []
    for path in Path('/proc').iterdir():
        if not path.name.isdigit() or int(path.name) == os.getpid():
            continue
        try:
            fields = (path / 'stat').read_text().rsplit(')', 1)[1].split()
            argv = (path / 'cmdline').read_bytes().split(b'\0')
            argv = [arg.decode(errors='replace') for arg in argv if arg]
            same_uid = path.stat().st_uid == os.getuid()
            # q4t services explicitly run as this user. Foreign root processes
            # are checked by visible argv/PGID; their exe need not be readable.
            exe = ''
            if argv and same_uid:
                try:
                    exe = os.readlink(path / 'exe')
                except PermissionError:
                    # Same-UID non-dumpable services can deny unprivileged
                    # readlink. Observe the same proc link without signaling.
                    fallback = ['sudo', '-n', '/usr/bin/readlink', '--',
                                str(path / 'exe')]
                    observation = {
                        'pid': int(path.name),
                        'source': 'sudo_readlink_after_direct_PermissionError',
                        'command': fallback, 'returncode': None,
                        'starttime_ticks_before': fields[19],
                        'outcome': 'UNRESOLVED'}
                    exe_fallbacks.append(observation)
                    try:
                        result = command(fallback)
                    except (OSError, subprocess.TimeoutExpired) as error:
                        observation['failure'] = type(error).__name__ + ': ' + str(error)
                        require(False, 'exe visibility fallback failed: '
                                + json.dumps(observation))
                    observation.update(returncode=result.returncode,
                                       stdout=os.fsdecode(result.stdout),
                                       stderr=os.fsdecode(result.stderr))
                    # A vanished process retains the existing race behavior.
                    # A reused PID or unresolved link remains a refusal.
                    try:
                        after = (path / 'stat').read_text().rsplit(')', 1)[1].split()
                    except (FileNotFoundError, ProcessLookupError):
                        observation['outcome'] = 'PROCESS_VANISHED'
                        raise
                    observation['starttime_ticks_after'] = after[19]
                    require(after[19] == fields[19],
                            'PID identity changed during exe fallback: '
                            + json.dumps(observation))
                    require(result.returncode == 0
                            and observation['stdout'].startswith('/')
                            and observation['stdout'].endswith('\n')
                            and '\n' not in observation['stdout'][:-1],
                            'exe visibility fallback unresolved: '
                            + json.dumps(observation))
                    exe = observation['stdout'][:-1]
                    observation['outcome'] = 'RESOLVED_SAME_PID_STARTTIME'
                    observation['resolved_exe'] = exe
            foreign_exe_not_inspected += int(bool(argv) and not same_uid)
            task_token = any(arg.startswith(str(R) + '/') and
                             arg != str(Path(__file__).resolve())
                             for arg in argv)
            if int(fields[2]) in OWNED_PGIDS or task_token or exe.startswith(str(R) + '/'):
                matches.append({'pid': int(path.name), 'pgid': int(fields[2]),
                                'state': fields[0], 'argv': argv, 'exe': exe})
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            # A full /proc scan is not silently presented as exhaustive.
            errors.append({'pid': int(path.name), 'error': 'PermissionError'})
    require(not present and not matches and not errors,
            'owned/reused process identity present or process visibility unknown: '
            + json.dumps({'recorded_pids_present': present, 'matches': matches,
                          'visibility_errors': errors,
                          'exe_permission_fallbacks': exe_fallbacks}))
    current = []
    for owned in UNITS:
        result = command(['sudo', '-n', 'systemctl', 'show', owned['unit'],
                          '--property=LoadState,ActiveState,SubState,MainPID,ControlPID,ControlGroup'])
        props = dict(line.split('=', 1) for line in result.stdout.decode().splitlines()
                     if '=' in line)
        require(props.get('LoadState') == 'not-found'
                and props.get('ActiveState') == 'inactive'
                and props.get('MainPID') == props.get('ControlPID') == '0'
                and not props.get('ControlGroup'), 'owned unit still exists/unknown')
        require(not Path('/sys/fs/cgroup' + owned['cgroup']).exists(),
                'owned cgroup still exists')
        current.append({**owned, 'properties_now': props,
                        'query_returncode': result.returncode, 'cgroup_absent': True})
    sockets = []
    for filename in ('tcp', 'tcp6'):
        network = Path('/proc/net') / filename
        for line in network.read_text().splitlines()[1:]:
            fields = line.split()
            if fields[3] == '0A' and int(fields[1].rsplit(':', 1)[1], 16) in PORTS:
                sockets.append({'table': str(network), 'local': fields[1],
                                'state': fields[3], 'inode': fields[9]})
    require(not sockets, 'owned TCP port still has a listener: ' + json.dumps(sockets))
    return {'ports_checked': sorted(PORTS), 'listening_sockets': sockets,
            'port_scope': 'Current network namespace, TCP IPv4/IPv6 listeners only.',
            'recorded_pids': sorted(OWNED_PIDS), 'recorded_pgids': sorted(OWNED_PGIDS),
            'recorded_ids_present': [], 'task_process_matches': [],
            'visibility_errors': [], 'units': current,
            'foreign_executables_not_inspected': foreign_exe_not_inspected,
            'exe_permission_fallbacks': exe_fallbacks,
            'process_scan_scope': 'All readable argv/PGIDs and same-user executables; this is not a machine-wide absence proof for hidden foreign-user processes.',
            'PID_reuse_policy': 'Any currently present recorded numeric ID fails; no signal sent.',
            'monitor_scope': 'No monitor PID snapshot was recorded. Wrapper monitor_rc=0 proves wait completed; wrapper PGID and current exact task-path scan are also checked.',
            'outer_scope': 'The pipeline outer start/exit does not record PID/start ticks. Terminal receipt plus exact task-path scan is used; no invented PID identity.',
            'snapshot_only': True}

def protection():
    for name, digest in PINS.items():
        require(sha(R / name) == digest, 'pinned artifact changed: ' + name)
    plan = read(R / 'execution-plan.json')
    frozen = plan['frozen_files']
    checked, model_configs = {}, {}
    for name, digest in frozen.items():
        require(sha(name) == digest, 'frozen input changed: ' + name)
        checked[name] = digest
        if Path(name).resolve().is_relative_to(MODEL):
            model_configs[name] = digest
    require(set(map(Path, model_configs)) == MODEL_HASH_ALLOWLIST,
            'frozen model JSON allowlist changed')
    require(git(W, 'rev-parse', 'HEAD').decode().strip() == COMMIT
            and not git(W, 'status', '--porcelain').strip(),
            'runtime source worktree must remain clean at tested commit')
    binaries = {}
    for name, path, digest in (
            ('runtime', R / 'candidate-build/q4t', RUNTIME),
            ('numerical_test', R / 'candidate-build/q4t_tests', TEST),
            ('main_current_entry_comparison', MAIN / 'build/q4t', MAIN_BINARY)):
        binaries[name] = {'path': str(path), 'sha256': sha(path)}
        require(binaries[name]['sha256'] == digest, name + ' binary changed')
    numeric = read(R / 'numerical-decision.json')
    require(numeric['test_binary_sha256_before'] ==
            numeric['test_binary_sha256_after'] == TEST, 'numeric identity mismatch')
    entry = read(R / 'entry.json')
    require(entry['main_binary'] == {'path': str(MAIN / 'build/q4t'),
                                    'sha256': MAIN_BINARY},
            'current phase main binary entry changed')
    status_lines = git(MAIN, 'status', '--porcelain').decode().splitlines()
    require(status_lines == entry['main_status'] and len(status_lines) == 7,
            'original main seven-path status changed')
    paths = [line[3:] for line in entry['main_status']]
    current_diff = git(MAIN, 'diff', '--binary', '--no-ext-diff', 'HEAD', '--', *paths)
    digest = hashlib.sha256(current_diff).hexdigest()
    require(digest == entry['main_diff_sha256'] == DIFF
            and sha(R / 'entry-main.diff') == DIFF, 'original seven-path diff changed')
    require(git(MAIN, 'rev-parse', 'HEAD').decode().strip() == entry['main_head'],
            'main HEAD changed')
    model_entry = read(R / 'model-entry.json')['files']
    require(len(model_entry) == 228, 'model entry row count changed')
    actual, mismatches = set(), []
    for folder, dirs, files in os.walk(MODEL, followlinks=False):
        require(all(not (Path(folder) / d).is_symlink() for d in dirs),
                'model symlink directory unsupported')
        for name in files:
            path = Path(folder) / name
            require(stat.S_ISREG(path.lstat().st_mode), 'model nonregular file')
            actual.add(str(path))
    require(actual == {row['path'] for row in model_entry}, 'model path set changed')
    for row in model_entry:
        value = Path(row['path']).lstat()
        now = {'size': value.st_size, 'inode': value.st_ino,
               'device': value.st_dev, 'mtime_ns': value.st_mtime_ns}
        if any(now[key] != row[key] for key in now):
            mismatches.append({'path': row['path'], 'current': now})
    require(not mismatches, 'model metadata changed: ' + json.dumps(mismatches))
    reference = {}
    for label, path in [('main', MAIN), ('runtime_worktree', W)]:
        reference[label] = {
            'tracked_paths': git(path, 'ls-files', '--', 'reference').decode().splitlines(),
            'tracked_status': git(path, 'status', '--porcelain', '--untracked-files=no',
                                  '--', 'reference').decode().splitlines()}
        require(not reference[label]['tracked_status'], 'tracked reference changed')
    return {'main': {'head': entry['main_head'], 'status': status_lines,
                     'seven_path_diff_sha256': digest},
            'binaries': binaries,
            'build_export_scope': 'Frozen archive/identity and build inputs are hashed; '
                                  'the exported source tree is not rescanned here.',
            'main_binary_evidence_scope': 'Matches this phase entry.json directly. '
                'Previous-phase runtime identity is not substituted; snapshot '
                'equality does not prove continuous absence of transient writes.',
            'model': {'rows': 228, 'file_set_and_metadata_match': True,
                      'fields': ['size', 'inode', 'device', 'mtime_ns'],
                      'weight_payload_hashed': False,
                      'explicit_config_index_sha256': model_configs,
                      'limit': 'Metadata equality is not byte-content proof or a continuous write audit.'},
            'reference': {'status': 'UNKNOWN_WHOLE_TREE_UNCHANGED',
                          'observations': reference,
                          'limit': 'No entry whole-tree manifest. Tracked-only Git observations do not cover ignored/untracked reference trees or prove no writes. This checker performs no reference writes.'},
            'frozen_input_sha256': checked}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--self-sha256', required=True)
    args = parser.parse_args()
    require(not OUT.exists() and not OUT.is_symlink(), 'output exists; refuse overwrite')
    require(sha(__file__) == args.self_sha256, 'reviewed self SHA mismatch')
    # Refusal here creates no output and performs no binary/model/source audit.
    for name, digest in PINS.items():
        require(sha(R / name) == digest, 'pinned entry/plan changed: ' + name)
    gate = terminal_gate()
    report = {'schema': 1, 'started_t': time.time(), 'checker_sha256': args.self_sha256,
              'terminal_gate': gate, 'failure': None,
              'scoped_checks_passed': False, 'whole_tree_readonly_proven': False,
              'performance_or_resource_acceptance': False, 'goal_complete': False}
    try:
        report['cleanup'] = current_cleanup()
        report['protection'] = protection()
        for name, digest in READ_SHA.items():
            require(sha(name) == digest, 'receipt changed during audit: ' + name)
        require(terminal_gate() == gate, 'started-controller inventory changed')
        report['cleanup_at_end'] = current_cleanup()
        report['scoped_checks_passed'] = True
        report['status'] = 'SCOPED_CHECKS_PASS_WITH_EXPLICIT_LIMITATIONS'
    except Exception as error:
        report['failure'] = type(error).__name__ + ': ' + str(error)
        report['status'] = 'FAIL_OR_UNKNOWN'
    report.update(ended_t=time.time(), metadata_source_sha256=READ_SHA,
                  pinned_source_sha256=PINS,
                  gpu_limit='No separate GPU allocator inventory is taken; absence of owned service/process identities is checked. This is not a whole-device idle or memory-release proof.',
                  self_hash_rule='Output does not hash itself; bind its final SHA externally.')
    with OUT.open('x') as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps({'status': report['status'], 'output': str(OUT)}))
    return 0 if report['scoped_checks_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
