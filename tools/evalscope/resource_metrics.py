"""Read process/cgroup I/O and memory counters without inventing physical totals.

All reads are observational. /proc/PID/io rchar/wchar are logical I/O, while
read_bytes/write_bytes and cgroup io.stat describe storage accounting. Selected
model-device counters include OTHER workloads; parent disks are never added.
Missing observations and discontinuous counters have null values with reasons.
"""
import os
from pathlib import Path
import time


PROC_ROOT = Path('/proc')
CGROUP_ROOT = Path('/sys/fs/cgroup')
SYS_DEV_ROOT = Path('/sys/dev/block')
PROCESS_COUNTERS = ('rchar', 'wchar', 'syscr', 'syscw', 'read_bytes',
                    'write_bytes', 'cancelled_write_bytes')
DISK_COUNTERS = ('reads_completed', 'reads_merged', 'sectors_read',
                 'read_milliseconds', 'writes_completed', 'writes_merged',
                 'sectors_written', 'write_milliseconds', 'ios_in_progress',
                 'io_milliseconds', 'weighted_io_milliseconds',
                 'discards_completed', 'discards_merged', 'sectors_discarded',
                 'discard_milliseconds', 'flushes_completed',
                 'flush_milliseconds')


def failure(reason, exc=None):
    result = {'reason': reason}
    if exc is not None:
        result.update(message=str(exc), errno=getattr(exc, 'errno', None))
    return result


def read_value(path, parser):
    try:
        return {'value': parser(Path(path).read_text()), 'error': None}
    except (OSError, UnicodeError, ValueError, IndexError) as exc:
        return {'value': None, 'error': failure('read_or_parse_failed', exc)}


def natural(text):
    value = int(text.strip())
    if value < 0:
        raise ValueError('negative unsigned counter')
    return value


def limit(text):
    return 'max' if text.strip() == 'max' else natural(text)


def flat_counters(text):
    result = {}
    for line in text.splitlines():
        key, value = line.replace(':', '').split()
        result[key] = natural(value)
    if not result:
        raise ValueError('empty counter record')
    return result


def io_counters(text):
    result = {}
    for line in text.splitlines():
        words = line.split()
        major, minor = words[0].split(':')
        natural(major)
        natural(minor)
        result[words[0]] = {key: natural(value) for key, value in
                            (word.split('=', 1) for word in words[1:])}
    return result


def pressure(text):
    result = {}
    for line in text.splitlines():
        words = line.split()
        values = dict(word.split('=', 1) for word in words[1:])
        if not all(key in values for key in ('avg10', 'avg60', 'avg300', 'total')):
            raise ValueError('incomplete PSI record')
        result[words[0]] = {key: natural(value) if key == 'total' else float(value)
                            for key, value in values.items()}
    if not result:
        raise ValueError('empty PSI record')
    return result


def normalize_cgroup(path, cgroup_root=CGROUP_ROOT):
    root = Path(cgroup_root)
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('cgroup path must be absolute without parent traversal')
    # Accept either the actual mount path or /system.slice/unit.service.
    return path if path.is_relative_to(root) else root / str(path).lstrip('/')


def directory_identity(path):
    try:
        stat = Path(path).stat()
        return {'value': {'device': stat.st_dev, 'inode': stat.st_ino},
                'error': None}
    except OSError as exc:
        return {'value': None, 'error': failure('identity_unavailable', exc)}


def read_cgroup(path, cgroup_root=CGROUP_ROOT):
    """Read a retained cgroup even after its process has exited; never reset it."""
    path = normalize_cgroup(path, cgroup_root)
    before = directory_identity(path)
    parsers = {
        **{key: natural for key in ('memory.current', 'memory.peak',
           'memory.swap.current', 'memory.swap.peak', 'pids.current')},
        **{key: limit for key in ('memory.max', 'memory.high', 'memory.swap.max')},
        **{key: flat_counters for key in ('memory.stat', 'memory.events',
           'memory.events.local', 'memory.swap.events', 'cgroup.events')},
        'io.stat': io_counters, 'memory.pressure': pressure,
        'io.pressure': pressure,
        'cgroup.procs': lambda text: [natural(line) for line in text.splitlines()],
    }
    observations = {name: read_value(path / name, parser)
                    for name, parser in parsers.items()}
    after = directory_identity(path)
    consistent = before['value'] is not None and before == after
    return {'path': str(path), 'identity': before['value'],
            'identity_error': (before['error'] or after['error'] or
                               (failure('identity_changed') if not consistent else None)),
            'consistent_identity': consistent,
            'observations': observations}


def process_identity(pid, proc_root):
    def parse(text):
        parts = text.rsplit(')', 1)[1].split()
        return {'pid': pid, 'start_ticks': natural(parts[19])}
    return read_value(proc_root / str(pid) / 'stat', parse)


def unified_membership(text):
    entries = [line.split(':', 2)[2] for line in text.splitlines()
               if line.startswith('0::')]
    if len(entries) != 1 or not entries[0].startswith('/'):
        raise ValueError('missing or ambiguous unified cgroup membership')
    return entries[0]


def read_process(pid, proc_root=PROC_ROOT):
    proc_root = Path(proc_root)
    before = process_identity(pid, proc_root)
    io = read_value(proc_root / str(pid) / 'io', flat_counters)
    if io['value'] is not None and not all(k in io['value'] for k in PROCESS_COUNTERS):
        io = {'value': None, 'error': failure('incomplete_process_io')}
    membership = read_value(proc_root / str(pid) / 'cgroup', unified_membership)
    after = process_identity(pid, proc_root)
    consistent = before['value'] is not None and before == after
    if not consistent:
        io = {'value': None, 'error': failure('process_exited_or_identity_changed')}
    return {'pid': pid, 'identity': before['value'],
            'identity_error': (before['error'] or after['error'] or
                               (failure('identity_changed') if not consistent else None)),
            'consistent_identity': consistent, 'io': io,
            'cgroup_membership': membership}


def model_devices(paths, sys_dev_root=SYS_DEV_ROOT):
    """Select filesystem devices only, without adding their whole-disk parents."""
    devices = {}
    errors = []
    for path in paths:
        try:
            dev = Path(path).stat().st_dev
        except OSError as exc:
            errors.append({'path': str(path), 'error': failure('stat_failed', exc)})
            continue
        key = f'{os.major(dev)}:{os.minor(dev)}'
        if key in devices:
            continue
        sys_path = Path(sys_dev_root) / key
        parent = None
        if (sys_path / 'partition').exists():
            parent = read_value(sys_path.resolve().parent / 'dev', str.strip)
        devices[key] = {'device': key, 'filesystem_dev': dev,
                        'example_model_path': str(path),
                        'sysfs_path': str(sys_path.resolve()),
                        'partition_parent_device': parent}
    return {'devices': devices, 'errors': errors,
            'scope': 'model filesystem devices, global background; no device sum'}


def disk_counters(text):
    words = text.split()
    if len(words) < 11:
        raise ValueError('incomplete block-device stat')
    result = {name: natural(value) for name, value in zip(DISK_COUNTERS, words)}
    # Kernel block-stat sectors use 512 bytes, independent of sector size.
    result['read_bytes'] = result['sectors_read'] * 512
    result['write_bytes'] = result['sectors_written'] * 512
    if 'sectors_discarded' in result:
        result['discard_bytes'] = result['sectors_discarded'] * 512
    return result


def collect_resources(pid, expected_cgroup=None, model_paths=(), *, pids=None,
                      device_spec=None, proc_root=PROC_ROOT,
                      cgroup_root=CGROUP_ROOT, sys_dev_root=SYS_DEV_ROOT):
    """One observation for monitor rows or controller request/exit boundaries.

    expected_cgroup selects a retained unit before launch/after exit. With a
    live PID its actual membership is recorded and compared, never assumed.
    """
    start = time.time()
    proc_root = Path(proc_root)
    selected = sorted(set(pids if pids is not None else ([pid] if pid else [])))
    processes = [read_process(item, proc_root) for item in selected]
    root = next((item for item in processes if item['pid'] == pid), None)
    actual = root['cgroup_membership']['value'] if root else None
    actual_path = normalize_cgroup(actual, cgroup_root) if actual else None
    expected_path = (normalize_cgroup(expected_cgroup, cgroup_root)
                     if expected_cgroup is not None else None)
    path = expected_path or actual_path
    matches = (actual_path == expected_path if actual_path is not None and
               expected_path is not None else None)
    binding = {'root_pid': pid, 'actual_cgroup_path': str(actual_path) if actual_path else None,
               'expected_cgroup_path': str(expected_path) if expected_path else None,
               'expected_matches_actual': matches,
               'status': ('mismatch' if matches is False else 'bound_to_live_pid'
                          if root and root['consistent_identity'] and actual_path else
                          'configured_path_without_live_pid' if expected_path else
                          'no_cgroup_identity')}
    specs = device_spec if device_spec is not None else model_devices(model_paths, sys_dev_root)
    devices = {}
    for key, spec in specs['devices'].items():
        sys_path = Path(sys_dev_root) / key
        devices[key] = {**spec, 'identity': directory_identity(sys_path),
                        'stat': read_value(sys_path / 'stat', disk_counters)}
    cgroup = read_cgroup(path, cgroup_root) if path else None
    return {'schema_version': 1, 'start_t': start, 'end_t': time.time(),
            'binding': binding, 'processes': processes,
            'cgroup': cgroup,
            'model_devices': {'devices': devices, 'errors': specs['errors'],
                              'scope': specs['scope']}}


def counter_delta(current, previous, same_identity=True):
    """Consecutive samples only. A gap, new identity or reset is not zero I/O."""
    if current is None:
        return {'value': None, 'error': failure('missing_current')}
    if previous is None:
        return {'value': None, 'error': failure('missing_previous')}
    if not same_identity:
        return {'value': None, 'error': failure('identity_changed')}
    values = {}
    errors = {}
    for key in current.keys() | previous.keys():
        value = current.get(key)
        old = previous.get(key)
        if value is None:
            values[key] = None
            errors[key] = 'missing_current_counter'
        elif old is None:
            values[key] = None
            errors[key] = 'missing_previous_counter'
        elif value < old:
            values[key] = None
            errors[key] = 'counter_decreased_or_reset'
        else:
            values[key] = value - old
    return {'value': values, 'error': errors or None}


class ResourceSampler:
    """Small state holder for consecutive observations; no stale gap filling."""
    def __init__(self, expected_cgroup=None, model_paths=(), **roots):
        self.expected = expected_cgroup
        self.roots = roots
        self.devices = model_devices(model_paths, roots.get('sys_dev_root', SYS_DEV_ROOT))
        self.previous = None
        self.last_bound_path = None

    def sample(self, pid, pids=None):
        expected = self.expected or (self.last_bound_path if pid is None else None)
        current = collect_resources(pid, expected,
                                    pids=pids, device_spec=self.devices, **self.roots)
        previous = self.previous or {}
        if current['binding']['status'] == 'bound_to_live_pid':
            self.last_bound_path = current['binding']['actual_cgroup_path']
        old_procs = {p['pid']: p for p in previous.get('processes', [])}
        for proc in current['processes']:
            old = old_procs.get(proc['pid'], {})
            proc['io_delta'] = counter_delta(
                proc['io']['value'], old.get('io', {}).get('value'),
                proc['consistent_identity'] and proc['identity'] == old.get('identity'))
        cg = current['cgroup']
        old_cg = previous.get('cgroup') or {}
        if cg:
            same = (cg['consistent_identity'] and cg['identity'] == old_cg.get('identity')
                    and cg['path'] == old_cg.get('path'))
            obs = cg['observations']
            old_obs = old_cg.get('observations', {})
            cg['counter_deltas'] = {}
            for key in ('memory.events', 'memory.events.local', 'memory.swap.events'):
                cg['counter_deltas'][key] = counter_delta(
                    obs[key]['value'], old_obs.get(key, {}).get('value'), same)
            for key in ('memory.pressure', 'io.pressure'):
                now_psi = obs[key]['value']
                old_psi = old_obs.get(key, {}).get('value')
                cg['counter_deltas'][key] = counter_delta(
                    {k: v['total'] for k, v in now_psi.items()} if now_psi else None,
                    {k: v['total'] for k, v in old_psi.items()} if old_psi else None,
                    same)
            io = obs['io.stat']['value']
            old_io = old_obs.get('io.stat', {}).get('value')
            cg['counter_deltas']['io.stat'] = {
                'value': {dev: counter_delta(io.get(dev), (old_io or {}).get(dev), same)
                          for dev in io.keys() | (old_io or {}).keys()}
                         if io is not None else None,
                'error': failure('missing_current') if io is None else None}
        old_devices = previous.get('model_devices', {}).get('devices', {})
        for dev, item in current['model_devices']['devices'].items():
            old = old_devices.get(dev, {})
            now_values = item['stat']['value']
            old_values = old.get('stat', {}).get('value')
            if now_values is not None:
                now_values = {k: v for k, v in now_values.items() if k != 'ios_in_progress'}
            if old_values is not None:
                old_values = {k: v for k, v in old_values.items() if k != 'ios_in_progress'}
            item['counter_delta'] = counter_delta(
                now_values, old_values,
                item['identity']['value'] is not None and
                item['identity']['value'] == old.get('identity', {}).get('value'))
        self.previous = current
        return current


def resource_summary(samples):
    """Retain endpoint references and source peaks, never a physical RAM sum."""
    def endpoint(sample):
        if sample is None:
            return None
        cg = sample.get('cgroup') or {}
        obs = cg.get('observations', {})
        return {'sequence': sample.get('sequence'), 'phase': sample.get('phase'),
                'start_t': sample['start_t'], 'end_t': sample['end_t'],
                'binding': sample['binding'], 'cgroup_path': cg.get('path'),
                'cgroup_identity': cg.get('identity'),
                'cgroup_consistent_identity': cg.get('consistent_identity'),
                'memory_current_bytes': obs.get('memory.current', {}).get('value'),
                'memory_peak_bytes': obs.get('memory.peak', {}).get('value')}

    count = live_count = mismatches = io_unknown = 0
    cgroup_unknown = {}
    device_unknown = 0
    last_live = final = None
    paths = set()
    peaks = {key: None for key in ('memory.current', 'memory.peak', 'memory.swap.current')}
    for sample in samples:
        count += 1
        final = sample
        if sample['binding']['status'] == 'bound_to_live_pid':
            live_count += 1
            last_live = sample
        mismatches += sample['binding']['status'] == 'mismatch'
        io_unknown += sum(p['io']['value'] is None for p in sample['processes'])
        cg = sample.get('cgroup') or {}
        if cg:
            paths.add(cg['path'])
        for name, observation in cg.get('observations', {}).items():
            if observation['value'] is None:
                cgroup_unknown[name] = cgroup_unknown.get(name, 0) + 1
        device_unknown += sum(item['stat']['value'] is None for item in
                              sample['model_devices']['devices'].values())
        for name in peaks:
            value = cg.get('observations', {}).get(name, {}).get('value')
            if cg.get('consistent_identity') and isinstance(value, int):
                peaks[name] = max(peaks[name], value) if peaks[name] is not None else value
    return {'schema_version': 1, 'samples_file': 'resource-samples.jsonl',
            'sample_count': count, 'live_pid_sample_count': live_count,
            'last_live_pid_sample': endpoint(last_live),
            'final_sample': endpoint(final),
            'observed_cgroup_counter_peaks_bytes': peaks,
            'cgroup_paths': sorted(paths),
            'binding_mismatch_samples': mismatches,
            'process_io_unknown_observations': io_unknown,
            'cgroup_unknown_observations': cgroup_unknown,
            'model_device_stat_unknown_observations': device_unknown,
            'notes': [
                'Cgroup memory.current/peak measure charge, not complete physical RAM.',
                'Thor cudaMalloc device bytes and externally charged warm cache may be absent from this cgroup.',
                'memory.stat file includes shmem; kernel and other views have overlapping subsets.',
                'rchar/wchar are logical bytes; read_bytes/write_bytes and io.stat describe storage accounting.',
                'Model-device stats are global background for selected filesystem devices; parent disks are not added.',
                'No physical total or I/O sum is formed across these overlapping views.',
                'Endpoint references locate original rows; the last live sample is not guaranteed to be the final request boundary.',
                'Missing/reset/new-identity counter intervals remain null with reasons in the sidecar.',
            ]}
