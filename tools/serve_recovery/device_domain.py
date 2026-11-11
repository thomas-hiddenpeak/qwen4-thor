"""Explicit root device-domain deployment; never part of a healthy model loop.

The administrator chooses this optional exclusive mode. No other process is
stopped or moved. Kernel custody survives manager exit; admission requires fresh
independent facts and a durable charge. Management failure cannot send a model
signal, reset a driver, or refill either recovery budget.
"""
from contextlib import contextmanager
import ctypes
import fcntl
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import sys
import uuid

from tools.serve_recovery import (access_store, child, device_budget, device_control,
                                device_inventory, device_platform, device_store,
                                ledger, store)

FORMAT = 'q4t-device-domain-v1'
CGROUP = Path('/sys/fs/cgroup')
BPFFS = Path('/sys/fs/bpf')


def secure_directory(path, *, private=False):
    path = Path(path).absolute()
    ledger.require(path.resolve(strict=True) == path, 'device path must be canonical')
    for entry in (path, *path.parents):
        info = entry.lstat()
        ledger.require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and
                       not info.st_mode & 0o022, 'device path ancestry must be root-owned')
    ledger.require(not private or stat.S_IMODE(path.stat().st_mode) == 0o700,
                   'device state must be private')
    return path


def read_record(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        info = store.check_file(fd)
        ledger.require(0 < info.st_size <= 65536, 'device record bound')
        raw = os.read(fd, 65537)
        after = store.check_file(fd)
        ledger.require(len(raw) == info.st_size and all(getattr(info, key) == getattr(after, key)
            for key in ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')),
            'device record changed')
        return store.decode(raw)
    finally:
        os.close(fd)


def write_record(path, value):
    """An interrupted replace can only leave a recoverable intent, not a grant."""
    raw = ledger.canonical(value)
    ledger.require(len(raw) <= 65536, 'device record bound')
    path = Path(path); temporary = path.with_name(path.name+'.pending')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        store.check_file(fd); os.ftruncate(fd, 0)
        store.Store.write_all(fd, raw); os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_DIRECTORY | os.O_RDONLY | os.O_CLOEXEC)
    try: os.fsync(fd)
    finally: os.close(fd)


def configuration(value):
    ledger.keys(value, ('format', 'service_id', 'domain_id', 'runtime_uid',
                        'runtime_gid', 'control_sha256'))
    ledger.require(value['format'] == FORMAT and ledger.identifier(value['service_id']) and
        ledger.identifier(value['domain_id']) and
        all(ledger.integer(value[key], minimum=1) and value[key] < 2**32-1
            for key in ('runtime_uid', 'runtime_gid')),
        'invalid device domain configuration')
    device_budget.hashed(value['control_sha256'])
    return value


def initialize(directory, service_id, uid, gid, executable, executable_sha256):
    device_store.privileged()
    directory = Path(directory).absolute()
    secure_directory(directory.parent)
    value = configuration(dict(format=FORMAT, service_id=service_id,
        domain_id=str(uuid.uuid4()), runtime_uid=uid, runtime_gid=gid,
        control_sha256=executable_sha256))
    ledger.require(not directory.exists(), 'device domain already exists; never reset its budget')
    # Copy only an operator-selected, hash-bound native helper. No setuid bit
    # or automatic installation into a search path is introduced.
    source = Path(executable).resolve(strict=True)
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        ledger.require(stat.S_ISREG(info.st_mode) and 0 < info.st_size <= 32*1024**2,
                       'device helper bound')
        raw = bytearray()
        while len(raw) < info.st_size:
            block = os.read(fd, min(1024**2, info.st_size-len(raw)))
            ledger.require(block, 'short device helper')
            raw.extend(block)
        ledger.require(not os.read(fd, 1) and raw[:4] == b'\x7fELF' and
            hashlib.sha256(raw).hexdigest() == executable_sha256,
            'device helper does not match selected artifact')
    finally: os.close(fd)
    directory.mkdir(mode=0o700)
    fd = os.open(directory/'q4t-device-control',
                 os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o500)
    try: store.Store.write_all(fd, raw); os.fsync(fd)
    finally: os.close(fd)
    write_record(directory/'configuration.json', value)
    # An incomplete initial store is recoverable only before its permanent
    # marker is exposed. Once bound, deleting state never creates a fresh one.
    binding = access_store.binding(directory, service_id)
    initial = device_budget.initial(service_id, value['domain_id'], uid, store.stamp())
    write_record(directory/'bootstrap.json', initial)
    parent = os.open(directory.parent, os.O_DIRECTORY | os.O_RDONLY | os.O_CLOEXEC)
    try: os.fsync(parent)
    finally: os.close(parent)
    access_store.bootstrap_bound(binding, initial, device_store.Store)
    return dict(configuration=value, armed=False, model_actions=False)


@contextmanager
def exclusive(directory):
    device_store.privileged()
    directory = secure_directory(directory, private=True)
    fd = os.open(directory/'admin.lock',
                 os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = store.check_file(fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        named = (directory/'admin.lock').lstat()
        ledger.require((info.st_dev, info.st_ino) == (named.st_dev, named.st_ino),
                       'device management lock changed')
        yield directory
    finally: os.close(fd)


def resume_initialize(directory):
    with exclusive(directory) as directory:
        config = configuration(read_record(directory/'configuration.json'))
        initial = device_budget.validate(read_record(directory/'bootstrap.json'))
        ledger.require(initial['revision'] == 0 and
            initial['service_id'] == config['service_id'] and
            initial['domain_id'] == config['domain_id'] and
            initial['runtime_uid'] == config['runtime_uid'], 'device bootstrap identity changed')
        binding = access_store.binding(directory, config['service_id'])
        return access_store.bootstrap_bound(binding, initial, device_store.Store)


@contextmanager
def managed(directory):
    with exclusive(directory) as directory:
        config = configuration(read_record(directory/'configuration.json'))
        executable = directory/'q4t-device-control'
        ledger.require(device_platform.artifact(executable) == config['control_sha256'],
                       'installed device helper changed')
        binding = access_store.binding(directory, config['service_id'])
        with device_store.open_bound(binding) as writer:
            policy = writer.head['state']['policy']
            ledger.require(policy['domain_id'] == config['domain_id'] and
                           policy['runtime_uid'] == config['runtime_uid'],
                           'device budget configuration changed')
        yield directory, config, binding, executable


class Observation:
    """One bounded isolated fact worker; an unreaped child retains the lease."""
    def __init__(self, lease_fd):
        self.lease_fd, self.pending = lease_fd, None

    def inspect(self, timeout=5):
        ledger.require(self.pending is None or self.pending.poll() is not None,
                       'device observation worker not reaped')
        self.pending = subprocess.Popen([sys.executable, '-I', '-B',
            str(Path(device_platform.__file__).resolve())],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env={}, pass_fds=(self.lease_fd,))
        try:
            raw, _ = self.pending.communicate(timeout=timeout)
            ledger.require(self.pending.returncode == 0 and len(raw) <= 65536,
                           'device observation failed')
            value = store.decode(raw)
            ledger.require(value.get('version') == 1 and
                           value.get('status') == 'NO_USER_HANDLES_OBSERVED',
                           'device ownership unavailable: '+str(value.get('status'))+
                           ' '+str(value.get('outstanding', value.get('reason')))[:1024])
            return value
        except BaseException:
            if self.pending.poll() is None:
                self.pending.kill()
                try: self.pending.wait(timeout=1)
                except subprocess.TimeoutExpired: pass
            raise
        finally:
            if self.pending.poll() is not None: self.pending = None


def names(config):
    name = 'q4t-device-'+config['domain_id']
    return CGROUP/name, BPFFS/name


def host_scope():
    secure_directory(CGROUP); secure_directory(BPFFS)
    def identity(path):
        value = os.stat(path)
        return value.st_dev, value.st_ino
    ledger.require(CGROUP.stat().st_ino == 1 and
        identity('/proc/self/ns/cgroup') == identity('/proc/1/ns/cgroup') and
        identity('/proc/self/ns/user') == identity('/proc/1/ns/user'),
        'complete host cgroup and user namespaces required')
    # A cgroup namespace can make a subtree appear as /. The inode above and
    # native cgroup2 filesystem verification independently reject that case.


def empty(group):
    secure_directory(group)
    ledger.require(device_inventory.read(group/'cgroup.type') == 'domain\n' and
        device_inventory.read(group/'cgroup.procs') == '' and
        device_inventory.read(group/'cgroup.threads') == '' and
        'populated 0' in device_inventory.read(group/'cgroup.events').splitlines() and
        not any(entry.is_dir() for entry in group.iterdir()),
        'device domain is populated or contains descendants')


@contextmanager
def scope(config, *, create=False):
    host_scope()
    group, pins = names(config)
    if create:
        group.mkdir(mode=0o755, exist_ok=True)
        pins.mkdir(mode=0o700, exist_ok=True)
    secure_directory(pins, private=True); empty(group)
    target = os.open(CGROUP, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    allowed = os.open(group, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try: yield group, pins/'policy', target, allowed
    finally: os.close(allowed); os.close(target)


def rules(observation):
    return [dict(major=row['major'], minor=0, all_minors=True)
            for row in observation['facts']['character_devices']]


def lease(lease_fd, lease_path, config):
    child.verify_lease(lease_fd, lease_path)
    info = os.fstat(lease_fd)
    ledger.require(info.st_uid == config['runtime_uid'], 'device lease belongs to another user')
    return info


def arm(directory, lease_fd, lease_path):
    with managed(directory) as (directory, config, binding, executable):
        lease(lease_fd, lease_path, config)
        observer = Observation(lease_fd)
        before = observer.inspect()  # Busy hosts are untouched, even briefly.
        with scope(config, create=True) as (group, pin, target, allowed):
            control = device_control.Control(executable, lease_fd, lease_path)
            args = dict(target_fd=target, allowed_fd=allowed, devices=rules(before), pin=str(pin))
            intent = dict(version=1, boot_id=before['facts']['boot_id'],
                devices=args['devices'], target_cgroup_id=os.fstat(target).st_ino,
                allowed_cgroup_id=os.fstat(allowed).st_ino)
            if pin.exists():
                ledger.require(read_record(directory/'fence.json') == intent,
                               'retained fence differs; disarm its original rules first')
            else:
                write_record(directory/'fence.json', intent)
            with control.call('recover' if pin.exists() else 'attach', args,
                              inherited=(target, allowed)) as retained:
                ledger.require(retained.result['target_cgroup_id'] == 1,
                               'device policy does not cover the host')
                after = observer.inspect(); empty(group)
                ledger.require(before == after, 'initial ownership facts changed')
                record = dict(version=1, domain_id=config['domain_id'],
                    service_id=config['service_id'], observation=after,
                    filter=retained.result, devices=args['devices'])
                write_record(directory/'qualification.json', record)
                policy = device_store.commit_qualification(binding, ledger.digest(record))
                return dict(armed=True, domain_id=config['domain_id'],
                    qualification_sha256=policy['qualification']['sha256'], model_actions=False)


def current(directory, config, binding, observer):
    record = read_record(directory/'qualification.json')
    ledger.keys(record, ('version', 'domain_id', 'service_id', 'observation', 'filter', 'devices'))
    ledger.require(record['version'] == 1 and record['domain_id'] == config['domain_id'] and
        record['service_id'] == config['service_id'] and
        record['devices'] == rules(record['observation']), 'device qualification identity changed')
    with device_store.open_bound(binding) as writer:
        qualification = writer.head['state']['policy']['qualification']
        ledger.require(qualification == dict(boot_id=store.stamp().boot_id,
            sha256=ledger.digest(record)), 'current boot has no durable device qualification')
    ledger.require(observer.inspect() == record['observation'],
                   'device ownership or installed driver changed')
    return record


def disarm(directory, lease_fd, lease_path):
    with managed(directory) as (directory, config, binding, executable):
        lease(lease_fd, lease_path, config)
        # Disable the durable grant first. An interrupted detach cannot create
        # another launch, and a subsequent arm must qualify again without reset.
        group, pins = names(config)
        if not group.exists() and not pins.exists():
            device_store.disable(binding)
            return dict(armed=False, model_actions=False)
        with scope(config) as (group, pin, target, allowed):
            device_store.disable(binding)
            if pin.exists():
                intent = read_record(directory/'fence.json')
                ledger.keys(intent, ('version', 'boot_id', 'devices',
                                    'target_cgroup_id', 'allowed_cgroup_id'))
                ledger.require(intent['version'] == 1 and
                    intent['boot_id'] == store.stamp().boot_id and
                    intent['target_cgroup_id'] == os.fstat(target).st_ino and
                    intent['allowed_cgroup_id'] == os.fstat(allowed).st_ino,
                    'device detach intent differs from current domain')
                control = device_control.Control(executable, lease_fd, lease_path)
                args = dict(target_fd=target, allowed_fd=allowed,
                            devices=intent['devices'], pin=str(pin))
                with control.call('recover', args, inherited=(target, allowed)) as retained:
                    with control.call('remove', dict(args, identity=retained.result),
                                      inherited=(target, allowed)): pass
            # No processes are moved or killed to force this cleanup through.
        group.rmdir(); pins.rmdir()
        return dict(armed=False, model_actions=False)


def enter(directory, lease_fd, lease_path, manifest_sha256, command):
    device_budget.hashed(manifest_sha256)
    ledger.require(1 <= len(command) <= 64 and os.path.isabs(command[0]) and
        all(type(arg) is str and len(arg) <= 4096 and '\0' not in arg for arg in command),
        'explicit absolute device entry command required')
    with managed(directory) as (directory, config, binding, executable):
        held = lease(lease_fd, lease_path, config)
        with scope(config) as (group, pin, target, allowed):
            record = current(directory, config, binding, Observation(lease_fd))
            control = device_control.Control(executable, lease_fd, lease_path)
            args = dict(target_fd=target, allowed_fd=allowed, pin=str(pin),
                        devices=record['devices'], identity=record['filter'])
            with control.call('adopt', args, inherited=(target, allowed)) as retained:
                charge = device_store.charge_once(binding, manifest_sha256,
                                                  held.st_dev, held.st_ino)
                policy = charge['state']; ticket = policy['last_ticket']
                claims = dict(service_id=config['service_id'], domain_id=config['domain_id'],
                    boot_id=ticket['boot_id'], manifest_sha256=manifest_sha256,
                    qualification_sha256=ticket['qualification_sha256'],
                    journal_receipt_sha256=charge['receipt_sha256'], ticket_revision=policy['revision'],
                    issued_ns=ticket['issued_ns'], expires_ns=ticket['expires_ns'],
                    runtime_uid=config['runtime_uid'], lease_device=held.st_dev,
                    lease_inode=held.st_ino, filter=retained.result)
                with control.call('issue', dict(link_fd=retained.fds[0],
                    program_fd=retained.fds[1], admission=claims),
                    inherited=retained.fds) as issued:
                    empty(group)
                    # Only this explicit administrator process enters. Children
                    # inherit the exact allowed group after credentials drop.
                    (group/'cgroup.procs').write_text(str(os.getpid()))
                    fds = (*issued.fds, *retained.fds)
                    env = dict(PATH='/usr/bin:/bin', LANG='C.UTF-8',
                        Q4T_MANAGED_LEASE_FD=str(lease_fd),
                        Q4T_DEVICE_ADMISSION_FDS=','.join(map(str, fds)))
                    for fd in (*fds, lease_fd): os.set_inheritable(fd, True)
                    libc = ctypes.CDLL(None, use_errno=True)
                    ledger.require(libc.prctl(38, 1, 0, 0, 0) == 0, 'cannot set no_new_privs')
                    os.setgroups([]); os.setresgid(config['runtime_gid'], config['runtime_gid'], config['runtime_gid'])
                    os.setresuid(config['runtime_uid'], config['runtime_uid'], config['runtime_uid'])
                    ledger.require(os.geteuid() == config['runtime_uid'], 'device entry did not drop privilege')
                    os.execve(command[0], command, env)


def status(directory):
    with managed(directory) as (_directory, config, binding, _executable):
        with device_store.open_bound(binding) as writer:
            value = device_budget.describe(writer.head['state']['policy'])
        group, pins = names(config)
        return dict(value, registered_group=str(group), pin_present=(pins/'policy').exists(),
                    runtime_verified=False)


def run(argv=None):
    import argparse
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('init', 'init-resume', 'status', 'arm', 'enter', 'disarm'))
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--service-id')
    parser.add_argument('--runtime-uid', type=int)
    parser.add_argument('--runtime-gid', type=int)
    parser.add_argument('--device-control', type=Path)
    parser.add_argument('--device-control-sha256')
    parser.add_argument('--lease-path', type=Path)
    parser.add_argument('--lease-fd', type=int)
    parser.add_argument('--manifest-sha256')
    # Preserve the command literally, including arguments named like ours.
    argsv = list(sys.argv[1:] if argv is None else argv)
    index = argsv.index('--') if '--' in argsv else len(argsv)
    args = parser.parse_args(argsv[:index]); command = argsv[index+1:]
    os.umask(0o077)
    device_store.privileged()
    if args.operation == 'init':
        ledger.require(all(value is not None for value in (args.service_id,
            args.runtime_uid, args.runtime_gid, args.device_control, args.device_control_sha256)),
            'init requires service, runtime user/group and hash-bound device helper')
        result = initialize(args.directory, args.service_id, args.runtime_uid,
            args.runtime_gid, args.device_control, args.device_control_sha256)
    elif args.operation == 'init-resume': result = resume_initialize(args.directory)
    elif args.operation == 'status': result = status(args.directory)
    else:
        ledger.require(args.lease_path is not None, 'device operation requires shared lease path')
        owned = args.lease_fd is None
        lease_fd = (os.open(args.lease_path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
                    if owned else args.lease_fd)
        try:
            if owned: fcntl.flock(lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if args.operation == 'arm': result = arm(args.directory, lease_fd, args.lease_path)
            elif args.operation == 'disarm': result = disarm(args.directory, lease_fd, args.lease_path)
            else:
                result = enter(args.directory, lease_fd, args.lease_path, args.manifest_sha256, command)
        finally:
            if owned: os.close(lease_fd)
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0


def main(argv=None):
    import json
    try:
        return run(argv)
    except (OSError, ValueError) as error:
        print(json.dumps(dict(status='FAILED', category=type(error).__name__,
            reason=str(error)[:2048], model_actions=False)), file=sys.stderr)
        return 1
