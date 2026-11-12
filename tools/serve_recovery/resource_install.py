"""Explicit root-owned installation and non-destructive resource removal.

No model process is started, signalled or migrated here. A public root-owned
receipt exposes only the installed capability, while private durable phases
prevent recreating lost published history or treating partial removal as done.
"""
from contextlib import contextmanager
from copy import deepcopy
import os
from pathlib import Path
import stat
import uuid

from tools.serve_recovery import device_domain, ledger, resource_kernel as kernel, store

FORMAT = 'q4t-resource-installation-v2'
PRIVATE = 'installation.json'
PUBLIC = 'resource.json'


def validate(value):
    ledger.keys(value, ('format', 'service_id', 'domain_id', 'runtime_uid', 'runtime_gid',
                        'policy', 'phase', 'record', 'revision',
                        *(('renewal',) if 'renewal' in value else ()),
                        *(('previous_record',) if 'previous_record' in value else ())))
    ledger.require(value['format'] == FORMAT and ledger.identifier(value['service_id']) and
        ledger.identifier(value['domain_id']) and ledger.integer(value['revision']) and
        all(ledger.integer(value[n], minimum=1) and value[n] < 2**32-1 for n in
            ('runtime_uid', 'runtime_gid')) and
        value['phase'] in ('prepared', 'ready', 'renewing', 'revoking', 'empty', 'removed'),
        'invalid resource installation')
    policy = value['policy']; selected = policy['service']
    ledger.require(policy == kernel.policy(selected['memory_high_bytes']//1024**2,
        selected['memory_max_bytes']//1024**2, selected['pids_max']), 'resource installation policy differs')
    record = value['record']
    ledger.require((record is None) == (value['phase'] == 'prepared'), 'resource installation lost kernel identity')
    if record is not None:
        kernel.validate(record)
        ledger.require(all(record[name] == value[name] for name in
            ('service_id', 'domain_id', 'runtime_uid', 'policy')), 'installed kernel identity differs')
    ledger.require(('renewal' in value) == (value['phase'] == 'renewing'),
                   'resource renewal checkpoint differs')
    if 'renewal' in value:
        renewal = value['renewal']; ledger.keys(renewal, ('target_boot', 'source_sha256'))
        ledger.require(ledger.identifier(renewal['target_boot']) and
            renewal['target_boot'] != record['boot_id'] and
            renewal['source_sha256'] == ledger.digest(record), 'invalid resource renewal source')
    if 'previous_record' in value:
        previous = kernel.validate(value['previous_record'])
        ledger.require(record is not None and previous['boot_id'] != record['boot_id'] and
            all(previous[key] == record[key] for key in
                ('service_id', 'domain_id', 'runtime_uid', 'root', 'policy')),
            'resource renewal lost its prior installation identity')
    return value


@contextmanager
def exclusive(directory):
    ledger.require(os.geteuid() == 0, 'resource installation requires root')
    directory = device_domain.secure_directory(directory)
    ledger.require(stat.S_IMODE(directory.stat().st_mode) == 0o755, 'resource publication directory mode')
    with device_domain.exclusive(directory/'admin'):
        yield directory


def public_value(value):
    return dict(format=FORMAT, service_id=value['service_id'], domain_id=value['domain_id'],
        runtime_uid=value['runtime_uid'], runtime_gid=value['runtime_gid'], revision=value['revision'],
        phase=value['phase'], record=value['record'], admission_enabled=value['phase'] == 'ready',
        model_actions=False, device_authorized=False)


def publish(directory, value):
    """Private state must be durable first. An old public file never enables entry."""
    raw = ledger.canonical(public_value(value)); temporary = directory/(PUBLIC+'.pending')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        before = os.fstat(fd)
        ledger.require(stat.S_ISREG(before.st_mode) and before.st_uid == 0 and before.st_nlink == 1 and
            not before.st_mode & 0o022, 'resource publication must be root-owned')
        os.ftruncate(fd, 0); store.Store.write_all(fd, raw); os.fchmod(fd, 0o644); os.fsync(fd)
    finally: os.close(fd)
    os.replace(temporary, directory/PUBLIC)
    fd = os.open(directory, os.O_DIRECTORY | os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try: os.fsync(fd)
    finally: os.close(fd)


def commit(directory, value):
    validate(value)
    device_domain.write_record(directory/'admin'/PRIVATE, value)
    publish(directory, value)


def initialize(directory, service_id, uid, gid, selected):
    ledger.require(os.geteuid() == 0, 'resource installation requires root')
    directory = Path(directory).absolute(); device_domain.secure_directory(directory.parent)
    initial = validate(dict(format=FORMAT, service_id=service_id, domain_id=str(uuid.uuid4()),
        runtime_uid=uid, runtime_gid=gid, policy=selected, phase='prepared', record=None, revision=0))
    directory.mkdir(mode=0o755, exist_ok=True)
    device_domain.secure_directory(directory)
    ledger.require(stat.S_IMODE(directory.stat().st_mode) in (0o700, 0o755), 'invalid resource installation directory')
    directory.chmod(0o755)
    (directory/'admin').mkdir(mode=0o700, exist_ok=True)
    with exclusive(directory):
        if (directory/'admin'/PRIVATE).exists():
            value = load(directory)
        else:
            # No kernel creation or ready publication can precede the first
            # durable private intent. Only this unpublished prefix is resumable.
            ledger.require({p.name for p in directory.iterdir()} == {'admin'} and
                {p.name for p in (directory/'admin').iterdir()} <=
                    {'admin.lock', PRIVATE+'.pending'}, 'published resource authority is missing')
            value = None; pending = directory/'admin'/(PRIVATE+'.pending')
            if pending.exists():
                try: value = validate(device_domain.read_record(pending))
                except ledger.LedgerError: pass
            if value is None: value = initial
            ledger.require(value['phase'] == 'prepared' and value['revision'] == 0,
                           'unpublished installation was already used')
            ledger.require(all(value[key] == initial[key] for key in
                ('service_id', 'runtime_uid', 'runtime_gid', 'policy')), 'resource bootstrap selection changed')
            commit(directory, value)
        ledger.require(all(value[key] == initial[key] for key in
            ('service_id', 'runtime_uid', 'runtime_gid', 'policy')), 'resource installation selection changed')
    return finish(directory)


def load(directory): return validate(device_domain.read_record(directory/'admin'/PRIVATE))


def finish(directory):
    with exclusive(directory) as directory:
        value = load(directory)
        ledger.require(value['phase'] in ('prepared', 'ready', 'renewing'),
                       'resource installation is being removed')
        if value['phase'] == 'renewing': return renew_locked(directory, value)
        if value['phase'] == 'prepared':
            record = kernel.create(value['service_id'], value['domain_id'], value['runtime_uid'],
                                   value['policy'], resume_unpublished=True)
            value = dict(value, phase='ready', record=record, revision=value['revision']+1)
            commit(directory, value)
        else:
            with kernel.Scope(value['record']): pass
            # Repair only the public copy; never reconstruct private authority.
            publish(directory, value)
        return public_value(value)


def renew_locked(directory, value):
    """Keep the installation identity; only a new actual host boot recreates it."""
    current_boot = store.stamp().boot_id
    ledger.require(value['phase'] in ('ready', 'renewing'), 'removed resource installation cannot renew')
    root = Path(value['record']['root'])
    if value['phase'] == 'ready' and value['record']['boot_id'] == current_boot:
        with kernel.Scope(value['record']): pass
        publish(directory, value)
        return public_value(value)
    if value['phase'] == 'ready' or value['renewal']['target_boot'] != current_boot:
        # Neither same-boot deletion nor a pre-existing new domain supplies
        # the original disappeared kernel epoch. Never adopt or erase it.
        ledger.require(value['record']['boot_id'] != current_boot and not root.exists(),
                       'resource renewal requires a new boot and absent old domain')
        value = dict(value, phase='renewing', revision=value['revision']+1,
                     renewal=dict(target_boot=current_boot, source_sha256=ledger.digest(value['record'])))
        commit(directory, value)
    # Publish revocation before creating/resuming an unpublished new epoch.
    publish(directory, value)
    record = kernel.create(value['service_id'], value['domain_id'], value['runtime_uid'],
                           value['policy'], resume_unpublished=True)
    ledger.require(record['boot_id'] == value['renewal']['target_boot'], 'host boot changed during renewal')
    value = dict(value, phase='ready', revision=value['revision']+1,
                 previous_record=deepcopy(value['record']), record=record)
    del value['renewal']
    commit(directory, value)
    return public_value(value)


def renew(directory):
    with exclusive(directory) as directory: return renew_locked(directory, load(directory))


def status(directory):
    with exclusive(directory) as directory:
        value = load(directory)
        result = public_value(value)
        if value['phase'] == 'ready' and value['record']['boot_id'] != store.stamp().boot_id:
            result.update(kernel_verified=False, admission_enabled=False, renewal_required=True)
        elif value['phase'] == 'ready':
            with kernel.Scope(value['record']) as scope:
                result.update(kernel_verified=True, service_empty=scope.empty(), outer_empty=scope.empty('outer'))
        else: result['kernel_verified'] = False
        return result


def remove(directory):
    """Revoke new entry first; preserve healthy tasks until explicitly stopped."""
    with exclusive(directory) as directory:
        value = load(directory)
        from tools.serve_recovery import device_resource
        device_resource.removal_allowed(directory, value)
        ledger.require(value['phase'] not in ('prepared', 'renewing'),
                       'finish unpublished installation before removal')
        if value['phase'] == 'ready':
            value = dict(value, phase='revoking', revision=value['revision']+1); commit(directory, value)
        if value['phase'] == 'revoking':
            # Re-publish an interrupted revocation BEFORE inspecting/removing.
            publish(directory, value)
            if value['record']['boot_id'] != store.stamp().boot_id:
                ledger.require(not Path(value['record']['root']).exists(),
                               'old resource domain path was recreated after boot')
            else:
                with kernel.Scope(value['record']) as scope:
                    ledger.require(scope.empty('outer') and scope.empty(),
                                   'resource removal waits for explicit stop and empty owned domains')
            value = dict(value, phase='empty', revision=value['revision']+1); commit(directory, value)
        if value['phase'] == 'empty':
            record = value['record']; root = Path(record['root'])
            # The durable empty checkpoint precedes the first rmdir. Existing
            # entry holders live inside these leaves and new entry is revoked.
            if root.exists():
                ledger.require(kernel.identity(root) == record['root_identity'], 'removal root replaced')
                for role in kernel.NAMES:
                    path = root/role
                    if not path.exists(): continue
                    ledger.require(kernel.identity(path) == record['children'][role], 'removal leaf replaced')
                    fd = kernel.directory(path)
                    try:
                        ledger.require(kernel.counters(kernel.read(fd, 'cgroup.events')).get('populated') == 0,
                                       'resource removal leaf repopulated')
                    finally: os.close(fd)
                    path.rmdir()
                root.rmdir()
            value = dict(value, phase='removed', revision=value['revision']+1); commit(directory, value)
        publish(directory, value)
        return dict(public_value(value), removed=True, history_retained=True)


def read_public(directory):
    directory = device_domain.secure_directory(directory)
    path = directory/PUBLIC
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        ledger.require(stat.S_ISREG(before.st_mode) and before.st_uid == 0 and before.st_nlink == 1 and
            stat.S_IMODE(before.st_mode) == 0o644 and 0 < before.st_size <= 65536,
            'root resource publication required')
        raw = os.read(fd, 65537); after = os.fstat(fd)
        ledger.require(len(raw) == before.st_size and all(getattr(before, k) == getattr(after, k)
            for k in ('st_dev', 'st_ino', 'st_mtime_ns', 'st_ctime_ns')), 'resource publication changed')
        value = store.decode(raw)
        ledger.keys(value, ('format', 'service_id', 'domain_id', 'runtime_uid', 'runtime_gid', 'revision',
                           'phase', 'record', 'admission_enabled', 'model_actions', 'device_authorized'))
        ledger.require(value['record'] is not None and value['phase'] == 'ready' and
            value['admission_enabled'] is True and value['model_actions'] is False and
            value['device_authorized'] is False, 'resource entry is not enabled')
        original = {k:value[k] for k in ('format', 'service_id', 'domain_id', 'runtime_uid', 'runtime_gid',
                                       'revision', 'phase', 'record')}
        validate(dict(original, policy=value['record']['policy']))
        return deepcopy(value)
    finally: os.close(fd)


def main(argv=None):
    import argparse
    import json
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ['enter']:
        from tools.serve_recovery import resource_launch
        return resource_launch.main(argv[1:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('init', 'init-resume', 'renew', 'status', 'remove'))
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--service-id')
    parser.add_argument('--runtime-uid', type=int)
    parser.add_argument('--runtime-gid', type=int)
    parser.add_argument('--memory-high-mib', type=int)
    parser.add_argument('--memory-max-mib', type=int)
    parser.add_argument('--tasks-max', type=int)
    args = parser.parse_args(argv); os.umask(0o077)
    try:
        if args.operation == 'init':
            selected = kernel.policy(args.memory_high_mib, args.memory_max_mib, args.tasks_max)
            result = initialize(args.directory, args.service_id, args.runtime_uid, args.runtime_gid, selected)
        else:
            result = {'init-resume': finish, 'renew': renew, 'status': status, 'remove': remove}[args.operation](args.directory)
        print(json.dumps(result, sort_keys=True)); return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps(dict(status='FAILED', category=type(error).__name__, model_actions=False)), file=sys.stderr)
        return 1
