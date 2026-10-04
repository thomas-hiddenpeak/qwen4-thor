"""One-shot, read-only final protection check; never launches or stops workloads.

Run after all started controllers finish and before editing the source worktree.
Only final-protection.json is created. Existing output is never overwritten.
No project modules are imported, no model payload is read, no process is killed.
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
import tarfile
import time

R = Path('/home/rm01/models/dev/qwen4-thor/.q4t-work/'
         'offload-request-policy-20261004')
W = R / 'source'
MAIN = R.parent.parent
MODEL = Path('/home/rm01/models/dev/llm/garnermccloud/'
             'Qwen3.8-Flash-Next-NVFP4-SSD-Stream')
OUT = R / 'final-protection.json'
COMMIT = '52b42b2e7775929ed9cd6a237c822bc6dfa87a06'
RUNTIME = '34eb4cf5b53dfcc5b0783120fcdec438fd321dbd4c981f52eee581c96f7fb93a'
TEST = '34f105309b126dfe8b639317aa7953586280602d0721df6f2f6631af28f50556'
MAIN_BINARY = '46b3f977b0653c2f317c63df5b9f6669d1f3bb1de17892970b605676dd5434bc'
DIFF = '33375caff6dcb3e638e2d2f001428883cbc0e400c5f1b7403a081826e9cd6947'
PINS = {
    'execution-plan.json': 'dd12d7cf8beaad1d5bf2db1d581ef95282b8da53b5c45fdd62bcc7580b0f3d9d',
    'plan.json': '7ca311700e9ede574ffec89deaa746d8dc0fcd3defdce66496aff09b6e266479',
    'coverage-amendment.json': '12c1579eba32cc91219bd2d3453cf65fca40bf7fc88022e12d1acab705e6a23e',
    'continue_coverage.py': 'fbb96f5dbf506e438354df6258c7c607ea78d63001bb39b095fbff13f2b1ff8f',
    'entry.json': '34b1d7000340d42d7c421038fd5cfa6bca3e7274327f3cde1b63493ad7ee155b',
    'model-entry.json': 'ab1aeb1a567a595579dbd36dd0d50a2713127b09ef2d427c20aa561cc2cc6b58',
    'completion-requirements-v2.json': 'dbd4064003bbdc2cbff9efb2900c2cbd6d1697553ed1c80ca03a7ef121a0c6cf',
    'final-packaging-checklist.json': '69d1e56016ab0b3a1344e923070f67f02b3f5baaf1a90e9ec69e79b8c356564c',
    'audit_resources.py': 'b7b94dcd950389b9e2c0c985630ad702c8040acb05032a23eaf6c17e2ad42399',
    'resource-audit-preparation.json': '0affff35e2ebd213bbf95cb0afa9aaeee36af8f2a3f7ab5be66eb1744fc7e7ee',
    '../offload-partition-runtime-20261003/audit_raw_resources_frozen.py':
        '035f1f840d00350a7b6db62e1b8ef7f62a5fda02406dae91395d291a67652da4',
    '../offload-diagnostics-20261004/protection-audit.json':
        '2e85b63b01df58fdd0c544d69f5bce77129151a970990e493ece7d375b4a12f4',
}
GROUPS = ('quality-on', 'inheritance-off', 'inheritance-on',
          'matrix-off', 'matrix-on')
READ_SHA = {}
OWNED_PIDS = set()
OWNED_PGIDS = set()
COMMANDS = []
UNITS = []


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def sha(path):
    path = Path(path)
    require(not path.resolve().is_relative_to(MODEL),
            'model content hashing prohibited')
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
    COMMANDS.append(row['command'])


def terminal_gate():
    # Missing terminal files refuse the invocation before reading group details.
    required = [R / (g + suffix) for g in GROUPS for suffix in
                ('-controller-start.json', '-controller-exit.json', '-stage.json')]
    required += [R / (name + '-continuation-exit.json')
                 for name in ('inheritance', 'coverage')]
    for path in required:
        require(path.is_file() and not path.is_symlink(),
                'not released; terminal missing: ' + str(path))
    starts = sorted(R.glob('*-start.json'))
    starts += sorted((R / 'host-01').glob('*-start.json'))
    starts += sorted((R / 'numerical-01').glob('*-start.json'))
    require(starts, 'missing start inventory')
    for start in starts:
        finish = start.with_name(start.name.replace('-start.json', '-exit.json'))
        require(finish.is_file(), 'started controller is not terminal: ' + str(start))
        first, last = read(start), read(finish)
        ended(last)
        require(first['started_t'] == last['started_t'], 'start/exit mismatch')
        require(last.get('failure') is None and not last.get('cleanup_unknown'),
                'failed or unknown controller cleanup')
        if 'cleanup_complete' in last:
            process_record(last)
        elif start.name.startswith('coverage-step-'):
            require(last.get('child_controller_reaped') is True
                    and last.get('cleanup_unknown') is False, 'unreaped step')
            require(type(last.get('pid')) is int and last['pid'] > 1,
                    'missing step PID')
            OWNED_PIDS.add(last['pid'])
            # continue_coverage launches this direct child in a new session.
            OWNED_PGIDS.add(last['pid'])
            COMMANDS.append(last['command'])
        elif start.name not in ('inheritance-continuation-start.json',
                                'coverage-continuation-start.json'):
            raise RuntimeError('unsupported started-controller schema: ' + str(start))
    history = read(R / 'inheritance-continuation-exit.json')
    coverage = read(R / 'coverage-continuation-exit.json')
    require(coverage.get('completed') is True
            and coverage.get('cleanup_unknown') is False, 'coverage not closed')
    for row in history['commands'] + coverage['commands']:
        ended(row)
        require(row.get('failure') is None, 'continuation child failed')
        COMMANDS.append(row['command'])
    for name, count in [('host', 8), ('numerical', 2)]:
        decision = read(R / (name + '-decision.json'))
        require(len(decision['records']) == count,
                'direct-contract process record count changed')
        require(len(list((R / (name + '-01')).glob('*-start.json'))) == count,
                'direct-contract start inventory incomplete')
        for row in decision['records']:
            process_record(row)
    # This checker supports the actual five-group scope, not unrun D adapters.
    actual = {p.name for p in (W / '.q4t-work/evidence').iterdir() if p.is_dir()}
    require(actual == set(GROUPS), 'unexpected/missing evidence group')
    for group in GROUPS:
        base = W / '.q4t-work/evidence' / group
        controller = read(R / (group + '-controller-exit.json'))
        stage = read(R / (group + '-stage.json'))
        require(stage.get('cleanup_complete') is True
                and stage.get('failure') is None, 'stage cleanup not closed')
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
        require(type(runner.get('runner_pid')) is int
                and runner['runner_pid'] == runner.get('pgid')
                and runner['runner_pid'] > 1, 'runner identity invalid')
        OWNED_PIDS.add(runner['runner_pid'])
        OWNED_PGIDS.add(runner['pgid'])
        http = read(base / 'http/exit.json')
        require(http.get('cleanup_failure') is None, 'HTTP cleanup failed')
        protocol = read(base / 'protocol.json')
        launch = read(base / 'http/isolation/launch.json')
        identity = read(base / 'http/isolation/identity.json')
        cleanup = read(base / 'http/isolation/cleanup.json')
        unit = protocol['unit']
        require(re.fullmatch(r'q4t-ram-\d+-' + str(controller['pid'])
                             + r'\.service', unit), 'unit/wrapper mismatch')
        require(launch['unit'] == unit and '--unit=' + unit in launch['argv'],
                'unit launch ownership mismatch')
        cgroup = '/system.slice/' + unit
        pid = identity['pid']
        require(type(pid) is int and pid > 1
                and identity['properties']['MainPID'] == str(pid)
                and identity['properties']['ControlGroup'] == cgroup
                and '0::' + cgroup in identity['membership'],
                'service identity mismatch')
        require(cleanup.get('unit_removed') is True
                and cleanup['properties_after'].get('LoadState') == 'not-found'
                and wrapper['unit_after_cleanup'].get('LoadState') == 'not-found',
                'unit cleanup receipt incomplete')
        OWNED_PIDS.add(pid)
        UNITS.append({'group': group, 'unit': unit, 'cgroup': cgroup, 'pid': pid})
        COMMANDS.extend([read(base / 'monitor-command.json'),
                         read(base / 'runner-command.json'), launch['argv']])
    return {'start_records': len(starts), 'groups': list(GROUPS),
            'continuations_terminal': True}


def current_cleanup():
    # Never signal a PID. Existing numeric IDs may have been reused; fail closed.
    present = [pid for pid in sorted(OWNED_PIDS) if Path('/proc', str(pid)).exists()]
    matches, errors, foreign_exe_not_inspected = [], [], 0
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
            exe = os.readlink(path / 'exe') if argv and same_uid else ''
            foreign_exe_not_inspected += int(bool(argv) and not same_uid)
            task_token = any(arg.startswith(str(R) + '/') and
                             not arg.endswith('check_final_protection.py')
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
                          'visibility_errors': errors}))
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
    return {'recorded_pids': sorted(OWNED_PIDS), 'recorded_pgids': sorted(OWNED_PGIDS),
            'recorded_ids_present': [], 'task_process_matches': [],
            'visibility_errors': [], 'units': current,
            'foreign_executables_not_inspected': foreign_exe_not_inspected,
            'process_scan_scope': 'All readable argv/PGIDs and same-user executables; this is not a machine-wide absence proof for hidden foreign-user processes.',
            'PID_reuse_policy': 'Any currently present recorded numeric ID fails; no signal sent.',
            'monitor_scope': 'No monitor PID snapshot was recorded. Wrapper monitor_rc=0 proves wait completed; wrapper PGID and current exact task-path scan are also checked.',
            'outer_scope': 'Some build/history outer records lack PID/start ticks. Terminal receipts and current task-path scan are used; no invented PID identity.',
            'snapshot_only': True}


def export_check():
    source = R / 'candidate-source'
    expected = set()
    with tarfile.open(R / 'candidate-source.tar') as archive:
        for member in archive:
            require(not member.name.startswith('/') and
                    '..' not in Path(member.name).parts, 'unsafe archive name')
            target = source / member.name
            if member.isdir():
                require(target.is_dir() and not target.is_symlink(), 'export directory changed')
            elif member.issym():
                expected.add(member.name)
                require(target.is_symlink() and os.readlink(target) == member.linkname,
                        'export symlink changed')
            else:
                require(member.isfile(), 'unsupported archive member')
                expected.add(member.name)
                with archive.extractfile(member) as content:
                    require(sha(target) == hashlib.file_digest(content, 'sha256').hexdigest(),
                            'export bytes changed: ' + member.name)
    actual = {str(p.relative_to(source)) for p in source.rglob('*')
              if p.is_file() or p.is_symlink()}
    require(actual == expected, 'export file set changed')
    return {'source_commit': COMMIT, 'files': len(expected), 'matches_archive': True}


def protection():
    for name, digest in PINS.items():
        require(sha(R / name) == digest, 'pinned artifact changed: ' + name)
    plan, amendment = read(R / 'execution-plan.json'), read(R / 'coverage-amendment.json')
    frozen = {**plan['frozen_files'], **amendment['original_files'],
              **amendment['source_sha256']}
    checked, model_skips = {}, []
    for name, digest in frozen.items():
        if Path(name).resolve().is_relative_to(MODEL):
            model_skips.append(name)
            continue
        require(sha(name) == digest, 'frozen input changed: ' + name)
        checked[name] = digest
    require(git(W, 'rev-parse', 'HEAD').decode().strip() == COMMIT
            and not git(W, 'status', '--porcelain').strip(),
            'runtime source worktree must remain clean at tested commit')
    binaries = {}
    for name, path, digest in (
            ('runtime', R / 'candidate-build/q4t', RUNTIME),
            ('numerical_test', R / 'candidate-build/q4t_tests', TEST),
            ('main_historical_comparison', MAIN / 'build/q4t', MAIN_BINARY)):
        binaries[name] = {'path': str(path), 'sha256': sha(path)}
        require(binaries[name]['sha256'] == digest, name + ' binary changed')
    numeric = read(R / 'numerical-decision.json')
    require(numeric['test_binary_sha256_before'] ==
            numeric['test_binary_sha256_after'] == TEST, 'numeric identity mismatch')
    prior = read(R.parent / 'offload-diagnostics-20261004/protection-audit.json')
    require(prior['main_binary_sha256'] == MAIN_BINARY, 'historical binary evidence changed')
    entry = read(R / 'entry.json')
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
            'binaries': binaries, 'export': export_check(),
            'main_binary_evidence_scope': 'Matches preceding diagnostic protection-audit, SHA bound in pins. This phase entry.json has no binary digest; no claim of continuous absence of transient writes.',
            'model': {'rows': 228, 'file_set_and_metadata_match': True,
                      'fields': ['size', 'inode', 'device', 'mtime_ns'],
                      'payload_hashed': False,
                      'limit': 'Metadata equality is not byte-content proof or a continuous write audit.'},
            'reference': {'status': 'UNKNOWN_WHOLE_TREE_UNCHANGED',
                          'observations': reference,
                          'limit': 'No entry whole-tree manifest. Tracked-only Git observations do not cover ignored/untracked reference trees or prove no writes. This checker performs no reference writes.'},
            'frozen_input_sha256': checked, 'model_content_hashes_deliberately_skipped': model_skips}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--self-sha256', required=True)
    args = parser.parse_args()
    require(not OUT.exists() and not OUT.is_symlink(), 'output exists; refuse overwrite')
    require(sha(__file__) == args.self_sha256, 'reviewed self SHA mismatch')
    # Refusal here creates no output and performs no binary/model/source audit.
    gate = terminal_gate()
    cleanup = current_cleanup()
    report = {'schema': 1, 'started_t': time.time(), 'checker_sha256': args.self_sha256,
              'terminal_gate': gate, 'cleanup': cleanup, 'failure': None,
              'scoped_checks_passed': False, 'whole_tree_readonly_proven': False,
              'performance_or_resource_acceptance': False, 'goal_complete': False}
    try:
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
