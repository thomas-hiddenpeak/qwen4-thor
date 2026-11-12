"""Explicit management of outer recovery; no implicit initialization or launch.

The durable initialization intent is separate from the policy journal. Runtime
and operator transitions require its COMPLETE publication, so interrupted
bootstrap may finish exact initial files, but lost published history cannot be
recreated from the seed. Enable/disable never writes the core service intent.
"""
from contextlib import ExitStack
import os
from pathlib import Path
import uuid

from tools.serve_host import native
from tools.serve_recovery import access_store, bundle, deployment, ledger, lifecycle
from tools.serve_recovery import outer_instance, outer_policy, outer_store, store

FORMAT = 'q4t-outer-initialization-v1'
INTENT = 'outer-initialization.json'
PENDING = 'outer-initialization.pending'


def validate(value, binding):
    ledger.keys(value, ('format', 'binding', 'initial', 'phase'))
    ledger.require(value['format'] == FORMAT and value['binding'] == binding and
        value['phase'] in ('prepared', 'complete'), 'outer initialization identity changed')
    initial = outer_policy.validate(value['initial'])
    ledger.require(initial['service_id'] == binding['service_id'] and initial['revision'] == 0 and
        initial == outer_policy.initial(binding['service_id'], ledger.Stamp(
            initial['clock']['boot_id'], initial['clock']['monotonic_ns'])),
        'outer initialization seed is not fresh')
    return value


def write_intent(state, value):
    fd = os.open(state/PENDING, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        store.check_file(fd); os.ftruncate(fd, 0)
        store.Store.write_all(fd, ledger.canonical(value)); os.fsync(fd)
    finally: os.close(fd)
    os.replace(state/PENDING, state/INTENT)
    directory = os.open(state, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try: os.fsync(directory)
    finally: os.close(directory)


def load_initial(state, binding):
    if os.path.lexists(state/INTENT):
        return validate(native.private_json(state/INTENT), binding)
    ledger.require(not any(os.path.lexists(state/name) for name in
        (outer_store.Store.marker_name, outer_store.Store.directory_name,
         outer_store.Store.marker_name+'.pending')), 'unbound outer initialization state')
    value = None
    if os.path.lexists(state/PENDING):
        try: value = native.private_json(state/PENDING)
        except ledger.LedgerError: pass  # No policy authority ever existed.
    if value is None:
        value = dict(format=FORMAT, binding=binding,
            initial=outer_policy.initial(binding['service_id'], store.stamp()), phase='prepared')
    validate(value, binding)
    ledger.require(value['phase'] == 'prepared', 'unpublished outer initialization changed')
    write_intent(state, value)
    return value


def ready(state, desc):
    binding = access_store.binding(state, desc['service_id'])
    value = validate(native.private_json(Path(state)/INTENT), binding)
    ledger.require(value['phase'] == 'complete', 'outer initialization requires explicit resume')
    access_store.check_binding(binding, outer_store.Store)
    return binding


def selected(state, package, digest):
    manifest = bundle.verify(package, digest)
    ledger.require(manifest['version'] >= 18, 'outer management requires service profile v18')
    return lifecycle.selected_state(state, package, digest)


def initialize(state, package, digest):
    state = Path(state).absolute()
    with ExitStack() as locks:
        locks.enter_context(deployment.claim(state, shared=True))
        locks.enter_context(deployment.claim(state, name='outer-management.lock'))
        desc = selected(state, package, digest)
        instance = locks.enter_context(outer_instance.Instance(state, desc['service_id']))
        core = locks.enter_context(store.Store(state, desc['service_id']))
        ledger.require(core.service_contract and core.state['desired'] == 'STOPPED' and
            not any(core.state['active'].values()), 'outer initialization requires stopped empty core')
        value = load_initial(state, instance.binding)
        first = value['phase'] == 'prepared'
        if first:
            access_store.bootstrap_bound(instance.binding, value['initial'], outer_store.Store)
        with outer_store.open_bound(instance.binding) as current:
            policy = current.head['state']['policy']
            if first:
                ledger.require(policy == value['initial'], 'unpublished outer policy was already used')
                write_intent(state, dict(value, phase='complete'))
            return dict(outer_policy.describe(policy), service_id=desc['service_id'],
                initialized=first, initialization_complete=True, automatic_start=False)


def status(state, package, digest):
    state = Path(state).absolute()
    with deployment.claim(state, shared=True):
        desc = selected(state, package, digest)
        ready(state, desc)
        value = lifecycle.read_family(state, desc, outer_store.Store)
        return dict(outer_policy.describe(value), service_id=desc['service_id'],
            revision=value['revision'], initialization_complete=True, automatic_start=False)


def configure(state, package, digest, *, enabled):
    ledger.require(type(enabled) is bool, 'explicit outer enable flag required')
    state = Path(state).absolute()
    with deployment.claim(state, shared=True), deployment.claim(state, name='outer-management.lock'):
        desc = selected(state, package, digest)
        binding = ready(state, desc)
        with outer_store.open_bound(binding) as current:
            previous = current.head['state']['policy']
            value = previous
            if previous['enabled'] != enabled:
                value = outer_policy.change(previous, 'configure', dict(enabled=enabled), store.stamp())
                current.transact(str(uuid.uuid4()), previous['revision'], value)
            return dict(outer_policy.describe(value), service_id=desc['service_id'],
                revision=value['revision'], automatic_start=False, core_intent_changed=False)


def acknowledge(state, package, digest):
    state = Path(state).absolute()
    with ExitStack() as locks:
        locks.enter_context(deployment.claim(state, shared=True))
        locks.enter_context(deployment.claim(state, name='outer-management.lock'))
        desc = selected(state, package, digest)
        binding = ready(state, desc)
        locks.enter_context(outer_instance.Instance(state, desc['service_id']))
        current = locks.enter_context(outer_store.open_bound(binding))
        core = locks.enter_context(store.Store(state, desc['service_id']))
        ledger.require(core.service_contract, 'outer acknowledgement requires original core fence')
        previous = current.head['state']['policy']
        value = outer_policy.change(previous, 'acknowledge', dict(core=core.state), store.stamp())
        current.transact(str(uuid.uuid4()), previous['revision'], value)
        return dict(outer_policy.describe(value), service_id=desc['service_id'],
            acknowledged=True, automatic_start=False, core_intent_changed=False)
