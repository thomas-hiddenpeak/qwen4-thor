"""Identity and one-shot process helpers for this bounded experiment."""
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tarfile
import time

sys.dont_write_bytecode = True

R = Path(__file__).resolve().parent
W = R / 'source'


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def save(path, data):
    with Path(path).open('x') as stream:
        json.dump(data, stream, indent=2, allow_nan=False)
        stream.write('\n')


def environment():
    return {**{k: v for k, v in os.environ.items()
               if not k.startswith('Q4T_') and k != 'LD_PRELOAD'},
            'PYTHONDONTWRITEBYTECODE': '1'}


def export_identity(plan):
    """Compare the build source with every file in the committed export."""
    archive = R / 'candidate-source.tar'
    identity = read(R / 'candidate-source-identity.json')
    build = read(R / 'build-identity.json')
    for path in (archive, R / 'candidate-source-identity.json',
                 R / 'build-identity.json'):
        assert str(path) in plan['frozen_files'], path
    assert identity['commit'] == build['source_commit'] == plan['runtime_source_commit']
    assert identity['archive_sha256'] == sha(archive)
    assert build['binary_sha256'] == plan['runtime_binary_sha256']
    source = R / 'candidate-source'
    assert Path(identity['source']) == Path(build['source']) == source
    expected = set()
    with tarfile.open(archive) as stream:
        for member in stream:
            target = source / member.name
            assert target.absolute().is_relative_to(source)
            assert '..' not in Path(member.name).parts and not member.name.startswith('/')
            if member.isdir():
                assert target.is_dir() and not target.is_symlink(), member.name
                continue
            expected.add(member.name)
            if member.issym():
                assert target.is_symlink() and os.readlink(target) == member.linkname
            else:
                assert member.isfile() and target.is_file() and not target.is_symlink()
                with stream.extractfile(member) as content:
                    assert sha(target) == hashlib.file_digest(content, 'sha256').hexdigest(), member.name
    actual = {str(p.relative_to(source)) for p in source.rglob('*')
              if p.is_file() or p.is_symlink()}
    assert actual == expected, sorted(actual ^ expected)
    cache = R / 'candidate-build/CMakeCache.txt'
    assert sha(cache) == build['cmake_cache_sha256']
    assert sha(R / 'candidate-build/compile_commands.json') == build['compile_commands_sha256']
    assert ('CMAKE_HOME_DIRECTORY:INTERNAL=' + str(source)) in cache.read_text().splitlines()
    return {'archive_sha256': identity['archive_sha256'],
            'source_commit': identity['commit'], 'export_files': len(expected),
            'source_path': str(source), 'verified_t': time.time()}


def frozen(plan_sha):
    assert sha(R / 'execution-plan.json') == plan_sha
    plan = read(R / 'execution-plan.json')
    assert sha(R / 'plan.json') == plan['phase_plan_sha256']
    assert sha(plan['runtime_binary_path']) == plan['runtime_binary_sha256']
    assert subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=W,
                                   text=True).strip() == plan['runtime_source_commit']
    assert not subprocess.check_output(['git', 'status', '--porcelain'], cwd=W).strip()
    for path, digest in plan['frozen_files'].items():
        assert sha(path) == digest, path
    export_identity(plan)
    return plan


def prerequisite(name, plan):
    record = read(R / (name + '-decision.json'))
    assert record['passed'] is True, name
    assert record['runtime_binary_sha256'] == plan['runtime_binary_sha256']
    assert record['runtime_source_commit'] == plan['runtime_source_commit']
    assert record['plan_sha256'] == sha(R / 'execution-plan.json')
    assert record['ended_t'] <= time.time()
    for path, digest in record.get('source_sha256', {}).items():
        assert sha(path) == digest, path
    for path, digest in record.get('log_sha256', {}).items():
        assert sha(path) == digest, path
    return record


def run(command, directory, label, cwd=W, env=None, timeout=None,
        interrupt_grace=10):
    """Own the direct process group; allow Python HTTP cleanup on SIGINT."""
    sys.path.insert(0, str(W / 'tools/evalscope'))
    from run_budget_experiment import process_group_state
    directory.mkdir(parents=True, exist_ok=True)
    record = {'started_t': time.time(), 'command': command, 'cwd': str(cwd),
              'returncode': None, 'failure': None, 'automatic_retry': False,
              'timeout_s': timeout, 'signals': [], 'cleanup_complete': False}
    save(directory / (label + '-start.json'), record)
    proc = None
    previous_term = signal.getsignal(signal.SIGTERM)
    previous_int = signal.getsignal(signal.SIGINT)

    def interrupted(number, frame):
        raise KeyboardInterrupt('controller received signal ' + str(number))

    signal.signal(signal.SIGTERM, interrupted)
    try:
        with (directory / (label + '.log')).open('x') as log:
            proc = subprocess.Popen(command, cwd=cwd,
                env=environment() if env is None else env, stdout=log,
                stderr=subprocess.STDOUT, start_new_session=True)
            record['pid'] = record['pgid'] = proc.pid
            record['returncode'] = proc.wait(timeout=timeout)
            state = process_group_state(proc.pid)
            if state['live_pids'] or state['errors']:
                raise RuntimeError('owned child left live descendants or unknown group')
    except BaseException as error:
        record['failure'] = type(error).__name__ + ': ' + str(error)
        raise
    finally:
        # A second terminal signal must not bypass our first cleanup attempt.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        try:
            if proc is not None:
                assert proc.pid != os.getpgrp()
                state = process_group_state(proc.pid)
                record['group_before_cleanup'] = state
                for sig, grace in ((signal.SIGINT, interrupt_grace),
                                   (signal.SIGTERM, 5), (signal.SIGKILL, 5)):
                    if not state['live_pids'] and not state['errors']:
                        break
                    try:
                        os.killpg(proc.pid, sig)
                        record['signals'].append({'signal': sig.name, 't': time.time()})
                    except ProcessLookupError:
                        pass
                    except OSError as error:
                        record.setdefault('cleanup_errors', []).append(str(error))
                    end = time.monotonic() + grace
                    while True:
                        proc.poll()
                        state = process_group_state(proc.pid)
                        if (not state['live_pids'] and not state['errors'] or
                                time.monotonic() >= end):
                            break
                        time.sleep(.05)
                proc.wait(timeout=5)
                record['returncode'] = proc.returncode
                record['group_after_cleanup'] = process_group_state(proc.pid)
                final = record['group_after_cleanup']
                record['cleanup_complete'] = not (final['live_pids'] or
                    final['zombie_pids'] or final['errors'] or
                    record.get('cleanup_errors'))
        except BaseException as error:
            record.setdefault('cleanup_errors', []).append(type(error).__name__ + ': ' + str(error))
            record['cleanup_complete'] = False
        finally:
            record['ended_t'] = time.time()
            save(directory / (label + '-exit.json'), record)
            signal.signal(signal.SIGTERM, previous_term)
            signal.signal(signal.SIGINT, previous_int)
    if not record['cleanup_complete']:
        raise RuntimeError('controller process-group cleanup incomplete')
    return record


def cleanup_http_unit(command, record, plan):
    """Interrupt fallback: only the unit created by this exact wrapper PID."""
    sys.path.insert(0, str(W / 'tools/evalscope'))
    from isolated_service import unit_properties
    output = Path(command[command.index('--output') + 1]).resolve()
    assert output.is_relative_to(W / '.q4t-work/evidence')
    protocol_path = output / 'protocol.json'
    if not protocol_path.exists():
        return {'attempted': False, 'reason': 'wrapper made no protocol/unit claim'}
    protocol = read(protocol_path)
    unit = protocol['unit']
    assert re.fullmatch(r'q4t-ram-\d+-' + str(record['pid']) + r'\.service', unit)
    assert protocol['binary_sha256'] == plan['runtime_binary_sha256']
    assert protocol['binary'] == plan['runtime_binary_path']
    result = {'unit': unit, 'protocol_sha256': sha(protocol_path), 'attempted': False}
    launch = output / 'http/isolation/launch.json'
    props = unit_properties(unit)
    result['before'] = props
    if props.get('LoadState') != 'not-found':
        assert launch.is_file(), 'refuse unit without exact launch ownership evidence'
        evidence = read(launch)
        assert evidence['unit'] == unit
        assert '--unit=' + unit in evidence['argv']
        assert plan['runtime_binary_path'] in evidence['argv']
        result['launch_sha256'] = sha(launch)
        result['attempted'] = True
        for action, timeout_s in [('stop', 40), ('reset-failed', 15)]:
            child = subprocess.run(['sudo', '-n', 'systemctl', action, unit],
                capture_output=True, text=True, timeout=timeout_s)
            result[action] = {'returncode': child.returncode,
                              'stdout': child.stdout, 'stderr': child.stderr}
    result['after'] = unit_properties(unit)
    result['unit_removed'] = result['after'].get('LoadState') == 'not-found'
    group = output / 'runner-process-group.json'
    result['inner_runner_cleanup'] = read(group) if group.exists() else None
    result['inner_runner_cleanup_unknown'] = not group.exists()
    return result
