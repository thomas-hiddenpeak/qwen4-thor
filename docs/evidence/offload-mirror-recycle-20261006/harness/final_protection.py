"""Bounded protection/owned-cleanup audit; never replay acceptance or payloads."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

R = Path(__file__).resolve().parent
W = R / 'source'
MAIN = R.parent.parent
MODEL = MAIN.parent / 'llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream'
CONFIGS = {MODEL / 'config.json', MODEL / 'model.safetensors.index.json'}
ENTRY_SHA = 'ce67e9a6cbfea246bb511dd635fb18fd39d60fd09ef0589739ff69db8f4aaeea'
MODEL_ENTRY_SHA = '31cfb5f2cae36d75f84dcd1b27d7a11ce5972e2b6ba9637b9eb8493cd91dd0f4'
PLAN_SHA = '5e9f1627c85cd21a7467caed538fb80bc88cedb534f59fed6c094a922be8537e'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    path = Path(path).resolve()
    require(not path.is_relative_to(MODEL) or path in CONFIGS,
            'refuse model payload/content hashing: ' + str(path))
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def git(path, *args):
    return subprocess.check_output(['git', *args], cwd=path, timeout=30)


def no_processes(state):
    return all(not state[key] for key in ('live_pids', 'zombie_pids', 'errors'))


def process_groups():
    """One /proc metadata snapshot; observe groups, never signal processes."""
    groups, errors = {}, []
    for path in Path('/proc').iterdir():
        if not path.name.isdecimal():
            continue
        try:
            fields = (path / 'stat').read_text().rsplit(')', 1)[1].split()
            group = int(fields[2])
            groups.setdefault(group, []).append(
                {'pid': int(path.name), 'state': fields[0]})
        except FileNotFoundError:
            continue
        except (OSError, ValueError, IndexError) as error:
            errors.append({'pid': path.name, 'error': str(error)})
    require(not errors, 'process-group visibility incomplete: ' + str(errors))
    return groups


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--expected-head', required=True)
    parser.add_argument('--self-label', default='final-protection-01')
    parser.add_argument('--output', type=Path, default=R / 'final-protection.json')
    args = parser.parse_args()
    require(re.fullmatch(r'[0-9a-f]{40}', args.expected_head), 'invalid HEAD')
    require(re.fullmatch(r'[a-z0-9][a-z0-9-]*', args.self_label), 'invalid label')
    output = args.output.resolve()
    require(output.parent == R and output.suffix == '.json', 'output outside R')
    report = dict(schema=1, passed=False, started_t=time.time(), failure=None,
                  source_sha256={}, checks={}, new_HTTP=0, model_runs=0)

    def bind(path):
        path = Path(path).resolve()
        require(path.stat().st_size <= 16 << 20, 'metadata file too large')
        report['source_sha256'][str(path)] = sha(path)
        return json.loads(path.read_text())

    with output.open('x') as stream:
        try:
            for name, digest in [('entry.json', ENTRY_SHA),
                                 ('model-entry.json', MODEL_ENTRY_SHA),
                                 ('execution-plan.json', PLAN_SHA)]:
                require(sha(R / name) == digest, name + ' identity differs')
            entry, model, plan = (bind(R / name) for name in
                                 ('entry.json', 'model-entry.json',
                                  'execution-plan.json'))
            require(len(entry['main_status']) == 7, 'expected seven MAIN changes')
            require(git(MAIN, 'rev-parse', 'HEAD').decode().strip() ==
                    entry['main_head'], 'MAIN HEAD differs')
            require(git(MAIN, 'status', '--porcelain').decode().splitlines() ==
                    entry['main_status'], 'MAIN status differs')
            require(hashlib.sha256(git(MAIN, 'diff', '--binary', 'HEAD')).hexdigest()
                    == entry['main_diff_sha256'], 'MAIN full diff differs')
            require(sha(entry['main_binary']['path']) ==
                    entry['main_binary']['sha256'], 'MAIN binary differs')
            require(git(MAIN, 'status', '--porcelain', '--untracked-files=no',
                        '--', 'reference').decode().splitlines() ==
                    entry['reference_tracked_status'], 'reference status differs')
            report['checks']['MAIN'] = {'head': entry['main_head'],
                'status': entry['main_status'], 'diff_sha256': entry['main_diff_sha256'],
                'binary_sha256': entry['main_binary']['sha256']}
            require(len(entry['old_worktrees']) == 3, 'expected three old trees')
            for path, head in entry['old_worktrees'].items():
                require(git(path, 'rev-parse', 'HEAD').decode().strip() == head,
                        'old HEAD differs: ' + path)
                require(not git(path, 'status', '--porcelain').strip(),
                        'old tree dirty: ' + path)
            report['checks']['old_worktrees'] = entry['old_worktrees']
            files = []
            for path in sorted(MODEL.rglob('*')):
                if path.is_file():
                    stat = path.stat()
                    files.append(dict(path=str(path), size=stat.st_size,
                        inode=stat.st_ino, device=stat.st_dev,
                        mtime_ns=stat.st_mtime_ns))
            require(len(files) == 228 and files == model['files'],
                    'model file metadata differs')
            require(set(map(Path, model['config_index_sha256'])) == CONFIGS,
                    'unexpected model content hash set')
            require({str(path): sha(path) for path in CONFIGS} ==
                    model['config_index_sha256'], 'model config/index differs')
            require(model['weight_payload_hashed'] is False,
                    'entry incorrectly claims payload hashes')
            report['checks']['model'] = dict(metadata_records=228,
                config_index_sha256=model['config_index_sha256'],
                payload_hashed=False, payload_read=False)
            head = git(W, 'rev-parse', 'HEAD').decode().strip()
            require(head == args.expected_head, 'current expected HEAD differs')
            require(not git(W, 'status', '--porcelain').strip(), 'current tree dirty')
            require(git(W, 'branch', '--show-current').decode().strip() ==
                    entry['working_branch'], 'current branch differs')
            subprocess.run(['git', 'merge-base', '--is-ancestor',
                plan['runtime_source_commit'], head], cwd=W, check=True, timeout=30)
            changes = git(W, 'diff', '--name-only', plan['runtime_source_commit'],
                          head).decode().splitlines()
            require(all(path.startswith('docs/') for path in changes),
                    'current HEAD changes frozen runtime/source outside docs')
            require(sha(plan['runtime_binary_path']) == plan['runtime_binary_sha256'],
                    'accepted experiment binary differs')
            report['checks']['current_tree'] = dict(head=head,
                runtime_source_commit=plan['runtime_source_commit'],
                documentation_changes=changes,
                runtime_binary_sha256=plan['runtime_binary_sha256'])

            # Only this stage's flat owned receipts and direct-contract subdirs.
            # Do not traverse candidate-source, old worktrees or raw resources.
            starts = list(R.glob('*-start.json'))
            direct_dirs = [path for path in R.iterdir() if path.is_dir() and
                           re.fullmatch(r'(host|numerical)-\d{2}', path.name)]
            require(len(direct_dirs) <= 12, 'too many direct-contract directories')
            for path in direct_dirs:
                starts.extend(path.glob('*-start.json'))
            require(len(starts) <= 128, 'owned receipt bound exceeded')
            self_start = R / (args.self_label + '-start.json')
            require(self_start in starts, 'run checker through owned run helper')
            own = bind(self_start)
            require(str(Path(__file__).resolve()) in own['command'],
                    'self receipt does not own this checker')
            groups = process_groups()
            closures = []
            for start in sorted(starts):
                if start == self_start:
                    continue
                initial = bind(start)
                end = start.with_name(start.name[:-11] + '-exit.json')
                require(end.is_file(), 'owned start lacks terminal receipt: ' + str(start))
                terminal = bind(end)
                require(terminal['command'] == initial['command'] and
                        terminal['started_t'] == initial['started_t'],
                        'owned start/exit identity differs: ' + str(start))
                require(terminal['cleanup_complete'] is True and
                        no_processes(terminal['group_after_cleanup']) and
                        type(terminal['returncode']) is int,
                        'owned terminal cleanup incomplete: ' + str(end))
                pgid = terminal['pgid']
                require(type(pgid) is int and pgid > 0 and pgid == terminal['pid'],
                        'owned process group identity invalid')
                require(not groups.get(pgid),
                        'owned PGID currently exists (possible reuse): ' + str(pgid))
                closures.append(dict(start=str(start), exit=str(end), pgid=pgid,
                    returncode=terminal['returncode'], failure=terminal['failure'],
                    cleanup_complete=True, currently_absent=True))

            # Query only exact units claimed by the frozen service group outputs.
            units, unstarted, no_service = [], [], []
            for group in plan['groups']:
                directory = W / '.q4t-work/evidence' / group['id']
                if not directory.exists():
                    unstarted.append(group['id'])
                    continue
                protocol = bind(directory / 'protocol.json')
                controller = bind(R / (group['id'] + '-controller-exit.json'))
                unit = protocol['unit']
                require(re.fullmatch(r'q4t-ram-\d+-' + str(controller['pid']) +
                                     r'\.service', unit), 'unit owner differs')
                require(protocol['binary_sha256'] == plan['runtime_binary_sha256'],
                        'unit binary identity differs')
                wrapper = bind(directory / 'wrapper-exit.json')
                if wrapper.get('failure') == 'cold payload gate failed; no service started':
                    # This exact frozen wrapper exit occurs before launching
                    # the monitor/runner/service. Preserve it as a failure,
                    # without requiring service-only artifacts that cannot exist.
                    gate = bind(directory / 'cache-gate.json')
                    require(wrapper['runner_rc'] is None and
                            wrapper['monitor_rc'] is None and
                            wrapper['cleanup_failed'] is False and
                            gate['cold_payload_established'] is False and
                            type(controller['returncode']) is int and
                            controller['returncode'] != 0 and
                            not (directory / 'runner-process-group.json').exists() and
                            not (directory / 'http').exists() and
                            not (directory / 'memory').exists(),
                            'cold failure has unexpected started-service artifacts')
                    no_service.append(dict(group=group['id'], unit=unit,
                        service_started=False,
                        terminal_reason=wrapper['failure'],
                        controller_returncode=controller['returncode']))
                    continue
                inner = bind(directory / 'runner-process-group.json')
                require(inner['cleanup_complete'] is True and inner['runner_reaped']
                        and no_processes(inner['after_cleanup']),
                        'inner HTTP runner cleanup incomplete')
                require(not groups.get(inner['pgid']), 'inner runner PGID exists')
                cleanup = bind(directory / 'http/isolation/cleanup.json')
                require(cleanup['unit_removed'] and
                        cleanup['properties_after']['MainPID'] == '0',
                        'recorded service cleanup incomplete')
                command = ['sudo', '-n', 'systemctl', 'show', unit,
                           '--property=LoadState', '--property=MainPID',
                           '--property=ActiveState']
                observation = subprocess.run(command, capture_output=True,
                    text=True, timeout=5)
                props = dict(line.split('=', 1) for line in
                             observation.stdout.splitlines() if '=' in line)
                require(props.get('LoadState') == 'not-found' and
                        props.get('MainPID') == '0', 'owned unit still exists')
                units.append(dict(group=group['id'], unit=unit,
                    properties=props, show_returncode=observation.returncode,
                    runner_pgid=inner['pgid']))
            report['checks']['owned_cleanup'] = dict(receipts=closures, units=units,
                unstarted_groups=unstarted, no_service_terminals=no_service,
                self_excluded=str(self_start),
                current_checker_pgid=os.getpgrp(), proc_snapshot_groups=len(groups))
            report.update(passed=True, status='FINAL_PROTECTION_PASS')
        except BaseException as error:
            report.update(status='FINAL_PROTECTION_FAILED',
                          failure=type(error).__name__ + ': ' + str(error))
        report['source_sha256'][str(Path(__file__).resolve())] = sha(__file__)
        report['limits'] = [
            'Model size/inode/device/mtime and config/index equality do not prove '
            'weight byte equality or exclude transient writes; payloads never opened.',
            'Reference scope is tracked Git status only; old delivered trees use '
            'HEAD/status, without rereading old evidence or ignored files.',
            'Owned receipt cleanup and current PGID/unit absence do not prove '
            'global GPU/process idleness; PGID reuse fails conservatively.',
            'Current checker receipt is excluded while it runs; root must close '
            'its own actual exit/cleanup and later push/delivery metadata separately.',
            'Failed test/controller outcomes remain visible; protection PASS is '
            'not quality, numerical, speed, resource, full-matrix or 54GB acceptance.',
            'No old raw-resource/trace reread, model inference, tests, GPU query '
            'or cache advice is performed. Only exact owned systemd units are queried.']
        report['ended_t'] = time.time()
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps({key: report[key] for key in ('passed', 'status', 'failure')}),
          flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
