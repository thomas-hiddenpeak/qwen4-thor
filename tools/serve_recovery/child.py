"""CPU exec barrier: a model cannot load before its identity is durable.

Only the private parent channel carries a permit. No permit/status file is
accepted, no shell is invoked, and the inherited lease is never unlocked.
"""
import argparse
import fcntl
import os
from pathlib import Path
import socket
import stat
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.serve_recovery import journal, ledger, progress


def progress_environment(args, permit):
    """Only the named observation socket survives; its failure is auxiliary."""
    env = {key: value for key, value in os.environ.items()
           if not key.startswith('Q4T_PROGRESS_')}
    env['Q4T_CONTROLLER_GENERATION'] = str(args.controller_generation)
    env['Q4T_MODEL_GENERATION'] = str(permit['model_generation'])
    fd = args.progress_fd
    if fd is None or fd < 3 or fd in (args.fd, args.lease_fd):
        return env
    sock = None
    try:
        sock = socket.socket(fileno=fd)
        journal.verify_parent(sock, args.parent_pid, args.parent_start)
        ledger.require(type(args.service_id) is str and
            progress.UUID.fullmatch(args.service_id) is not None,
            'invalid progress service identity')
        sock.setblocking(False)
        sock.set_inheritable(True)
        env.update(Q4T_PROGRESS_FD=str(fd),
            Q4T_PROGRESS_PARENT_START=str(args.parent_start),
            Q4T_SERVICE_ID=args.service_id)
        sock.detach()
    except (OSError, ValueError):
        if sock is not None:
            sock.close()
        else:
            try:
                os.close(fd)
            except OSError:
                pass
    return env


def verify_lease(fd, path):
    ledger.require(fd >= 3 and Path(path).is_absolute(), 'invalid lease input')
    held = os.fstat(fd)
    probe = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        named = os.fstat(probe)
        ledger.require(stat.S_ISREG(held.st_mode) and held.st_nlink == 1 and
                       (held.st_dev, held.st_ino) == (named.st_dev, named.st_ino)
                       and fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDWR,
                       'lease identity changed')
        locks = [line.split() for line in
                 Path(f'/proc/self/fdinfo/{fd}').read_text().splitlines()
                 if line.startswith('lock:')]
        ledger.require(len(locks) == 1 and len(locks[0]) == 9 and
                       locks[0][2:5] == ['FLOCK', 'ADVISORY', 'WRITE'] and
                       locks[0][7:] == ['0', 'EOF'], 'inherited lease lost')
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        raise ledger.LedgerError('lease is not continuously held')
    finally:
        os.close(probe)


def run(args):
    command = args.command
    ledger.require(command and command[0] == '--', 'exec separator required')
    command = command[1:]
    ledger.require(1 <= len(command) <= 64 and os.path.isabs(command[0]) and
                   all(len(part) <= 4096 and '\0' not in part for part in command),
                   'invalid bounded exec command')
    with socket.socket(fileno=args.fd) as channel:
        journal.verify_parent(channel, args.parent_pid, args.parent_start)
        verify_lease(args.lease_fd, args.lease_path)
        journal.send(channel, dict(version=1, pid=os.getpid(),
            start_ticks=journal.process_start(os.getpid()),
            boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip()))
        channel.settimeout(10)
        permit = journal.receive(channel)
        ledger.keys(permit, ('version', 'ticket', 'controller_generation',
                             'model_generation'))
        ledger.require(type(permit['version']) is int and permit['version'] == 1
            and ledger.identifier(permit['ticket']) and
            ledger.integer(permit['controller_generation'], minimum=1) and
            permit['controller_generation'] == args.controller_generation and
            ledger.integer(permit['model_generation'], minimum=1),
            'invalid model exec permit')
        ledger.require(os.getppid() == args.parent_pid and
                       journal.process_start(args.parent_pid) == args.parent_start,
                       'controller died before model exec')
        verify_lease(args.lease_fd, args.lease_path)
        # Only the lease and validated optional observation endpoint survive.
        # The permit barrier must close, including for an interpreter target.
        os.set_inheritable(args.fd, False)
        os.set_inheritable(args.lease_fd, True)
        os.execve(command[0], command, progress_environment(args, permit))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fd', type=int, required=True)
    parser.add_argument('--parent-pid', type=int, required=True)
    parser.add_argument('--parent-start', type=int, required=True)
    parser.add_argument('--lease-fd', type=int, required=True)
    parser.add_argument('--lease-path', required=True)
    parser.add_argument('--controller-generation', type=int, required=True)
    parser.add_argument('--progress-fd', type=int)
    parser.add_argument('--service-id')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    run(parser.parse_args())


if __name__ == '__main__':
    try:
        main()
    except (OSError, ledger.LedgerError) as error:
        print(f'model exec barrier rejected: {error}', file=sys.stderr)
        raise SystemExit(1)
