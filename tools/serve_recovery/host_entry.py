"""Protected native entry shared by manual launch and optional host adapters."""
import ctypes
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
from tools.serve_recovery import deployment, host_artifact as artifact, host_install as host
from tools.serve_recovery import ledger, lifecycle, outer_manager, outer_store
from tools.serve_recovery import resource_launch, service


def no_new_privileges():
    libc = ctypes.CDLL(None, use_errno=True)
    ledger.require(libc.prctl(38, 1, 0, 0, 0) == 0, 'host entry requires no_new_privs')


def check(directory):
    value = host.public(directory)
    ledger.require(os.getresuid() == (value['runtime_uid'],)*3 and
        os.getresgid() == (value['runtime_gid'],)*3, 'host runtime identity differs')
    artifact.verify(directory, value['artifact'])
    state = Path(value['state']); chosen = value['artifact']
    with deployment.claim(state, shared=True):
        desc = outer_manager.selected(state, chosen['package'], chosen['manifest_sha256'])
        ledger.require(desc['service_id'] == value['service_id'], 'host service identity differs')
        outer_manager.ready(state, desc)
        policy = lifecycle.read_family(state, desc, outer_store.Store)
    return dict(checked=True, service_id=value['service_id'], runtime_uid=os.geteuid(),
                manifest_sha256=chosen['manifest_sha256'], outer_enabled=policy['enabled'])


def privileged(directory, operation, *, daemon=False):
    ledger.require(os.geteuid() == 0, 'host entry requires explicit root invocation')
    with host.exclusive(directory) as directory:
        value = host.load(directory)
        ledger.require(value['phase'] in ('ready', 'revoking') and
            (operation == 'stop' or value['phase'] == 'ready' and value['enabled']),
            'host entry is disabled or not ready')
        root = artifact.verify(directory, value['artifact'])
        ledger.require(root == ROOT, 'privileged entry must execute the installed protected source')
        ledger.require(host.public(directory) == value, 'host private/public authority differs')
        command = [value['artifact']['python']['path'], '-I', '-B',
            str(root/'tools/serve_recovery/host_entry.py'),
            '--runtime-stop' if operation == 'stop' else '--runtime', '--directory', str(directory)]
        if daemon: command.append('--daemon')
        if operation == 'enter':
            host.check_resource(value)
            resource_launch.enter(value['resource']['directory'], command)
        else:
            # Explicit stop needs no resource admission and works while the
            # host entry is disabled/revoking. Core stop owns its normal locks.
            no_new_privileges(); os.setgroups([])
            os.setresgid(value['runtime_gid'], value['runtime_gid'], value['runtime_gid'])
            os.setresuid(value['runtime_uid'], value['runtime_uid'], value['runtime_uid'])
            os.execve(command[0], command, host.environment())


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    operations = parser.add_mutually_exclusive_group(required=True)
    for name in ('check', 'enter', 'stop', 'runtime', 'runtime-stop'):
        operations.add_argument('--'+name, action='store_true')
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--daemon', action='store_true')
    args = parser.parse_args(argv)
    if args.enter or args.stop:
        return privileged(args.directory, 'stop' if args.stop else 'enter', daemon=args.daemon)
    no_new_privileges(); checked = check(args.directory)
    if args.check:
        print(json.dumps(checked, sort_keys=True)); return 0
    value = host.public(args.directory); chosen = value['artifact']; state = Path(value['state'])
    if args.runtime_stop:
        return service.main(['stop', '--state', str(state)],
                            package=Path(chosen['package']), digest=chosen['manifest_sha256'])
    ledger.require(value['phase'] == 'ready' and value['enabled'] and checked['outer_enabled'],
                   'host runtime admission is disabled')
    # Resume never turns STOPPED into RUNNING or erases pending/fault history.
    if args.daemon:
        from tools.serve_recovery import outer_daemon
        result = outer_daemon.start(state, Path(chosen['package']), chosen['manifest_sha256'],
                                    activate=False, timeout=30)
    else:
        from tools.serve_recovery import outer_runtime
        result = outer_runtime.run(state, Path(chosen['package']), chosen['manifest_sha256'], activate=False)
    print(json.dumps(result, sort_keys=True))
    return 1 if args.daemon and result.get('startup_confirmed') is not True else 0


if __name__ == '__main__': raise SystemExit(main())
