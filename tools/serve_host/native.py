"""Read-only projection of a pinned Guardian service; no recovery authority."""
import hashlib
import math
import os
from pathlib import Path
import re
import stat
import time

from tools.serve_ops import contracts, identity, transport
from tools.serve_recovery import control, journal, ledger, store

STATES = ('STARTING', 'READY', 'READY_DEGRADED', 'DRAINING', 'RECOVERING',
          'QUARANTINED', 'WAIT_DEPENDENCY', 'CIRCUIT_OPEN', 'STOPPED', 'UNKNOWN')
SOURCES = ('guardian', 'ledger', 'process', 'runtime')
REASONS = ('none', 'source_unavailable', 'identity_changed', 'source_stale',
           'collector_unavailable', 'collector_timeout', 'collector_budget')
STALE_SECONDS = 3
MAX_PACKET = 128 * 1024


def inode(path):
    value = Path(path).stat()
    return [value.st_dev, value.st_ino]


def stamp():
    return dict(boot_id=identity.bounded_file('/proc/sys/kernel/random/boot_id', 64)
                .decode().strip(), boottime=time.clock_gettime(time.CLOCK_BOOTTIME),
                unix=time.time())


def private_json(path, maximum=ledger.MAX_BYTES + 1024):
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        ledger.require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
            and info.st_nlink == 1 and not stat.S_IMODE(info.st_mode) & 0o077,
            'private observation source required')
        with os.fdopen(os.dup(fd), 'rb') as stream:
            raw = stream.read(maximum + 1)
        ledger.require(len(raw) <= maximum, 'observation source size bound')
        return store.decode(raw)
    finally:
        os.close(fd)


def bind(package, digest, state):
    # Called only in the isolated collector. Verification does not start a model.
    from tools.serve_recovery import bundle, service
    from tools.serve_service import package as engine
    package, state = Path(package).resolve(strict=True), Path(state).absolute()
    manifest = bundle.verify(package, digest)
    desc = service.descriptor(state, package, digest)
    base = engine.verify(package / 'engine', manifest['engine_manifest_sha256'])
    return dict(package=str(package), digest=digest, state=str(state),
        descriptor=desc, state_inode=inode(state), context={
            n: inode('/proc/self/ns/' + n) for n in ('pid', 'user', 'net', 'mnt')},
        boot_id=stamp()['boot_id'], guardian=inode(package / 'bin/q4t-guardian'),
        engine=inode(package / 'engine/q4t'), python=inode(manifest['python']['path']),
        port=base['settings']['operations_port'],
        observation_version=engine.read(package / 'engine/q4t.release.json')
            ['chat_protocol']['observability']['version'])


def process(pid, start, executable, context, *, parent=None):
    ledger.require(ledger.integer(pid, minimum=2) and ledger.integer(start, minimum=1),
                   'invalid process identity')
    path = Path('/proc') / str(pid)
    fields = identity.bounded_file(path / 'stat', 65536).decode().rsplit(')', 1)[1].split()
    ledger.require(int(fields[19]) == start and fields[0] not in ('Z', 'X') and
        (parent is None or int(fields[1]) == parent) and inode(path / 'exe') == executable
        and all(inode(path / 'ns' / n) == v for n, v in context.items()),
        'process identity changed')
    ledger.require(journal.process_start(pid) == start, 'process birth changed')


def empty(reason='source_unavailable', now=None):
    ledger.require(reason in REASONS, 'invalid observation reason')
    return dict(version=1, sampled=stamp() if now is None else now,
        state='UNKNOWN', reason=reason, sources={n: False for n in SOURCES},
        business_ready=None, observation=None, recovery_frozen=None,
        generations=None, ledger=None, process=None, runtime=None, auxiliary=None,
        gateway_binding=None, version_recovery=None)


def project_version(value):
    """Bounded public fields; never publish helper PIDs or retained paths."""
    if value is None:return None  # Pre-version-control packages remain readable.
    from tools.serve_recovery import version_policy as policy
    ledger.require(type(value) is dict and value.get('enabled') is True and
                   ledger.identifier(value.get('cycle_id')), 'invalid live version cycle')
    policy.hash_value(value.get('root_manifest_sha256'))
    if value.get('payload_manifest_sha256') is not None:
        policy.hash_value(value['payload_manifest_sha256'])
    for name in ('selected', 'launch_payload'):
        ledger.require(value.get(name) in (None, *policy.PAYLOADS), 'invalid observed payload')
    flags = ('fallback_consumed', 'protected', 'pending', 'payload_prepared', 'state_available')
    ledger.require(all(type(value.get(name)) is bool for name in flags),
                   'invalid observed recovery state')
    last = value.get('last_fault')
    if last is not None:
        policy.fault(last)
        ledger.require(policy.recognized(last), 'unknown observed version fault')
    reason = value.get('reason')
    ledger.require(type(reason) is str and len(reason.encode()) <= 1024,
                   'invalid version observation reason')
    names = ('enabled', 'cycle_id', 'root_manifest_sha256', 'payload_manifest_sha256',
             'selected', 'launch_payload', *flags, 'last_fault', 'reason')
    return {name: value.get(name) for name in names}


def payload_binding(binding, value):
    """Resolve only registered payloads of an already authenticated native root.

    This runs in the isolated collector. Qualification is verified once per
    immutable cycle; its executable inodes remain frozen across observations.
    No writer lock or repair operation is used, even if a commit is pending.
    """
    from tools.serve_recovery import version_registration
    from tools.serve_service import package as engine
    projection = project_version(value.get('version_recovery'))
    if projection is None:
        ledger.require(not version_registration.present(binding['state']),
                       'registered service has no live version projection')
        return binding
    ledger.require(projection['root_manifest_sha256'] == binding['digest'],
                   'observed control root differs')
    cycle = projection['cycle_id']
    cache = binding.get('version_payloads')
    if cache is None or cache['cycle_id'] != cycle:
        registration = version_registration.for_cycle(binding['state'], binding['descriptor'], cycle)
        payloads = {}
        for name, item in registration['payloads'].items():
            package = Path(item['descriptor']['package'])
            payloads[item['descriptor']['manifest_sha256']] = dict(
                {k:v for k,v in binding.items() if k != 'version_payloads'},
                engine=inode(package/'engine/q4t'),
                observation_version=engine.read(package/'engine/q4t.release.json')
                    ['chat_protocol']['observability']['version'],
                port=item['binding']['operations_port'], payload=name,
                binary_sha256=item['binding']['binary_sha256'],
                protocol_sha256=item['binding']['protocol_sha256'])
        cache = dict(cycle_id=cycle, payloads=payloads)
        binding['version_payloads'] = cache
    digest = projection['payload_manifest_sha256']
    if digest is None:
        ledger.require(not value.get('business_ready') and
            all(value.get(role+'_pid') is None for role in ledger.ROLES),
            'running payload identity missing')
        return binding
    ledger.require(digest in cache['payloads'], 'unregistered running payload')
    selected = cache['payloads'][digest]
    if value.get('model_ready'):
        ledger.require(value.get('model_binary_sha256') == selected['binary_sha256'] and
            value.get('model_protocol_sha256') == selected['protocol_sha256'],
            'observed model differs from prepared payload')
    return selected


def project_status(value):
    ledger.require(type(value) is dict and value.get('version') == 1 and
        value.get('state') in STATES and value['state'] != 'UNKNOWN', 'invalid Guardian state')
    for name in ('business_ready', 'recovery_frozen', 'model_ready',
                 'model_admission_open', 'controller_ready', 'gateway_ready'):
        ledger.require(type(value.get(name)) is bool, 'invalid Guardian readiness')
    generations = {role: value[role + '_generation'] for role in ledger.ROLES}
    ledger.require(all(ledger.integer(v) for v in generations.values()), 'invalid generation')
    ready = value['business_ready']
    ledger.require(not ready or value['state'] in ('READY', 'READY_DEGRADED') and
        value['model_ready'] and value['model_admission_open'] and
        value['controller_ready'] and value['gateway_ready'] and
        value['gateway_bound_model'] == generations['model'], 'contradictory readiness')
    ledger.require(not ready or all(ledger.integer(value.get(role + '_pid'), minimum=2)
        and ledger.integer(value.get(role + '_start_ticks'), minimum=1) and generations[role] >= 1
        for role in ledger.ROLES), 'ready process identity missing')
    health = value.get('controller_health')
    observation = auxiliary = None
    age = value.get('controller_health_age_ms')
    if type(health) is dict and ledger.integer(age) and age <= STALE_SECONDS * 1000:
        observed = health.get('observation')
        if observed in ('AVAILABLE', 'UNKNOWN', 'DEVICE_FAILURE'):
            observation = observed
        io = health.get('auxiliary_io')
        if type(io) is dict:
            names = ('acknowledged_operations', 'dropped_bytes', 'failed_operations',
                     'pending_records', 'queued_bytes', 'queued_records')
            if all(ledger.integer(io.get(n)) for n in names):
                auxiliary = {n: io[n] for n in names}
    gateway = None
    reason = value.get('gateway_binding_reason')
    generation = value.get('gateway_binding_model_generation')
    age = value.get('gateway_binding_age_ms')
    if (reason in control.GATEWAY_REASONS and ledger.integer(generation) and
            generation in (0, generations['model']) and ledger.integer(age) and
            (age <= STALE_SECONDS * 1000 or reason == 'none' and ready)):
        gateway = dict(reason=reason, model_generation=generation, age_ms=age)
    return dict(state=value['state'], business_ready=ready,
        observation=observation, recovery_frozen=value['recovery_frozen'],
        generations=generations, auxiliary=auxiliary, gateway_binding=gateway,
        version_recovery=project_version(value.get('version_recovery')))


def read_ledger(binding):
    directory = Path(binding['state'])
    # Never reconcile a pending commit or acquire the writer lock as an observer.
    ledger.require(not os.path.lexists(directory / 'ledger.pending'), 'ambiguous ledger commit')
    ledger.require(not os.path.lexists(directory / store.CONTRACT_PENDING), 'ambiguous service contract')
    value = private_json(directory / 'ledger.json')
    store.envelope_keys(value)
    ledger.require(value['sha256'] == ledger.digest(value['state']), 'ledger checksum')
    ledger.validate(value['state'], binding['descriptor']['service_id'])
    value = value['state']
    return dict(revision=value['revision'], desired=value['desired'],
        active=any(v is not None for v in value['active'].values()),
        quarantined=value['quarantine'] is not None)


def process_resources(pid):
    raw = identity.bounded_file(f'/proc/{pid}/status', 65536).decode()
    result = {}
    for field, name in (('VmRSS', 'rss_bytes'), ('VmHWM', 'rss_peak_bytes')):
        match = re.search(r'^' + field + r':\s+([0-9]+) kB$', raw, re.M)
        ledger.require(match is not None, 'missing process memory evidence')
        result[name] = int(match[1]) * 1024
    match = re.search(r'^Threads:\s+([0-9]+)$', raw, re.M)
    ledger.require(match is not None, 'missing process task evidence')
    result['threads'] = int(match[1])
    ledger.require(all(ledger.integer(v) for v in result.values()), 'process resource bound')
    return result


def runtime_metrics(binding, pid):
    # Verify loopback operations listener ownership before reading any metrics.
    path = Path('/proc') / str(pid)
    owned = set()
    import itertools
    files = list(itertools.islice((path / 'fd').iterdir(), 4097))
    ledger.require(len(files) <= 4096, 'descriptor limit')
    for file in files:
        try:
            match = re.fullmatch(r'socket:\[([0-9]+)\]', os.readlink(file))
            if match:
                owned.add(match[1])
        except FileNotFoundError:
            pass
    selected = identity.listener(identity.bounded_file('/proc/net/tcp').decode(),
                                 binding['port'], owned)
    ledger.require(selected['listener_scope'] == 'loopback', 'private operations listener required')
    response = transport.get(binding['port'], '/metrics', timeout=.5)
    ledger.require(response['http_status'] == 200, 'metrics unavailable')
    values, _ = contracts.parse_metrics(response['body'], version=binding['observation_version'])
    # Zero is the core wire sentinel for unknown memory, not a measured value.
    if not values.get('q4t_memory_observation_valid'):
        values.pop('q4t_memory_available_bytes', None)
    return values


def collect(binding, *, request=control.request, check_process=process,
            resources=process_resources, runtime=runtime_metrics):
    now = stamp()
    result = empty(now=now)
    try:
        ledger.require(now['boot_id'] == binding['boot_id'] and
            inode(binding['state']) == binding['state_inode'] and
            private_json(Path(binding['state']) / 'service.json') == binding['descriptor'],
            'service binding changed')
    except (OSError, ValueError, KeyError):
        return empty('identity_changed', now)
    try:
        result['ledger'] = read_ledger(binding)
        result['sources']['ledger'] = True
    except (OSError, ValueError, KeyError, TypeError):
        pass  # A broken observational file cannot override fresh Guardian truth.
    service_id = binding['descriptor']['service_id']
    try:
        value = request(service_id, 'status', timeout=.5)
        ledger.require(value['service_id'] == service_id, 'foreign service')
        check_process(value['guardian_pid'], value['guardian_start_ticks'],
                      binding['guardian'], binding['context'])
        projected = project_status(value)
        running = payload_binding(binding, value)
        for role, executable, parent in (
                ('controller', 'python', value['guardian_pid']),
                ('gateway', 'python', value['guardian_pid']),
                ('model', 'engine', value.get('controller_pid'))):
            if value.get(role + '_pid') is not None:
                check_process(value[role + '_pid'], value[role + '_start_ticks'],
                    running[executable], binding['context'], parent=parent)
        result.update(projected, reason='none')
        result['sources']['guardian'] = True
    except (OSError, ValueError, KeyError, TypeError):
        # STOPPED is a durable intent with no active grant, never a GPU-empty claim.
        if result['ledger'] and result['ledger']['desired'] == 'STOPPED' and not result['ledger']['active']:
            result.update(state='STOPPED', business_ready=False, reason='none')
        return result
    pid = value.get('model_pid')
    if pid is None:
        return result
    for name, reader in (('process', lambda: resources(pid)),
                         ('runtime', lambda: runtime(running, pid))):
        try:
            result[name] = reader()
            result['sources'][name] = True
        except (OSError, ValueError, RuntimeError, KeyError, TypeError):
            pass
    try:
        # A replacement during collection invalidates every runtime projection.
        after = request(service_id, 'status', timeout=.5)
        keys = ('guardian_pid', 'guardian_start_ticks', 'controller_generation',
                'controller_pid', 'controller_start_ticks', 'model_generation',
                'model_pid', 'model_start_ticks', 'gateway_generation', 'gateway_pid',
                'gateway_start_ticks')
        ledger.require(all(value[k] == after[k] for k in keys), 'generation changed')
        before_version = projected['version_recovery']
        after_version = project_version(after.get('version_recovery'))
        ledger.require((before_version is None) == (after_version is None) and
            (before_version is None or all(before_version[k] == after_version[k]
             for k in ('cycle_id', 'root_manifest_sha256', 'payload_manifest_sha256'))),
            'running version changed during observation')
        check_process(pid, value['model_start_ticks'], running['engine'],
                      binding['context'], parent=value['controller_pid'])
        result.update(project_status(after))
    except (OSError, ValueError, KeyError, TypeError):
        return empty('identity_changed', now)
    return result


def current(value, *, now=None, reason='collector_unavailable'):
    now = stamp() if now is None else now
    if value is None:
        return empty(reason, now)
    sampled = value['sampled']
    age = now['boottime'] - sampled['boottime']
    if sampled['boot_id'] != now['boot_id'] or not 0 <= age <= STALE_SECONDS:
        return empty('source_stale', now)
    # Preserve actual capture time. Rendering a cached value never renews it.
    return value


def metrics(value):
    lines = []
    def emit(name, amount, labels=''):
        ledger.require(type(amount) in (int, float) and math.isfinite(amount)
                       and amount >= 0, 'invalid public metric')
        lines.append(f'q4t_service_{name}' + ('{' + labels + '}' if labels else '') + f' {amount}')
    emit('sample_timestamp_seconds', value['sampled']['unix'])
    for name in STATES:
        emit('state', int(name == value['state']), f'state="{name}"')
    for name in REASONS:
        emit('reason', int(name == value['reason']), f'reason="{name}"')
    for name in SOURCES:
        emit('source_known', int(value['sources'][name]), f'source="{name}"')
    if value['business_ready'] is not None:
        emit('business_ready', int(value['business_ready']))
    gateway = value.get('gateway_binding')
    if gateway is not None:
        for name in control.GATEWAY_REASONS:
            emit('gateway_binding_reason', int(name == gateway['reason']),
                 f'reason="{name}"')
        emit('gateway_binding_age_seconds', gateway['age_ms'] / 1000)
    if value['observation'] is not None:
        for name in ('AVAILABLE', 'UNKNOWN', 'DEVICE_FAILURE'):
            emit('auxiliary_observation', int(value['observation'] == name), f'state="{name}"')
    if value['recovery_frozen'] is not None:
        emit('recovery_frozen', int(value['recovery_frozen']))
    recovery = value.get('version_recovery')
    if recovery is not None:
        for name in ('fallback_consumed', 'protected', 'pending', 'state_available'):
            emit('version_'+name, int(recovery[name]))
    for role, generation in (value['generations'] or {}).items():
        emit('generation', generation, f'role="{role}"')
    for name, amount in (value['process'] or {}).items():
        emit('process_' + name, amount)
    for name, amount in (value['auxiliary'] or {}).items():
        emit('auxiliary_' + name, amount)
    # Keys came from the fixed core metric contract, never raw upstream text.
    lines.extend(f'{name} {amount}' for name, amount in (value['runtime'] or {}).items())
    return '\n'.join(lines) + '\n'
