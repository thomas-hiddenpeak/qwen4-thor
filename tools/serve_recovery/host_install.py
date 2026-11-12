"""Explicit native host entry installation. No implicit startup or core reset.

The root-owned source snapshot is the only privileged persistent entry. The
service still runs as its selected ordinary UID. Enable/disable govern future
entries; stopping the actual service is a separate explicit operation.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import stat
import subprocess
import uuid

from tools.serve_recovery import bundle, device_domain, host_artifact as artifact
from tools.serve_recovery import ledger, resource_install as resources, resource_kernel, store

FORMAT = 'q4t-native-host-installation-v1'
PRIVATE = 'host.json'
PUBLIC = 'host-entry.json'


def validate(value):
    ledger.keys(value, ('format', 'installation_id', 'service_id', 'runtime_uid', 'runtime_gid',
        'state', 'resource', 'artifact', 'phase', 'enabled', 'revision'))
    ledger.require(value['format'] == FORMAT and all(ledger.identifier(value[n]) for n in
        ('installation_id', 'service_id')) and ledger.integer(value['revision']) and
        all(ledger.integer(value[n], minimum=1) and value[n] < 2**32-1 for n in
            ('runtime_uid', 'runtime_gid')) and type(value['enabled']) is bool and
        value['phase'] in ('prepared', 'ready', 'revoking', 'removed') and
        (not value['enabled'] or value['phase'] == 'ready'), 'invalid native host installation')
    chosen = value['artifact']; ledger.keys(chosen, ('package', 'manifest_sha256', 'python', 'sources'))
    ledger.keys(chosen['python'], ('path', 'sha256'))
    ledger.keys(value['resource'], ('directory', 'domain_id', 'policy'))
    ledger.require(ledger.identifier(value['resource']['domain_id']) and
        type(chosen['sources']) is dict and chosen['sources'].keys() == set(bundle.SOURCES) and
        all(type(d) is str and ledger.HASH.fullmatch(d) for d in
            (*chosen['sources'].values(), chosen['manifest_sha256'], chosen['python']['sha256'])),
        'invalid host source or resource binding')
    for path in (value['state'], chosen['package'], chosen['python']['path'], value['resource']['directory']):
        ledger.require(type(path) is str and 1 <= len(path) <= 4096 and
                       os.path.isabs(path) and '\0' not in path, 'absolute host binding required')
    selected = value['resource']['policy']['service']
    ledger.require(value['resource']['policy'] == resource_kernel.policy(
        selected['memory_high_bytes']//1024**2, selected['memory_max_bytes']//1024**2,
        selected['pids_max']), 'host resource policy differs')
    return value


@contextmanager
def exclusive(directory):
    directory = device_domain.secure_directory(directory)
    ledger.require(stat.S_IMODE(directory.stat().st_mode) == 0o755, 'host entry directory mode')
    with device_domain.exclusive(directory/'admin'): yield directory


def load(directory):
    return validate(device_domain.read_record(Path(directory)/'admin'/PRIVATE))


def publish(directory, value):
    # This receipt contains only IDs, paths and source hashes, never client keys.
    path = Path(directory)/PUBLIC; temporary = path.with_name(PUBLIC+'.pending')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(fd)
        ledger.require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1 and
                       not info.st_mode & 0o022, 'host publication must remain root-owned')
        os.ftruncate(fd, 0); store.Store.write_all(fd, ledger.canonical(validate(value)))
        os.fchmod(fd, 0o644); os.fsync(fd)
    finally: os.close(fd)
    os.replace(temporary, path); artifact.fsync_directory(directory)


def public(directory):
    device_domain.secure_directory(directory)
    return validate(store.decode(artifact.root_file(Path(directory)/PUBLIC, 65536, mode=0o644)))


def commit(directory, value):
    device_domain.write_record(Path(directory)/'admin'/PRIVATE, validate(value))
    publish(directory, value)


def resource_matches(value, current):
    ledger.require(current['phase'] == 'ready' and current['record'] is not None and
        all(current[n] == value[n] for n in ('service_id', 'runtime_uid', 'runtime_gid')) and
        current['domain_id'] == value['resource']['domain_id'] and
        current['policy'] == value['resource']['policy'], 'host resource installation differs')


def check_resource(value, *, renew=False):
    with resources.exclusive(value['resource']['directory']) as directory:
        current = resources.load(directory)
        if renew and (current['phase'] == 'renewing' or current['phase'] == 'ready' and
                      current['record']['boot_id'] != store.stamp().boot_id):
            resources.renew_locked(directory, current); current = resources.load(directory)
        resource_matches(value, current)
        ledger.require(resources.read_public(directory) == resources.public_value(current),
                       'host resource publication differs')


def environment():
    return dict(PATH='/usr/bin:/bin', LANG='C.UTF-8', PYTHONDONTWRITEBYTECODE='1')


def entry_environment(value, *, lease_fd=None, lease_path=None):
    """Carry only an actual selected lease through the protected root exec."""
    env = environment(); raw = os.environ.get('Q4T_MANAGED_LEASE_FD')
    if lease_fd is None:
        if raw is None: return env
        ledger.require(raw.isdecimal() and len(raw) <= 7 and 3 <= int(raw) <= 1000000,
                       'invalid inherited host lease descriptor')
        lease_fd = int(raw)
    chosen = value['artifact']
    manifest = store.decode(artifact.read(Path(chosen['package'])/'manifest.json',
        artifact.MANIFEST_LIMIT, chosen['manifest_sha256']))
    path = manifest['lock_path']
    ledger.require(lease_path is None or str(lease_path) == path,
                   'host entry must retain the selected package lease path')
    device_domain.lease(lease_fd, path, value)
    os.set_inheritable(lease_fd, True)
    env['Q4T_MANAGED_LEASE_FD'] = str(lease_fd)
    return env


def check_runtime(directory, value, *, probe=False):
    """Run the protected verifier after dropping UID, never import package code."""
    artifact.verify(directory, value['artifact'])
    script = Path(directory)/'source/tools/serve_recovery/host_entry.py'
    result = subprocess.run([value['artifact']['python']['path'], '-I', '-B', str(script),
        '--probe' if probe else '--check', '--directory', str(directory)], user=value['runtime_uid'],
        group=value['runtime_gid'], extra_groups=[], env=environment(),
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    ledger.require(result.returncode == 0 and len(result.stdout) <= 16384,
                   'unprivileged host runtime verification failed')
    checked = store.decode(result.stdout)
    ledger.require(checked.get('service_id') == value['service_id'] and
        checked.get('manifest_sha256') == value['artifact']['manifest_sha256'] and
        checked.get('runtime_uid') == value['runtime_uid'] and checked.get('checked') is True,
        'host runtime verification receipt differs')
    if probe:
        ledger.require(type(checked.get('resume_needed')) is bool and
            type(checked.get('entry_owned')) is bool and checked.get('model_actions') is False,
            'host entry probe receipt differs')
    return checked


def initialize(directory, package, digest, state, service_id, uid, gid, resource_directory):
    ledger.require(os.geteuid() == 0, 'native host installation requires root')
    chosen = artifact.selection(package, digest)
    state = Path(state).resolve(strict=True); info = state.stat()
    ledger.require(stat.S_ISDIR(info.st_mode) and info.st_uid == uid and
        stat.S_IMODE(info.st_mode) == 0o700, 'host state must belong to selected runtime UID')
    with resources.exclusive(resource_directory) as resource_directory:
        resource = resources.load(resource_directory)
    initial = validate(dict(format=FORMAT, installation_id=str(uuid.uuid4()),
        service_id=service_id, runtime_uid=uid, runtime_gid=gid, state=str(state),
        resource=dict(directory=str(resource_directory), domain_id=resource['domain_id'],
                      policy=resource['policy']), artifact=chosen,
        phase='prepared', enabled=False, revision=0))
    resource_matches(initial, resource)
    directory = Path(directory).absolute(); device_domain.secure_directory(directory.parent)
    directory.mkdir(mode=0o755, exist_ok=True); device_domain.secure_directory(directory)
    ledger.require(stat.S_IMODE(directory.stat().st_mode) in (0o700, 0o755), 'invalid host directory mode')
    directory.chmod(0o755); (directory/'admin').mkdir(mode=0o700, exist_ok=True)
    with exclusive(directory):
        if (directory/'admin'/PRIVATE).exists(): value = load(directory)
        else:
            ledger.require({p.name for p in directory.iterdir()} == {'admin'} and
                {p.name for p in (directory/'admin').iterdir()} <= {'admin.lock', PRIVATE+'.pending'},
                'published host authority is missing')
            pending = directory/'admin'/(PRIVATE+'.pending'); value = None
            if pending.exists():
                try: value = validate(device_domain.read_record(pending))
                except ledger.LedgerError: pass
            if value is None: value = initial
            ledger.require(value['phase'] == 'prepared' and value['revision'] == 0 and not value['enabled'],
                           'unpublished host installation was already used')
            ledger.require(all(value[n] == initial[n] for n in
                ('service_id', 'runtime_uid', 'runtime_gid', 'state', 'resource', 'artifact')),
                'unpublished host selection changed')
            commit(directory, value)
        ledger.require(all(value[n] == initial[n] for n in
            ('service_id', 'runtime_uid', 'runtime_gid', 'state', 'resource', 'artifact')),
            'host installation selection changed')
    return finish(directory)


def finish(directory):
    with exclusive(directory) as directory:
        value = load(directory)
        ledger.require(value['phase'] in ('prepared', 'ready'), 'host entry is being removed')
        if value['phase'] == 'prepared': artifact.prepare(directory, value['artifact'])
        else: artifact.verify(directory, value['artifact'])
        check_resource(value); publish(directory, value)
        checked = check_runtime(directory, value)
        if value['phase'] == 'prepared':
            value = dict(value, phase='ready', revision=value['revision']+1)
            commit(directory, value)
        return dict(installation=value, runtime=checked, automatic_start=False)


def configure(directory, *, enabled):
    ledger.require(type(enabled) is bool, 'explicit host enable flag required')
    with exclusive(directory) as directory:
        return configure_locked(directory, load(directory), enabled=enabled)


def configure_locked(directory, value, *, enabled, transaction=None):
    from tools.serve_recovery import host_handoff
    ledger.require(type(enabled) is bool and value == load(directory), 'host configuration binding differs')
    ledger.require(value['phase'] == 'ready', 'host installation is not ready')
    if enabled:
        host_handoff.require_admission(directory, value, transaction=transaction)
        publish(directory, value); check_resource(value)
        checked = check_runtime(directory, value)
        ledger.require(checked['outer_enabled'], 'explicit outer recovery enable required')
    if value['enabled'] != enabled:
        value = dict(value, enabled=enabled, revision=value['revision']+1)
        commit(directory, value)
    else: publish(directory, value)
    return dict(installation=value, core_intent_changed=False, model_actions=False)


def status(directory):
    with exclusive(directory) as directory:
        value = load(directory)
        try: current = public(directory) == value; reason = None
        except (OSError, ValueError) as error: current = False; reason = type(error).__name__
        trigger = trigger_phase(directory, value)
        registered = True if trigger == 'registered' else False if trigger in ('absent', 'removed') else None
        from tools.serve_recovery import host_handoff
        try: admission = host_handoff.admission(directory, value) or 'permitted'
        except (OSError, ValueError): admission = 'UNKNOWN'
        return dict(installation=value, publication_current=current, publication_error=reason,
                    model_actions=False, host_startup_registered=registered, host_trigger_phase=trigger,
                    handoff_admission=admission)


def trigger_phase(directory, value):
    from tools.serve_recovery import host_systemd
    if not any(os.path.lexists(Path(directory)/'admin'/name) for name in
        (host_systemd.RECORD, host_systemd.BOUND, host_systemd.RECORD+'.pending', host_systemd.BOUND+'.pending')):
        return 'absent'
    try: return host_systemd.load(directory, value)['phase']
    except (OSError, ValueError): return 'UNKNOWN'


def remove(directory):
    with exclusive(directory) as directory:
        value = load(directory)
        from tools.serve_recovery import host_handoff
        handoff = host_handoff.selected(directory, value)
        ledger.require(handoff is None or handoff[0]['phase'] == 'complete',
                       'complete the original host handoff before removing its entry')
        if value['phase'] == 'removed':
            publish(directory, value)
            return dict(removed=True, history_retained=True, resource_installation_retained=True)
        if value['phase'] != 'revoking':
            value = dict(value, phase='revoking', enabled=False, revision=value['revision']+1)
            commit(directory, value)
        else: publish(directory, value)
        ledger.require(trigger_phase(directory, value) in ('absent', 'removed'),
                       'remove the optional host trigger before removing its native entry')
        from tools.serve_recovery import cold_host
        cold_host.withdraw(directory)
        resource_directory = value['resource']['directory']
        with resources.exclusive(resource_directory) as resource_directory:
            current = resources.load(resource_directory)
            ledger.require(all(current[n] == value[n] for n in ('service_id', 'runtime_uid', 'runtime_gid'))
                and current['domain_id'] == value['resource']['domain_id'], 'host removal resource identity changed')
            if current['record'] is not None:
                record = current['record']
                if record['boot_id'] != store.stamp().boot_id:
                    ledger.require(not Path(record['root']).exists(), 'new boot resource path is occupied')
                elif current['phase'] == 'removed':
                    ledger.require(not Path(record['root']).exists(), 'removed resource path is occupied')
                else:
                    with resource_kernel.Scope(record) as scope:
                        ledger.require(scope.empty() and scope.empty('outer'),
                                       'explicit stop required before host removal')
        commit(directory, dict(value, phase='removed', revision=value['revision']+1))
        return dict(removed=True, history_retained=True, resource_installation_retained=True)


def entry(directory, *, operation='enter', daemon=False):
    ledger.require(os.geteuid() == 0 and operation in ('enter', 'start', 'ensure', 'stop'),
                   'explicit privileged host entry required')
    with exclusive(directory) as directory:
        value = load(directory)
        ledger.require(value['phase'] in ('ready', 'revoking') and
            (operation in ('stop', 'ensure') or value['phase'] == 'ready' and value['enabled']),
            'host entry is disabled or not ready')
        root = artifact.verify(directory, value['artifact'])
        publish(directory, value)
        command = [value['artifact']['python']['path'], '-I', '-B',
                   str(root/'tools/serve_recovery/host_entry.py'), '--'+operation,
                   '--directory', str(directory)]
        if daemon: command.append('--daemon')
        # After exec only the protected source can perform privileged entry.
        env = environment() if operation == 'stop' else entry_environment(value)
        os.execve(command[0], command, env)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='operation', required=True)
    for operation in ('init', 'init-resume', 'status', 'enable', 'disable', 'remove', 'enter', 'start', 'ensure', 'stop'):
        command = commands.add_parser(operation)
        command.add_argument('--directory', type=Path, required=True)
        if operation == 'init':
            for name in ('package', 'state', 'resource-directory'):
                command.add_argument('--'+name, type=Path, required=True)
            for name in ('digest', 'service-id'): command.add_argument('--'+name, required=True)
            for name in ('runtime-uid', 'runtime-gid'): command.add_argument('--'+name, type=int, required=True)
        if operation in ('enter', 'start'): command.add_argument('--daemon', action='store_true')
    args = parser.parse_args(argv)
    if args.operation == 'init':
        value = initialize(args.directory, args.package, args.digest, args.state, args.service_id,
                           args.runtime_uid, args.runtime_gid, args.resource_directory)
    elif args.operation == 'init-resume': value = finish(args.directory)
    elif args.operation == 'status': value = status(args.directory)
    elif args.operation in ('enable', 'disable'): value = configure(args.directory, enabled=args.operation == 'enable')
    elif args.operation == 'remove': value = remove(args.directory)
    else: return entry(args.directory, operation=args.operation, daemon=getattr(args, 'daemon', False))
    print(json.dumps(value, ensure_ascii=False, sort_keys=True)); return 0
