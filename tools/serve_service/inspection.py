"""Read-only, bounded observations; historical receipts are never repaired."""
import contextlib
import fcntl
import itertools
import os
from pathlib import Path
import re
import stat
import time

from tools.serve_ops.contracts import InvalidObservation, json_object
from tools.serve_ops.identity import bounded_file
from tools.serve_service import package as pkg
from tools.evalscope import nvml_process_query

PROC = Path('/proc')
MAX_PROCESSES = 8192


def require(value, code):
    if not value:
        raise InvalidObservation(code)


def inode(path):
    value = path.stat()
    return [value.st_dev, value.st_ino]


def boot_id():
    value = bounded_file(PROC/'sys/kernel/random/boot_id', 64).decode().strip()
    require(re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',
                         value), 'boot_identity_invalid')
    return value


def current_context():
    # PID and user namespaces alone do not identify the procfs/network view.
    return dict(uid=os.geteuid(), proc_root=inode(PROC), namespaces={
        name: inode(PROC/'self/ns'/name) for name in ('pid', 'net', 'user', 'mnt')})


def owner(value):
    require(isinstance(value, dict) and set(value) ==
            {'pid', 'start_ticks', 'boot_id'}, 'owner_identity_invalid')
    require(type(value['pid']) is int and 1 < value['pid'] < 2**31 and
            type(value['start_ticks']) is int and value['start_ticks'] > 0 and
            isinstance(value['boot_id'], str) and
            re.fullmatch(r'[0-9a-f-]{36}', value['boot_id']),
            'owner_identity_invalid')
    return value


def metadata(path, optional=False):
    try:
        return json_object(bounded_file(path, 2*1024**2))
    except FileNotFoundError:
        if optional:
            return None
        raise


def records(output):
    control = metadata(output/'controller.json')
    require(type(control.get('version')) is int and
            control['version'] in (1, 2), 'controller_version_unknown')
    known = [dict(role='controller', **owner(control['owner']))]
    worker = metadata(output/'status.json', True)
    if worker:
        require(worker['package_manifest_sha256'] == control['manifest_sha256'],
                'worker_package_mismatch')
        known.append(dict(role='worker', **owner(worker['owner'])))
        if worker.get('backend'):
            known.append(dict(role='backend', **owner(worker['backend'])))
    # Bound directory work as well as each JSON read. Never follow symlinks.
    with os.scandir(output) as entries:
        names = [row.name for row in itertools.islice(entries, 65)]
    require(len(names) <= 64, 'run_directory_limit')
    attempts = sorted(n for n in names if n.startswith('attempt-'))
    require(set(attempts) <= {f'attempt-{n}.json' for n in range(1, 5)},
            'attempt_inventory_invalid')
    for name in attempts:
        known.append(dict(role=name[:-5],
                          **owner(metadata(output/name)['owner'])))
    terminal = metadata(output/'terminal.json', True)
    # Ignore changing metrics/timestamps, but detect ownership/state changes.
    history = dict(worker=None if worker is None else {
        key: worker.get(key) for key in
        ('state', 'attempts', 'restarts', 'cleanup_confirmed')},
        terminal=None if terminal is None else {key: terminal.get(key) for key
            in ('state', 'cleanup_confirmed', 'automatic_reactivation')})
    return dict(control=control, known=known, history=history)


def process_row(pid):
    raw = bounded_file(PROC/str(pid)/'stat', 65536).decode()
    left, right = raw.rsplit(')', 1)
    fields = right.split()
    require(int(left.split('(', 1)[0]) == pid and len(fields) >= 20,
            'process_stat_invalid')
    return dict(pid=pid, state=fields[0], ppid=int(fields[1]),
                start_ticks=int(fields[19]))


def process_snapshot(known, control, package, boot):
    """Identify exact recorded owners and conservative same-package candidates.

    PGIDs are deliberately not used: a dead leader's PGID can be recycled.
    Pre-existing unrelated same-UID nondumpable processes cannot be descendants
    of this generation. New unreadable processes prevent an absence claim.
    """
    with os.scandir(PROC) as entries:
        pids = [int(p.name) for p in itertools.islice(
            (p for p in entries if p.name.isdecimal()), MAX_PROCESSES+1)]
    require(len(pids) <= MAX_PROCESSES, 'process_count_limit')
    executable = inode(package/'q4t')
    scripts = {str(package/'source/tools/serve_service'/name) for name in
               ('service.py', 'backend.py')}
    present, candidates, rows, uncertain = [], [], {}, []
    recorded = {r['pid'] for r in known}
    for pid in sorted(set(pids) | recorded):
        try:
            row = process_row(pid)
            rows[pid] = row
            roles = [r['role'] for r in known if r['pid'] == pid and
                     r['start_ticks'] == row['start_ticks'] and
                     r['boot_id'] == boot]
            if roles:
                present.append(dict(**row, roles=roles))
                continue
            if pid == os.getpid() or row['state'] == 'Z':
                continue
            if (PROC/str(pid)).stat().st_uid != os.geteuid():
                continue
            if boot == control['owner']['boot_id'] and (
                    row['start_ticks'] < control['owner']['start_ticks']):
                continue
            image = inode(PROC/str(pid)/'exe')
            args = bounded_file(PROC/str(pid)/'cmdline', 65536).split(b'\0')
            args = [s.decode(errors='replace') for s in args if s]
            managed = any(s in args for s in scripts) and (
                '_worker' in args or 'run' in args or
                str(package/'source/tools/serve_service/backend.py') in args)
            if image == executable or managed:
                candidates.append(dict(**row, reason='same_package'))
            require(process_row(pid)['start_ticks'] == row['start_ticks'],
                    'process_changed_during_read')
        except (FileNotFoundError, ProcessLookupError):
            # Missing exe/cmdline is not proof that the process disappeared.
            try:
                process_row(pid)
                uncertain.append(pid)
            except (FileNotFoundError, ProcessLookupError):
                rows.pop(pid, None)
            except (OSError, ValueError, IndexError):
                uncertain.append(pid)
        except (OSError, ValueError, IndexError):
            uncertain.append(pid)
    roots = {p['pid'] for p in present}
    descendants = set(roots)
    while True:
        found = {pid for pid, row in rows.items() if row['ppid'] in descendants}
        if found <= descendants:
            break
        descendants |= found
    for pid in sorted(descendants-roots):
        if pid != os.getpid():
            candidates.append(dict(**rows[pid], reason='recorded_owner_descendant'))
    require(len(present)+len(candidates)+len(uncertain) <= 256,
            'process_result_limit')
    return dict(present=present, candidates=candidates, unreadable=uncertain,
                complete=not uncertain)


def listeners(port):
    result = []
    for family, width in (('tcp', 8), ('tcp6', 32)):
        raw = bounded_file(PROC/'net'/family, 2*1024**2).decode()
        lines = raw.splitlines()
        require(lines and 'local_address' in lines[0],
                'listener_header_invalid')
        for line in lines[1:]:
            fields = line.split()
            require(len(fields) >= 10, 'listener_table_invalid')
            endpoint = fields[1].split(':')
            require(len(endpoint) == 2 and len(endpoint[0]) == width and
                    len(endpoint[1]) == 4, 'listener_endpoint_invalid')
            int(endpoint[0], 16)
            if int(endpoint[1], 16) == port and fields[3] == '0A':
                result.append(dict(family=family, address_hex=endpoint[0],
                                   inode=int(fields[9])))
    require(len(result) <= 256, 'listener_count_limit')
    return sorted(result, key=lambda row: (row['family'], row['inode']))


@contextlib.contextmanager
def lock_snapshot(path):
    """Probe the existing inode without creating it or rewriting its text."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        st = os.fstat(fd)
        require(stat.S_ISREG(st.st_mode), 'lock_not_regular')
        value = dict(identity=[st.st_dev, st.st_ino])
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            value['state'] = 'AVAILABLE'
        except BlockingIOError:
            value['state'] = 'BUSY'
        # Lock text is unauthoritative and can be mid-write when BUSY.
        try:
            text = metadata(path)
            value['historical_record'] = {k: text.get(k) for k in
                                           ('pid', 'state', 'output')}
        except (OSError, ValueError):
            value['historical_record'] = None
        yield value
    finally:
        os.close(fd)  # Only our open description, never another owner's lock.


def assess(result):
    errors = result['errors']
    complete = not errors and result.get('scope_verified') is True
    observed = result.get('processes', {})
    present = observed.get('present', [])
    candidates = observed.get('candidates', [])
    owner_live = any('controller' in row['roles'] for row in present)
    busy = (result.get('lock', {}).get('state') == 'BUSY' or
            result.get('recovery_marker_present') is True or
            bool(result.get('gpu', {}).get('pids')) or
            bool(result.get('listeners')) or bool(present) or bool(candidates))
    result['complete'] = complete
    result['resource_availability'] = ('BUSY' if busy else
                                       'AVAILABLE' if complete else 'UNKNOWN')
    result['cleanup_observed'] = complete and not busy
    result['run_state'] = ('OWNER_PRESENT' if owner_live else
        'RESIDUAL_RECORDED_PROCESSES' if present else
        'INACTIVE_OBSERVED' if result['cleanup_observed'] else
        'OWNER_ABSENT_RESOURCES_BUSY' if complete and busy else 'UNKNOWN')
    return result


def stable_resources(value):
    """Scheduler R/S transitions are not changes of ownership or identity."""
    value = {**value}
    if 'processes' in value:
        processes = {**value['processes']}
        for key in ('present', 'candidates'):
            processes[key] = [{**{k: v for k, v in row.items() if k != 'state'},
                               'zombie': row['state'] == 'Z'}
                              for row in processes[key]]
        value['processes'] = processes
    return value


def inspect(output):
    started = time.monotonic()
    result = dict(version='q4t.service.inspection.v1', read_only=True,
        snapshot_only=True, cleanup_observed=False, errors=[])
    errors = result['errors']
    try:
        output = Path(output).absolute()
        require(output == output.resolve(strict=True), 'run_path_not_canonical')
        before = records(output)
        control = before['control']
        package = Path(control['package'])
        manifest = pkg.verify(package, control['manifest_sha256'])
        repository = Path(manifest['repository'])
        require(any(output.is_relative_to(repository/name) for name in
                    ('build', '.q4t-work')), 'run_outside_workspace')
        # Keep worktree/common-dir resolution identical to the production guard.
        from tools.evalscope import gpu_exclusive as guard
        guard.ROOT = repository
        lock = guard.common_lock()
        require(Path(control['lock_path']) == lock, 'repository_lock_mismatch')
        context, boot = current_context(), boot_id()
        result['scope_verified'] = (control.get('observation_context') == context
                                   and control['owner']['boot_id'] == boot)
        if not result['scope_verified']:
            errors.append('legacy_or_different_observation_scope')
        result['history'] = before['history']
        result['package_manifest_sha256'] = control['manifest_sha256']
        with contextlib.ExitStack() as stack:
            try:
                result['lock'] = stack.enter_context(lock_snapshot(lock))
            except (OSError, ValueError):
                errors.append('lock_observation_unavailable')
            snapshots = []
            marker = lock.with_name(lock.name+'.recovery-required')
            result['recovery_marker_present'] = os.path.lexists(marker)
            for sample in range(2):
                observed = {}
                try:
                    observed['processes'] = process_snapshot(
                        before['known'], control, package, boot)
                    if not observed['processes']['complete']:
                        errors.append('process_observation_incomplete')
                except (OSError, ValueError, IndexError):
                    errors.append('process_observation_unavailable')
                try:
                    observed['listeners'] = listeners(manifest['settings']['port'])
                except (OSError, ValueError, IndexError):
                    errors.append('listener_observation_unavailable')
                snapshots.append(observed)
                if sample == 0:
                    gpu = nvml_process_query.query(timeout=5)
                    result['gpu'] = {key: gpu.get(key) for key in
                                     ('status', 'pids', 'device_count', 'error')}
                    if (gpu.get('status') != 'OK' or
                            type(gpu.get('device_count')) is not int or
                            gpu['device_count'] < 1 or
                            not isinstance(gpu.get('pids'), list) or
                            any(type(p) is not int or p <= 0
                                for p in gpu.get('pids', []))):
                        errors.append('gpu_observation_unavailable')
            result.update(snapshots[-1])
            if stable_resources(snapshots[0]) != stable_resources(snapshots[1]):
                errors.append('resources_changed_during_observation')
            if os.path.lexists(marker) != result['recovery_marker_present']:
                errors.append('recovery_marker_changed')
            if records(output) != before:
                errors.append('run_records_changed_during_observation')
            if current_context() != context or boot_id() != boot:
                errors.append('observation_scope_changed')
            if 'lock' in result and inode(lock) != result['lock']['identity']:
                errors.append('lock_inode_changed')
    except InvalidObservation as error:
        errors.append(error.code)
    except Exception:
        # No arbitrary exception text, request data, command line or environment.
        errors.append('inspection_input_or_observation_unavailable')
    result['errors'] = sorted(set(errors))
    result['elapsed_seconds'] = time.monotonic()-started
    return assess(result)
