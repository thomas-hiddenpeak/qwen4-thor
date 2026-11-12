"""Explicit one-shot cold business validation; never manages foreign services.

The administrator runs this only in an agreed maintenance window. One parent
retains the cooperative GPU lease through qualification, strict validation and
kernel rule removal. Production device-domain code owns every admission check.
There is no background recovery job, driver reset or automatic retry.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import runpy
import signal
import stat
import sys
import time
import traceback
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
# Load validation code as a file, leaving production namespaces untouched until
# the sealed candidate has been selected. Importing the checkout package here
# would correctly fail the production provenance gate before any GPU work.
business = SimpleNamespace(**runpy.run_path(str(ROOT/'tools/serve_recovery/cold_acceptance.py')))
domain = None


def require(value, reason):
    if not value: raise ValueError(reason)


def load_checked(plan):
    global domain
    if os.geteuid() == 0:
        from tools.serve_recovery import acceptance_owner
        acceptance_owner.verify(plan,business.LEGACY['verify'],reap,finish_child)
        # Pin the already verified package only after the owner check succeeds.
        # Reading private client policies as root is intentionally disallowed.
        *_,engine = business.LEGACY['load_modules'](plan)
        result = engine.read(Path(plan['package']['package'])/'manifest.json')
    else:
        result = business.LEGACY['verify'](plan)
    from tools.serve_recovery import device_domain
    domain = device_domain
    return result


def reap(pid, seconds):
    end = time.monotonic()+seconds
    while True:
        found, status = os.waitpid(pid, os.WNOHANG)
        if found: return os.waitstatus_to_exitcode(status)
        if time.monotonic() >= end: return None
        time.sleep(min(.05, max(0, end-time.monotonic())))


def finish_child(pid):
    """Signal only our unreaped direct child; it owns the strict guard cleanup."""
    result = reap(pid, 0)
    if result is not None: return result
    os.kill(pid, signal.SIGTERM)
    result = reap(pid, 30)
    if result is not None: return result
    os.kill(pid, signal.SIGKILL)
    return reap(pid, 1)


def execute(directory, lease_fd, lease_path, manifest, command, *, seconds=600):
    require(0 < seconds <= 600 and domain is not None, 'cold host validation deadline/adapter bound')
    result = dict(status='ENV_ABORT', child_exit=None, domain_removed=False,
                  other_services_modified=False)
    pid = None
    try:
        result['arm'] = domain.arm(directory, lease_fd, lease_path)
        pid = os.fork()
        if pid == 0:
            try:
                domain.enter(directory, lease_fd, lease_path, manifest, command)
            except BaseException:
                traceback.print_exc()
            os._exit(1)
        code = reap(pid, seconds)
        if code is None: raise TimeoutError('cold host validation deadline reached')
        pid = None
        result['child_exit'] = code
        result['status'] = ('PASS' if code == 0 else
                            'ENV_ABORT' if code == 75 else 'VALIDATION_FAILED')
    except BaseException as error:
        result['error'] = repr(error)
    finally:
        if pid is not None:
            try: result['child_exit'] = finish_child(pid)
            except BaseException as error: result['child_cleanup_error'] = repr(error)
        try:
            # Never move or kill a process to force an empty domain. If owned
            # work remains, production disarm refuses and retains the fence.
            result['disarm'] = domain.disarm(directory, lease_fd, lease_path)
            result['domain_removed'] = True
        except BaseException as error:
            result.update(status='ENV_ABORT', domain_cleanup_error=repr(error))
    return result


def main():
    os.umask(0o077)
    def interrupted(*_args): raise KeyboardInterrupt('cold validation interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--device-state', type=Path)
    parser.add_argument('--check', action='store_true', help='verify sealed sources only; no host/GPU action')
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text())
    manifest = load_checked(plan)
    if args.check:
        print(json.dumps(dict(package_verified=True, device_module=domain.__file__,
                              service_id=plan['service_id'], model_actions=False)))
        return 0
    from tools.serve_recovery import ledger
    ledger.require(os.geteuid() == 0, 'explicit administrator entry required')
    out = Path(plan['output'])
    uid, gid = plan['runtime_uid'], plan['runtime_gid']
    ledger.require(uid > 0 and gid > 0 and out.stat().st_uid == uid and
        out.stat().st_gid == gid and int(os.environ['SUDO_UID']) == uid,
        'maintenance operator differs from frozen validation owner')
    # This private validation domain is never the default service's domain.
    expected = Path('/var/lib')/('q4t-cold-validation-'+plan['service_id'])
    ledger.require(args.device_state == expected and not expected.exists(),
                   'new explicitly named private validation domain required')
    receipt = plan['package']; package = Path(receipt['package'])
    control = package/'bin/q4t-device-control'
    from tools.serve_service import package as engine
    manifest = engine.read(package/'manifest.json')
    control_sha = manifest['files']['bin/q4t-device-control']
    lock_path = Path(plan['lease_path'])
    fd = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        ledger.require(stat.S_ISREG(info.st_mode) and info.st_uid == uid and
            (info.st_dev, info.st_ino) == (lock_path.stat().st_dev, lock_path.stat().st_ino),
            'frozen shared GPU lease changed')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        ledger.require(not lock_path.with_name(lock_path.name+'.recovery-required').exists(),
                       'unfinished shared GPU recovery')
        initialized = domain.initialize(expected, plan['service_id'], uid, gid,
                                        control, control_sha)
        started = time.monotonic()
        command = [sys.executable, '-I', '-B', str(Path(business.__file__).resolve()),
                   'guard', '--plan', str(args.plan.resolve())]
        result = execute(expected, fd, lock_path, receipt['manifest_sha256'], command)
        result.update(initialized=initialized, seconds=time.monotonic()-started,
                      device_state=str(expected), lease_held_through_disarm=True)
        # The root journal remains private. Export only a bounded review record.
        report = out/'host-result.json'
        ledger.require(not report.exists(), 'host validation result already exists')
        business.save(report, result); os.chown(report, uid, gid)
        os.lseek(fd, 0, os.SEEK_SET); os.ftruncate(fd, 0)
        os.write(fd, ledger.canonical(dict(state='released' if result['domain_removed']
            else 'cleanup_pending', output=str(out), host_result=str(report))))
        os.fsync(fd)
        print(json.dumps(result))
        return 0 if result['status'] == 'PASS' else 75
    finally:
        # Surviving owned work retains its inherited open-file-description.
        os.close(fd)


if __name__ == '__main__': sys.exit(main())
