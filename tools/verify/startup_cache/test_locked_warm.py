"""Bounded real CPU mmap/mlock contracts; at most two files / 4 MiB."""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import resource
import signal
import subprocess
import sys
import time

from locked_warm import LockedWarm, ROOT, identity, make_manifest, save


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def clean_summary(helper, held):
    summary = helper.summary
    assert summary and summary['held'] == held, summary
    assert summary['remaining_mappings'] == 0, summary
    assert summary['final_identity']['vmlck_bytes'] == 0, summary
    assert helper.exit_record['reaped'], helper.exit_record
    assert not Path('/proc', str(helper.process.pid)).exists()
    return summary


def orphan_owner(output, files, privileged=False):
    helper = LockedWarm(output, make_manifest(files, fixture=True), timeout=15,
                        use_sudo=privileged)
    helper.grant()
    helper.acquire()
    save(output / 'orphan-owner.json', dict(
        helper.identity, launcher_pid=helper.process.pid))
    # Deliberate death of only this test owner; parent-death SIGTERM must
    # release the real file locks in the adopted helper.
    os._exit(0)


def targeted_final(out, manifest, files, initial):
    results = {}
    class ExitLogFailure:
        def __init__(self, stream):
            self.stream = stream
        @property
        def closed(self):
            return self.stream.closed
        def write(self, value):
            if json.loads(value)['event'] == 'exit':
                raise OSError(28, 'injected first write failure at exit')
            return self.stream.write(value)
        def flush(self):
            return self.stream.flush()
        def close(self):
            return self.stream.close()

    with LockedWarm(out / 'exit_log_failure', manifest, timeout=15) as helper:
        helper.grant()
        helper.acquire()
        helper._log = ExitLogFailure(helper._log)
        try:
            helper.release()
            raise AssertionError('exit log failure incorrectly accepted')
        except RuntimeError as error:
            assert 'persistence failed' in str(error), error
        assert clean_summary(helper, True)['status'] == 'PASS'
        recorded = json.loads((helper.output / 'exit.json').read_text())
        assert recorded['logging_errors'] and not recorded['release_success']
        assert not helper.exit_record['release_success']
        results['exit_log_failure'] = dict(status='PASS')

    libc = ctypes.CDLL(None, use_errno=True)
    assert libc.prctl(36, 1, 0, 0, 0) == 0
    case = out / 'privileged_parent_death'
    command = [sys.executable, '-B', str(Path(__file__).resolve()),
               '--output', str(case), '--orphan-privileged',
               '--orphan-owner', *map(str, files)]
    run = subprocess.run(command, capture_output=True, timeout=20)
    save(out / 'parent-death-command.json', dict(
        command=command, returncode=run.returncode,
        stdout=run.stdout.decode(), stderr=run.stderr.decode()))
    run.check_returncode()
    born = json.loads((case / 'orphan-owner.json').read_text())
    deadline = time.monotonic() + 5
    while True:
        pid, status = os.waitpid(born['launcher_pid'], os.WNOHANG)
        if pid:
            break
        if time.monotonic() >= deadline:
            raise AssertionError('privileged launch tree did not exit')
        time.sleep(.02)
    summary = rows(case / 'child-events.jsonl')[-1]
    assert summary['event'] == 'summary' and summary['status'] == 'FAILED'
    assert summary['held'] and summary['final_identity']['vmlck_bytes'] == 0
    assert summary['remaining_mappings'] == 0
    assert os.waitstatus_to_exitcode(status) == 125
    assert not Path('/proc', str(born['pid'])).exists()
    save(case / 'adopted-exit.json', dict(pid=pid, reaped=True,
                                        returncode=125, summary=summary))
    results['privileged_parent_death'] = dict(status='PASS')
    final = identity(os.getpid())
    save(out / 'parent-after.json', final)
    assert final['memlock'] == initial['memlock']
    assert final['vmlck_bytes'] == initial['vmlck_bytes'] == 0
    save(out / 'result.json', dict(status='PASS', cases=results,
        maximum_selected_bytes=4 << 20, gpu_used=False,
        privileged_startup_chain_tested=True,
        full_size_preparation_accepted=False,
        reused_unaffected_contracts=str(out.parent / 'locked-host-v4/result.json'),
        runtime_change='Only _finish defers final acceptance until exit logging '
                       'and stream closure finish; earlier 13 paths reused.'))
    print(json.dumps(dict(status='PASS', cases=len(results), output=str(out))))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--orphan-owner', nargs=2, type=Path)
    parser.add_argument('--orphan-privileged', action='store_true')
    parser.add_argument('--targeted-final', action='store_true')
    args = parser.parse_args()
    out = args.output.resolve()
    if args.orphan_owner:
        return orphan_owner(out, args.orphan_owner, args.orphan_privileged)
    if not out.is_relative_to(ROOT / '.q4t-work'):
        parser.error('output must be a new .q4t-work directory')
    out.mkdir(parents=True, exist_ok=False)
    sources = [Path(__file__).resolve(), Path(__file__).with_name('locked_warm.py')]
    save(out / 'sources.json', {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in sources})
    save(out / 'command.json', sys.argv)
    initial = identity(os.getpid())
    save(out / 'parent-before.json', initial)
    files = [out / 'fixture-a.bin', out / 'fixture-b.bin']
    for index, path in enumerate(files):
        with path.open('xb') as f:
            f.write(bytes([index + 1]) * (2 << 20))
    manifest = make_manifest(files, fixture=True)
    results = {}
    if args.targeted_final:
        return targeted_final(out, manifest, files, initial)
    try:
        with LockedWarm(out / 'success', manifest, timeout=15) as helper:
            ready_limit = helper.ready['identity']['memlock']
            helper.grant()
            helper.acquire()
            assert helper.held['identity']['vmlck_bytes'] == 4 << 20
            assert resource.getrlimit(resource.RLIMIT_MEMLOCK) == tuple(
                initial['memlock'])
            held_rows = rows(helper.output / 'child-events.jsonl')
            probes = [x for x in held_rows if x['event'] == 'mincore']
            assert len(probes) == 2 and all(
                x['resident_pages'] == x['pages'] == 512 for x in probes)
            assert helper.release()['release_success']
            summary = clean_summary(helper, True)
            assert summary['status'] == 'PASS'
            mmap_ptrs = [x['ptr'] for x in helper.events if x['event'] == 'mmap']
            unmap_ptrs = [x['ptr'] for x in helper.events if x['event'] == 'munmap']
            assert unmap_ptrs == list(reversed(mmap_ptrs))
            results['success'] = dict(status='PASS', initial_limit=ready_limit)

        for name, limit, successes in [('zero_quota', 0, 0),
                                        ('partial_quota', 2 << 20, 1)]:
            with LockedWarm(out / name, manifest, timeout=15) as helper:
                helper.grant(limit_bytes=limit)
                try:
                    helper.acquire()
                    raise AssertionError('low quota unexpectedly accepted')
                except RuntimeError as error:
                    assert 'stopped before held' in str(error), error
                helper.close()
                summary = clean_summary(helper, False)
                calls = [x for x in helper.events if x['event'] == 'mlock']
                assert sum(x['rc'] == 0 for x in calls) == successes
                assert calls[-1]['rc'] != 0 and calls[-1]['errno'] != 0
                assert len([x for x in helper.events
                            if x['event'] == 'munmap']) == successes + 1
                assert summary['status'] == 'FAILED'
                results[name] = dict(status='PASS', errno=calls[-1]['errno'])

        for name, held in [('eof_ready', False), ('eof_held', True),
                           ('sigterm_held', True), ('sigkill_held', True)]:
            with LockedWarm(out / name, manifest, timeout=15) as helper:
                if held:
                    helper.grant()
                    helper.acquire()
                if name.startswith('eof'):
                    helper.process.stdin.close()
                    helper.process.stdin = None
                elif name == 'sigterm_held':
                    helper.process.terminate()
                else:
                    helper.process.kill()
                if name != 'sigkill_held':
                    helper._wait('summary', 5)
                helper.process.wait(timeout=5)
                helper.close()
                if name != 'sigkill_held':
                    assert clean_summary(helper, held)['status'] == 'FAILED'
                else:
                    assert helper.process.returncode == -signal.SIGKILL
                    assert not Path('/proc', str(helper.process.pid)).exists()
                    assert helper.summary is None
                results[name] = dict(status='PASS',
                                     exit=helper.process.returncode)

        with LockedWarm(out / 'grant_denied', manifest, timeout=15) as helper:
            resource.prlimit(helper.process.pid, resource.RLIMIT_MEMLOCK,
                            (1 << 20, 1 << 20))
            try:
                helper.grant()
                raise AssertionError('missing privileged grant accepted')
            except PermissionError:
                pass
            helper.close()
            clean_summary(helper, False)
            assert not any(x['event'] == 'mmap' for x in helper.events)
            results['grant_denied'] = dict(status='PASS')

        with LockedWarm(out / 'held_timeout', manifest, timeout=.3) as helper:
            helper.grant()
            helper.acquire()
            helper._wait('summary', 3)
            helper.process.wait(timeout=3)
            helper.close()
            summary = clean_summary(helper, True)
            assert any('signal 14' in x for x in summary['errors'])
            results['held_timeout'] = dict(status='PASS')

        with LockedWarm(out / 'logging_failure', manifest, timeout=15) as helper:
            helper.grant()
            helper.acquire()
            helper._log.close()  # Inject failure only in the parent's log.
            helper.close()
            clean_summary(helper, True)
            assert helper.exit_record['logging_errors']
            assert not helper.exit_record['release_success']
            results['logging_failure'] = dict(status='PASS')

        for name in ('privileged_success', 'privileged_abort'):
            with LockedWarm(out / name, manifest, timeout=15,
                            use_sudo=True) as helper:
                assert helper.ready['identity']['memlock'] == [4 << 20] * 2
                assert helper.ready['identity']['uid'] == [os.getuid()] * 4
                assert all(int(v, 16) == 0 for v in
                           helper.ready['identity']['capabilities'].values())
                assert helper.ready['identity']['no_new_privs'] == 1
                helper.grant()
                helper.acquire()
                assert resource.getrlimit(resource.RLIMIT_MEMLOCK) == tuple(
                    initial['memlock'])
                if name == 'privileged_success':
                    helper.release()
                    assert clean_summary(helper, True)['status'] == 'PASS'
                else:
                    helper.close()
                    assert clean_summary(helper, True)['status'] == 'FAILED'
                assert not Path('/proc', str(helper.identity['pid'])).exists()
                results[name] = dict(status='PASS',
                                     helper_pid=helper.identity['pid'],
                                     launcher_pid=helper.process.pid)

        # Adopt only this test's orphaned helper so the test can reap it;
        # subreaper is local to the short-lived test process, never global.
        libc = ctypes.CDLL(None, use_errno=True)
        assert libc.prctl(36, 1, 0, 0, 0) == 0  # PR_SET_CHILD_SUBREAPER
        case = out / 'parent_death'
        command = [sys.executable, '-B', str(Path(__file__).resolve()),
                   '--output', str(case), '--orphan-owner', *map(str, files)]
        run = subprocess.run(command, capture_output=True, timeout=20)
        save(out / 'parent-death-command.json', dict(
            command=command, returncode=run.returncode,
            stdout=run.stdout.decode(), stderr=run.stderr.decode()))
        run.check_returncode()
        born = json.loads((case / 'orphan-owner.json').read_text())
        deadline = time.monotonic() + 5
        while True:
            pid, status = os.waitpid(born['pid'], os.WNOHANG)
            if pid:
                break
            if time.monotonic() >= deadline:
                current = identity(born['pid'])
                assert current['start_ticks'] == born['start_ticks']
                os.kill(born['pid'], signal.SIGKILL)
                os.waitpid(born['pid'], 0)
                raise AssertionError('parent-death cleanup timed out')
            time.sleep(.02)
        summary = rows(case / 'child-events.jsonl')[-1]
        assert summary['event'] == 'summary' and summary['status'] == 'FAILED'
        assert summary['final_identity']['vmlck_bytes'] == 0
        assert summary['remaining_mappings'] == 0
        assert os.waitstatus_to_exitcode(status) == 125
        save(case / 'adopted-exit.json', dict(pid=pid, reaped=True,
                                            returncode=125, summary=summary))
        results['parent_death'] = dict(status='PASS')
        final = identity(os.getpid())
        save(out / 'parent-after.json', final)
        assert final['memlock'] == initial['memlock']
        assert final['vmlck_bytes'] == initial['vmlck_bytes'] == 0
        save(out / 'result.json', dict(status='PASS', cases=results,
                                       maximum_selected_bytes=4 << 20,
                                       gpu_used=False, privileged_startup_chain_tested=True,
                                       full_size_preparation_accepted=False))
    except BaseException as error:
        save(out / 'result.json', dict(status='FAILED', cases=results,
                                       error=repr(error)))
        raise
    print(json.dumps(dict(status='PASS', cases=len(results), output=str(out))))


if __name__ == '__main__':
    main()
