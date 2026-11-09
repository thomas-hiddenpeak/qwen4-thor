"""Explicit resumable creation; descriptor publication is the authority boundary.

Before publication, a durable intent fixes the UUID, package and initial core
state. After publishing begins, lost journals can NEVER be rebuilt from seeds.
This module runs in an operator process only and never starts a model.
"""
from contextlib import ExitStack
import fcntl
import os
from pathlib import Path
import stat
import uuid

from tools.serve_recovery import access_manager, access_store, deployment, deployment_policy
from tools.serve_recovery import ledger, store

FORMAT = 'q4t-service-initialization-v1'
INTENT = 'initialization.json'
PENDING = 'initialization.pending'


def validate(value, binding, package, digest):
    ledger.keys(value, ('format', 'binding', 'descriptor', 'core', 'phase'))
    desc = deployment_policy.descriptor(value['descriptor'])
    ledger.require(value['format'] == FORMAT and value['binding'] == binding and
        desc['version'] == 2 and desc['deployment_revision'] == 1 and
        desc['service_id'] == binding['service_id'] and desc['package'] == str(package) and
        desc['manifest_sha256'] == digest and value['phase'] in ('prepared', 'publishing', 'complete'),
        'initialization identity changed')
    initial = value['core']
    ledger.validate(initial, desc['service_id'])
    ledger.require(initial['revision'] == 0 and initial['desired'] == 'STOPPED' and
        initial['last'] is None and not any(initial['active'].values()) and
        not any(initial['generations'].values()), 'initialization seed is not fresh')
    ledger.require(len(ledger.canonical(value)) <= 128*1024, 'initialization intent bound')
    return value


def write_intent(state, value):
    state = Path(state)
    fd = os.open(state/PENDING, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        store.check_file(fd);os.ftruncate(fd, 0)
        store.Store.write_all(fd, ledger.canonical(value));os.fsync(fd)
    finally:os.close(fd)
    os.replace(state/PENDING, state/INTENT)
    directory = os.open(state, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:os.fsync(directory)
    finally:os.close(directory)


def load_intent(state, package, digest):
    if os.path.lexists(state/INTENT):
        value = access_manager.private_json(state/INTENT)
    else:
        # No core or client file may exist without a published initial intent.
        # A partial very first intent has never granted authority, so it can be
        # replaced. A complete pending intent retains its exact original UUID.
        ledger.require({p.name for p in state.iterdir()} <=
            {'deployment.lock', 'guardian.lock', PENDING}, 'unbound initialization state')
        value = None
        if os.path.lexists(state/PENDING):
            try:value = access_manager.private_json(state/PENDING)
            except ledger.LedgerError:pass
        if value is None:
            sid = str(uuid.uuid4())
            value = dict(format=FORMAT, binding=access_store.binding(state, sid),
                descriptor=dict(version=2, service_id=sid, package=str(package), manifest_sha256=digest,
                                deployment_revision=1, deployment_id=str(uuid.uuid4())),
                core=ledger.new_state(sid, store.stamp()), phase='prepared')
        ledger.require(type(value) is dict, 'invalid initialization intent')
        desc = deployment_policy.descriptor(value.get('descriptor'))
        binding = access_store.binding(state, desc['service_id'])
        validate(value, binding, package, digest)
        ledger.require(value['phase'] == 'prepared', 'unpublished initialization phase')
        write_intent(state, value)
    ledger.require(type(value) is dict, 'invalid initialization intent')
    desc = deployment_policy.descriptor(value.get('descriptor'))
    binding = access_store.binding(state, desc['service_id'])
    return validate(value, binding, package, digest)


def initialize_core(state, value):
    """Finish only the unpublished, exact initial core files; no fresh budget."""
    ledger.require(not os.path.lexists(state/'service.json'), 'core was already published')
    sid = value['descriptor']['service_id']
    raw = ledger.canonical(dict(sha256=ledger.digest(value['core']), previous_sha256=None,
        state=value['core'], service_contract=store.SERVICE_CONTRACT))
    marker = ledger.canonical(dict(version=ledger.VERSION, service_id=sid))
    fd = os.open(state/store.LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        held = store.check_file(fd);fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        named = (state/store.LOCK).lstat()
        ledger.require((held.st_dev, held.st_ino) == (named.st_dev, named.st_ino) and
            held.st_size <= len(marker) and marker.startswith(os.pread(fd, len(marker)+1, 0)) and
            not os.path.lexists(state/store.CONTRACT_PENDING), 'unpublished core identity changed')
        os.ftruncate(fd, 0);store.Store.write_all(fd, marker);os.fsync(fd)
        if os.path.lexists(state/store.STATE):
            source = os.open(state/store.STATE, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                size = store.check_file(source).st_size
                ledger.require(size == len(raw) and os.pread(source, len(raw)+1, 0) == raw and
                    not os.path.lexists(state/store.PENDING), 'unpublished core is not initial state')
                os.fsync(source)
            finally:os.close(source)
        else:
            pending = os.open(state/store.PENDING, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            try:
                size = store.check_file(pending).st_size
                ledger.require(size <= len(raw) and raw.startswith(os.pread(pending, len(raw)+1, 0)),
                               'unpublished core pending differs')
                os.ftruncate(pending, 0);store.Store.write_all(pending, raw);os.fsync(pending)
            finally:os.close(pending)
            os.replace(state/store.PENDING, state/store.STATE)
        directory = os.open(state, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:os.close(fd)


def finish(state, package, digest, manifest):
    state, package = Path(state).absolute(), Path(package).resolve(strict=True)
    access_store.binding(state, str(uuid.UUID(int=0)))
    ledger.require(any(state.is_relative_to(Path(manifest['repository'])/name)
                       for name in ('build', '.q4t-work')), 'initialization outside repository work/build')
    ledger.require(manifest['version'] >= 8 and manifest.get('ingress_mode') == 'native',
                   'resumable initialization requires managed native profile')
    with ExitStack() as locks:
        locks.enter_context(deployment.claim(state))
        locks.enter_context(deployment.claim(state, name='guardian.lock'))
        value = load_intent(state, package, digest)
        desc, binding = value['descriptor'], value['binding']
        published = value['phase'] != 'prepared'
        if not published:initialize_core(state, value)
        core = locks.enter_context(store.Store(state, desc['service_id']))
        ledger.require(core.service_contract and core.state['desired'] == 'STOPPED' and
            not any(core.state['active'].values()), 'initialization requires stopped native authority')
        journals = {}
        for name, writer in deployment.FAMILIES.items():
            if not published:deployment.initialize_family(binding, name, desc)
            journals[name] = locks.enter_context(access_store.open_bound(binding, writer))
        deployment.consistent_clients(journals['access'].head['state']['policy'],
                                      journals['limits'].head['state']['policy'], desc)
        if not published:
            access_store.bootstrap_bound(binding, deployment_policy.initial(desc), deployment.Store)
        selection = locks.enter_context(access_store.open_bound(binding, deployment.Store))
        ledger.require(selection.head['state']['policy']['phase'] == 'stable' and
            selection.head['state']['policy']['current'] == desc, 'initialization deployment changed')
        (state/'runs').mkdir(mode=0o700, exist_ok=True)
        info = (state/'runs').lstat()
        ledger.require((state/'runs').resolve(strict=True) == state/'runs' and
            stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and
            stat.S_IMODE(info.st_mode) == 0o700,
            'private run directory required')
        if value['phase'] == 'prepared':
            value = dict(value, phase='publishing');write_intent(state, value)
        if os.path.lexists(state/'service.json'):
            ledger.require(deployment.read_descriptor(state) == desc, 'published initialization changed')
        else:deployment.write_descriptor(state, desc)
        if value['phase'] != 'complete':write_intent(state, dict(value, phase='complete'))
        return dict(state=str(state), service_id=desc['service_id'], initialized=True,
                    desired='STOPPED', automatic_start=False, resumed=published)
