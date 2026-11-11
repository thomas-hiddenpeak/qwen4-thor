"""Optional systemd trigger for native recovery, never an owner of the model.

Stopping this timer withdraws future host triggers. Business stop remains the
native service command and durable STOPPED intent. No unit is installed on
import, packaging, ordinary startup or native host installation.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess

from tools.serve_recovery import device_domain, host_artifact as artifact
from tools.serve_recovery import host_install as host, ledger, store

FORMAT = 'q4t-systemd-trigger-v1'
RECORD = 'systemd-trigger.json'
BOUND = 'systemd-trigger-bound.json'
DIRECTORIES = dict(runtime=Path('/run/systemd/system'), persistent=Path('/etc/systemd/system'))
SYSTEMCTL = Path('/usr/bin/systemctl')


def binding(value):
    return {key: value[key] for key in ('installation_id', 'service_id', 'runtime_uid',
        'runtime_gid', 'state', 'resource', 'artifact')}


def names(value):
    stem = 'q4t-host-'+value['service_id']+'-'+value['installation_id']
    return stem+'.service', stem+'.timer'


def quote(value):
    ledger.require(type(value) is str and 0 < len(value) <= 4096 and
        all(ord(x) >= 32 and ord(x) != 127 for x in value), 'bounded systemd argument required')
    return '"'+value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$')+'"'


def render(directory, value):
    host.validate(value); directory = Path(directory).absolute()
    service, timer = names(value)
    command = ' '.join(quote(x) for x in (value['artifact']['python']['path'], '-I', '-B',
        str(directory/'source/tools/serve_recovery/host_entry.py'), '--ensure', '--systemd-trigger',
        '--directory', str(directory)))
    # All descendants enter the native resource domain before daemon fork;
    # the helper has no authority to signal a model on timeout or failed IPC.
    return {service: ('[Unit]\nDescription=Q4T optional native recovery trigger\n'
        '\n[Service]\nType=oneshot\nExecStart='+command+'\nTimeoutStartSec=75s\n'
        'NoNewPrivileges=yes\nUMask=0077\n').encode(),
        timer: ('[Unit]\nDescription=Q4T optional host recovery scheduling\n'
        '\n[Timer]\nOnBootSec=15s\nOnUnitInactiveSec=30s\nAccuracySec=1s\n'
        'RandomizedDelaySec=0\nUnit='+service+'\n'
        '\n[Install]\nWantedBy=timers.target\n').encode()}


def manager(*arguments):
    ledger.require(os.geteuid() == 0, 'explicit root systemd integration required')
    artifact.root_file(SYSTEMCTL, 16*1024**2)
    result = subprocess.run([str(SYSTEMCTL), '--system', '--no-pager', '--no-ask-password',
        *arguments], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=host.environment(), timeout=8)
    ledger.require(result.returncode == 0 and len(result.stdout) <= 32768,
                   'optional systemd operation failed')
    return result.stdout.decode('utf-8').strip()


def properties(name):
    ledger.require(re.fullmatch(r'q4t-host-[a-f0-9-]+\.(service|timer)', name), 'owned unit name required')
    raw = manager('show', name, '--property=Id,LoadState,ActiveState,SubState,FragmentPath,DropInPaths')
    result = {}
    for row in raw.splitlines():
        key, separator, value = row.partition('=')
        ledger.require(separator and key not in result, 'ambiguous systemd properties')
        result[key] = value
    ledger.keys(result, ('Id', 'LoadState', 'ActiveState', 'SubState', 'FragmentPath', 'DropInPaths'))
    ledger.require(result['Id'] == name, 'systemd unit identity differs')
    return result


def validate(value, installed):
    ledger.keys(value, ('format', 'host', 'scope', 'units', 'phase', 'enabled', 'revision'))
    ledger.require(value['format'] == FORMAT and value['host'] == binding(installed) and
        value['scope'] in DIRECTORIES and value['phase'] in ('prepared', 'registered', 'removing', 'removed') and
        type(value['enabled']) is bool and (not value['enabled'] or value['phase'] == 'registered') and
        ledger.integer(value['revision']), 'invalid systemd trigger installation')
    ledger.require(type(value['units']) is dict and value['units'].keys() == set(names(installed)) and
        all(type(x) is str and ledger.HASH.fullmatch(x) for x in value['units'].values()),
        'systemd trigger unit inventory differs')
    return value


def marker(value):
    return {name: value[name] for name in ('format', 'host', 'scope', 'units')}


def load(directory, installed, *, allow_unpublished=False):
    value = validate(device_domain.read_record(Path(directory)/'admin'/RECORD), installed)
    path = Path(directory)/'admin'/BOUND
    if path.exists():
        ledger.require(device_domain.read_record(path) == marker(value), 'systemd permanent binding changed')
    else:
        ledger.require(allow_unpublished and value['phase'] == 'prepared' and value['revision'] == 0 and
            not any(os.path.lexists(root/name) for root in DIRECTORIES.values() for name in value['units']),
            'published systemd permanent binding is missing')
    return value


def commit(directory, value, installed):
    path = Path(directory)/'admin'/BOUND
    if path.exists():
        ledger.require(device_domain.read_record(path) == marker(value), 'systemd permanent binding changed')
    else:
        ledger.require(value['phase'] == 'prepared' and value['revision'] == 0,
                       'cannot reconstruct a published systemd binding')
    device_domain.write_record(Path(directory)/'admin'/RECORD, validate(value, installed))
    if not path.exists(): device_domain.write_record(path, marker(value))


def root_directory(scope):
    root = DIRECTORIES[scope]; device_domain.secure_directory(root)
    return root


def files_match(value):
    root = root_directory(value['scope'])
    for name, digest in value['units'].items():
        artifact.root_file(root/name, 16384, digest, mode=0o644)
    return root


def loaded_match(value):
    root = files_match(value); result = {}
    for name in value['units']:
        info = properties(name)
        ledger.require(info['LoadState'] == 'loaded' and info['FragmentPath'] == str(root/name) and
            info['DropInPaths'] == '', 'systemd trigger source or override changed')
        result[name] = info
    return result


def write_unit(path, raw):
    temporary = path.with_name(path.name+'.q4t-pending')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(fd)
        ledger.require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1 and
            not info.st_mode & 0o022, 'unpublished unit must remain root-owned')
        os.ftruncate(fd, 0); store.Store.write_all(fd, raw); os.fchmod(fd, 0o644); os.fsync(fd)
    finally: os.close(fd)
    ledger.require(not os.path.lexists(path), 'refusing to replace an existing unit')
    os.replace(temporary, path); artifact.fsync_directory(path.parent)


def install(directory, scope):
    ledger.require(scope in DIRECTORIES, 'explicit systemd installation scope required')
    with host.exclusive(directory) as directory:
        installed = host.load(directory)
        ledger.require(installed['phase'] == 'ready', 'native host installation must be ready')
        artifact.verify(directory, installed['artifact']); units = render(directory, installed)
        initial = dict(format=FORMAT, host=binding(installed), scope=scope,
            units={name: hashlib.sha256(raw).hexdigest() for name, raw in units.items()},
            phase='prepared', enabled=False, revision=0)
        path = directory/'admin'/RECORD
        if path.exists(): value = load(directory, installed, allow_unpublished=True)
        else:
            ledger.require(not any(os.path.lexists(directory/'admin'/name) for name in
                (BOUND, BOUND+'.pending')), 'published systemd authority is missing')
            value = None; pending = path.with_name(RECORD+'.pending')
            if pending.exists():
                try: value = validate(device_domain.read_record(pending), installed)
                except ledger.LedgerError: pass
            ledger.require(value is None or value['phase'] == 'prepared' and value['revision'] == 0 and
                not value['enabled'], 'unpublished trigger was already used')
            # Private intent always precedes unit creation. No lost published
            # history can claim an existing file or even an in-memory unit.
            for name in units:
                ledger.require(not any(os.path.lexists(root/name) for root in DIRECTORIES.values()) and
                    properties(name)['LoadState'] == 'not-found', 'existing systemd unit has no ownership intent')
            if value is None: value = initial
            ledger.require(value == initial, 'unpublished trigger selection changed')
            commit(directory, value, installed)
        ledger.require(value['scope'] == scope and value['units'] == initial['units'] and
                       value['phase'] in ('prepared', 'registered'), 'trigger selection changed or removed')
        root = root_directory(scope)
        if not (directory/'admin'/BOUND).exists(): commit(directory, value, installed)
        changed = False
        for name, raw in units.items():
            ledger.require(not any(os.path.lexists(other/name) for other in DIRECTORIES.values()
                if other != root), 'another unit source has appeared')
            if (root/name).exists():
                artifact.root_file(root/name, 16384, value['units'][name], mode=0o644)
            else:
                info = properties(name)
                ledger.require(info['LoadState'] == 'not-found' or
                    info['FragmentPath'] == str(root/name) and info['DropInPaths'] == '',
                    'missing owned unit has been replaced by another source')
                write_unit(root/name, raw); changed = True
        if value['phase'] == 'prepared' or changed:
            manager('daemon-reload'); loaded_match(value)
        else: loaded_match(value)
        if value['phase'] == 'prepared':
            value = dict(value, phase='registered', revision=value['revision']+1)
            commit(directory, value, installed)
        return dict(registered=True, enabled=value['enabled'], model_actions=False,
                    names=list(units), scope=scope)


def configure(directory, *, enabled):
    ledger.require(type(enabled) is bool, 'explicit timer enable flag required')
    with host.exclusive(directory) as directory:
        installed = host.load(directory); value = load(directory, installed)
        return configure_locked(directory, installed, value, enabled=enabled)


def configure_locked(directory, installed, value, *, enabled, transaction=None):
    from tools.serve_recovery import host_handoff
    ledger.require(type(enabled) is bool and installed == host.load(directory) and
        value == load(directory, installed), 'trigger configuration binding differs')
    ledger.require(value['phase'] == 'registered', 'systemd trigger is not registered')
    if enabled:
        host_handoff.require_admission(directory, installed, transaction=transaction)
        loaded_match(value)
        ledger.require(installed['phase'] == 'ready' and installed['enabled'],
                       'native host entry must be enabled explicitly')
        host.check_runtime(directory, installed)
    # Persist operator intent before external effects; retries repeat the
    # same selected command without recreating model/outer budget history.
    if value['enabled'] != enabled:
        value = dict(value, enabled=enabled, revision=value['revision']+1)
        commit(directory, value, installed)
    if not enabled: loaded_match(value)
    timer = names(installed)[1]
    scope_args = ('--runtime',) if value['scope'] == 'runtime' else ()
    if enabled:
        manager(*scope_args, 'enable', timer)
        # Never wait for an ensure child while holding its admin lock.
        manager('--no-block', 'start', timer)
    else:
        manager('stop', timer); manager(*scope_args, 'disable', timer)
    return dict(enabled=enabled, model_actions=False, core_intent_changed=False)


def status(directory):
    with host.exclusive(directory) as directory:
        installed = host.load(directory); value = load(directory, installed)
        result = dict(installation=value, model_actions=False, core_health='not_observed')
        if value['phase'] == 'registered':
            try: result['units'] = loaded_match(value); result['manager_verified'] = True
            except (OSError, ValueError, subprocess.SubprocessError): result['manager_verified'] = False
        return result


def remove(directory):
    with host.exclusive(directory) as directory:
        installed = host.load(directory); value = load(directory, installed)
        if value['phase'] == 'removed': return dict(removed=True, model_actions=False, history_retained=True)
        if value['phase'] != 'removing':
            value = dict(value, phase='removing', enabled=False, revision=value['revision']+1)
            commit(directory, value, installed)
        root = root_directory(value['scope']); service, timer = names(installed)
        for name, digest in value['units'].items():
            path = root/name
            if path.exists(): artifact.root_file(path, 16384, digest, mode=0o644)
            info = properties(name)
            ledger.require(info['LoadState'] == 'not-found' or
                info['FragmentPath'] == str(path) and info['DropInPaths'] == '',
                'systemd removal source or override changed')
        # Stop only the trigger. An already admitted native service continues.
        # A missing file does not mean the manager unloaded an active timer.
        # Source identity was checked above, including any in-memory unit.
        manager('stop', timer)
        manager(*(('--runtime',) if value['scope'] == 'runtime' else ()), 'disable', timer)
        info = properties(service)
        ledger.require(info['ActiveState'] in ('inactive', 'failed'),
                       'trigger helper is active; wait for its bounded completion before removing')
        for name, digest in value['units'].items():
            path = root/name
            if not path.exists(): continue
            artifact.root_file(path, 16384, digest, mode=0o644); path.unlink(); artifact.fsync_directory(root)
        manager('daemon-reload')
        commit(directory, dict(value, phase='removed', revision=value['revision']+1), installed)
        return dict(removed=True, model_actions=False, history_retained=True)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='operation', required=True)
    for operation in ('render', 'install', 'enable', 'disable', 'status', 'remove'):
        command = commands.add_parser(operation); command.add_argument('--directory', type=Path, required=True)
        if operation == 'install': command.add_argument('--scope', choices=tuple(DIRECTORIES), required=True)
    args = parser.parse_args(argv)
    if args.operation == 'render':
        value = {name: raw.decode() for name, raw in render(args.directory, host.public(args.directory)).items()}
    elif args.operation == 'install': value = install(args.directory, args.scope)
    elif args.operation in ('enable', 'disable'): value = configure(args.directory, enabled=args.operation == 'enable')
    elif args.operation == 'status': value = status(args.directory)
    else: value = remove(args.directory)
    print(json.dumps(value, ensure_ascii=False, sort_keys=True)); return 0
