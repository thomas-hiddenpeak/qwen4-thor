"""Explicit self-entry capability; never migrate another process or reset state.

The privileged installer enters the observer leaf before releasing its admin
lock, drops all user/group privileges, and execs the requested service command.
Service entry is opened only after permanent UID drop. Kernel common-ancestor
checks confine this descriptor to the installed subtree. Each child consumes
its copy before any Guardian or model is executed; no root-open FD crosses exec.
"""
from copy import deepcopy
import ctypes
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
from tools.serve_recovery import ledger, resource_install as install
from tools.serve_recovery import resource_kernel as kernel, store

DIRECTORY = 'Q4T_RESOURCE_DIRECTORY'
ENTRY_FD = 'Q4T_RESOURCE_ENTRY_FD'


def validate(binding):
    ledger.keys(binding, ('directory', 'publication'))
    ledger.require(type(binding['directory']) is str and
        1 <= len(binding['directory']) <= 4096 and Path(binding['directory']).is_absolute(),
        'invalid resource installation binding')
    value = binding['publication']
    ledger.keys(value, ('format', 'service_id', 'domain_id', 'runtime_uid', 'runtime_gid',
        'revision', 'phase', 'record', 'admission_enabled', 'model_actions', 'device_authorized'))
    ledger.require(value['phase'] == 'ready' and value['record'] is not None and
        value['admission_enabled'] is True and value['model_actions'] is False and
        value['device_authorized'] is False, 'resource binding lacks ready installation')
    private = {name: value[name] for name in ('format', 'service_id', 'domain_id',
        'runtime_uid', 'runtime_gid', 'revision', 'phase', 'record')}
    install.validate(dict(private, policy=value['record']['policy']))
    return binding


def command_valid(command):
    ledger.require(type(command) is list and 1 <= len(command) <= 48 and
        os.path.isabs(command[0]) and all(type(arg) is str and len(arg) <= 4096 and
        '\0' not in arg for arg in command), 'bounded absolute resource command required')


def successor(previous, current, actual_boot):
    """Pure lineage contract; callers must also verify actual live capability."""
    validate(previous); validate(current)
    old, new = previous['publication'], current['publication']
    ledger.require(previous['directory'] == current['directory'] and
        all(old[key] == new[key] for key in ('format', 'service_id', 'domain_id',
            'runtime_uid', 'runtime_gid')) and new['revision'] > old['revision'] and
        all(old['record'][key] == new['record'][key] for key in
            ('service_id', 'domain_id', 'runtime_uid', 'root', 'policy')) and
        old['record']['boot_id'] != actual_boot == new['record']['boot_id'],
        'resource renewal does not continue the original installation')


def enter(directory, command):
    """Root-only explicit entry. No child exists before membership and UID drop."""
    command_valid(command)
    entry_fd = None
    try:
        with install.exclusive(directory) as directory:
            value = install.load(directory)
            if value['phase'] == 'renewing' or value['phase'] == 'ready' and \
                    value['record']['boot_id'] != store.stamp().boot_id:
                install.renew_locked(directory, value)
                value = install.load(directory)
            ledger.require(value['phase'] == 'ready', 'resource entry has been revoked')
            ledger.require(install.read_public(directory) == install.public_value(value),
                           'resource private/public authority differs')
            with kernel.Scope(value['record']) as scope:
                outer_fd = scope.join_descriptor('outer')
                try: scope.join_self('outer', outer_fd)
                finally: os.close(outer_fd)
        # Admin removal now sees this process in outer until it actually exits.
        libc = ctypes.CDLL(None, use_errno=True)
        ledger.require(libc.prctl(38, 1, 0, 0, 0) == 0, 'cannot set no_new_privs')
        from tools.serve_recovery import runtime_identity
        runtime_identity.drop(value['runtime_uid'], value['runtime_gid'])
        with kernel.Scope(value['record']) as scope:
            entry_fd = scope.join_descriptor('service')
        os.set_inheritable(entry_fd, True)
        env = {key: val for key, val in os.environ.items()
               if not key.startswith(('LD_', 'PYTHON', 'Q4T_RESOURCE_'))}
        env.update({DIRECTORY: str(directory), ENTRY_FD: str(entry_fd)})
        os.execve(command[0], command, env)
    finally:
        if entry_fd is not None: os.close(entry_fd)


class Capability:
    def __init__(self, directory, fd, service_id):
        self.directory = Path(directory).absolute()
        self.fd = None; self.scope = None
        try:
            self.publication = install.read_public(self.directory)
            ledger.require(self.publication['service_id'] == service_id and
                self.publication['runtime_uid'] == os.geteuid() != 0 and
                self.publication['runtime_gid'] == os.getegid(),
                'resource runtime identity differs')
            self.scope = kernel.Scope(self.publication['record'])
            self.scope.member('outer'); self.scope.entry_descriptor('service', fd)
            # fstat cannot prove the credentials used to open an inherited FD.
            # Reopen under our permanently unprivileged identity before use.
            self.fd = self.scope.join_descriptor('service')
            self.check()
        except BaseException: self.close(); raise

    def check(self):
        ledger.require(install.read_public(self.directory) == self.publication,
                       'resource admission changed')
        self.scope.member('outer'); self.scope.entry_descriptor('service', self.fd)
        return self

    def binding(self):
        self.check()
        return validate(dict(directory=str(self.directory), publication=deepcopy(self.publication)))

    def dispatch(self, command, python, script):
        command_valid(command); self.check()
        return [python, '-I', '-B', str(script), '--child', '--directory', str(self.directory),
                '--entry-fd', str(self.fd), '--expected', ledger.digest(self.publication), '--', *command]

    def close(self):
        if self.scope is not None: self.scope.close(); self.scope = None
        if self.fd is not None: os.close(self.fd); self.fd = None


def inherited(service_id):
    directory, raw_fd = os.environ.get(DIRECTORY), os.environ.get(ENTRY_FD)
    if directory is None and raw_fd is None: return None
    ledger.require(directory is not None and raw_fd is not None and raw_fd.isdecimal() and
                   3 <= int(raw_fd) < 2**31, 'incomplete resource entry capability')
    fd = int(raw_fd)
    try: result = Capability(directory, fd, service_id)
    finally: os.close(fd)
    os.environ.pop(DIRECTORY); os.environ.pop(ENTRY_FD)
    return result


def child(directory, fd, expected, command):
    command_valid(command)
    value = install.read_public(directory)
    ledger.require(ledger.digest(value) == expected and value['runtime_uid'] == os.geteuid() != 0
        and value['runtime_gid'] == os.getegid(), 'resource child installation changed')
    try:
        with kernel.Scope(value['record']) as scope:
            scope.member('outer'); scope.entry_descriptor('service', fd)
            safe_fd = scope.join_descriptor('service')
            try: scope.join_self('service', safe_fd)
            finally: os.close(safe_fd)
    finally: os.close(fd)
    # The entry FD must never leak into Guardian, controller or model code.
    os.execve(command[0], command, dict(os.environ))


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--child', action='store_true')
    parser.add_argument('--entry-fd', type=int)
    parser.add_argument('--expected')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if args.child: child(args.directory, args.entry_fd, args.expected, command)
    else: enter(args.directory, command)


if __name__ == '__main__': main()
