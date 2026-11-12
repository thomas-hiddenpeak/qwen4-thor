"""Operator lifecycle checks and reloads; no implicit model restart or launch."""
from contextlib import ExitStack
import fcntl
import os
from pathlib import Path
import stat
import uuid

from tools.serve_host import native
from tools.serve_recovery import access_store, bundle, control, deployment
from tools.serve_recovery import ledger, quota_manager, store, tls_control
from tools.serve_service import package as engine


def read_family(state, desc, writer):
    """Observe a durable family without reconciling an unknown pending commit."""
    state = Path(state).absolute()
    binding = access_store.binding(state, desc['service_id'])
    access_store.check_binding(binding, writer)
    directory = Path(state)/writer.directory_name
    info = directory.lstat()
    ledger.require(directory.resolve() == directory and stat.S_ISDIR(info.st_mode) and
        info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700,
        'private policy directory required')
    fd = os.open(directory/'writer.lock', os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        held = store.check_file(fd)
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        named = (directory/'writer.lock').lstat()
        ledger.require((held.st_dev, held.st_ino) == (named.st_dev, named.st_ino) and
            native.private_json(directory/'writer.lock') == dict(format=writer.format,
                service_id=desc['service_id'], state_identity=binding['state_identity']),
            'policy writer identity changed')
        ledger.require(not os.path.lexists(directory/'policy.pending'), 'policy commit unknown')
        value = native.private_json(directory/'policy.json', writer.contract.MAX_BYTES+4096)
        ledger.keys(value, ('format', 'service_id', 'state_identity',
                            'previous_sha256', 'sha256', 'state'))
        ledger.require(value['format'] == writer.format and
            value['service_id'] == desc['service_id'] and
            value['state_identity'] == binding['state_identity'] and
            value['sha256'] == ledger.digest(value['state']) and
            (value['previous_sha256'] is None or type(value['previous_sha256']) is str and
             ledger.HASH.fullmatch(value['previous_sha256'])), 'policy envelope identity changed')
        writer.validate_state(value['state'])
        access_store.check_binding(binding, writer)
        return value['state']['policy']
    finally:os.close(fd)


def selected_state(state, package, digest):
    from tools.serve_recovery import service
    desc = service.descriptor(state, package, digest)
    if os.path.lexists(Path(state)/deployment.Store.marker_name):
        current = read_family(state, desc, deployment.Store)
        ledger.require(current['phase'] == 'stable' and current['current'] == desc,
                       'deployment needs resume')
    else:
        ledger.require(desc['version'] == 1, 'deployment journal unavailable')
    from tools.serve_recovery import publication
    if publication.present(state):publication.ready(state,desc)
    return desc


def check(package, digest, state=None):
    """Validate a package and optional selected state, without model/GPU calls."""
    package = Path(package).resolve(strict=True)
    manifest = bundle.verify(package, digest)
    base = engine.verify(package/'engine', manifest['engine_manifest_sha256'])
    value = dict(status='PASS',package_verified=True,capacity=base['settings']['profile'],
        state_checked=state is not None, credentials_checked=False,
        model_actions=False, gpu_queries=False, automatic_start=False)
    if manifest.get('tls_configuration') is not None:
        scratch = Path(manifest['repository'])/'.q4t-work/service-checks'
        scratch.mkdir(mode=0o700, exist_ok=True)
        policy = engine.read(package/'gateway-policy.json')
        tls = tls_control.check(package/'bin/q4t-ingress', policy,
                               manifest['tls_configuration'], scratch)
        value['credentials_checked'] = tls.get('ready') is True
    if state is not None:
        state = Path(state).absolute()
        with deployment.claim(state, shared=True):
            desc = selected_state(state, package, digest)
            core = native.read_ledger(dict(state=str(state), descriptor=desc))
            value.update(service_id=desc['service_id'], desired=core['desired'],
                         active_grants=core['active'])
            if manifest['version'] >= 8 and manifest.get('ingress_mode') == 'native':
                current = {name: read_family(state, desc, writer)
                           for name, writer in deployment.FAMILIES.items()}
                deployment.consistent_clients(current['access'], current['limits'], desc)
                value.update(client_state_valid=True,client_count=len(current['limits']['clients']))
            if manifest.get('recovery_profiles') is not None:
                from tools.serve_recovery import profile_manager
                value['adaptive_profiles'] = profile_manager.status(state, desc, manifest)
    return value


def stopped(state, package, digest):
    """An absent control endpoint alone is not evidence of completed cleanup."""
    state = Path(state).absolute()
    with ExitStack() as locks:
        locks.enter_context(deployment.claim(state, shared=True))
        locks.enter_context(deployment.claim(state, name='guardian.lock'))
        locks.enter_context(deployment.claim(state, name=store.LOCK, create=False))
        desc = selected_state(state, package, digest)
        ledger.require(native.private_json(state/store.LOCK) ==
            dict(version=ledger.VERSION, service_id=desc['service_id']), 'core writer identity changed')
        core = native.read_ledger(dict(state=str(state), descriptor=desc))
        ledger.require(core['desired'] == 'STOPPED' and not core['active'] and
                       not pending_outer(state, desc),
                       'service stop is not confirmed')
        return dict(service_id=desc['service_id'], state='STOPPED', desired='STOPPED',
            live_control=False, business_ready=False, cleanup_confirmed=True,
            model_actions=False, automatic_start=False)


def pending_outer(state, desc):
    from tools.serve_recovery import outer_store
    if not os.path.lexists(Path(state)/outer_store.Store.marker_name):
        ledger.require(not os.path.lexists(Path(state)/outer_store.Store.directory_name),
                       'outer state lost its persistent binding')
        return False
    return read_family(state, desc, outer_store.Store)['pending'] is not None


def request_stop(state, package, digest):
    """Persist operator intent even if the live Guardian endpoint is absent.

    A live instance or writer always prevents offline mutation. Old slots are
    retained for actual reconciliation: STOPPED never implies they are empty.
    """
    state = Path(state).absolute()
    with deployment.claim(state, shared=True), deployment.claim(state, name='guardian.lock'):
        desc = selected_state(state, package, digest)
        pending = pending_outer(state, desc)
        with store.Store(state, desc['service_id']) as current:
            if current.state['desired'] != 'STOPPED':
                current.transact(dict(id=str(uuid.uuid4()), expected_revision=current.state['revision'],
                    operation='desired', args=dict(state='STOPPED')))
            cleaned = not any(current.state['active'].values()) and not pending
            return dict(service_id=desc['service_id'], state='STOPPED' if cleaned else 'DRAINING',
                desired='STOPPED', live_control=False, business_ready=False, stop_durable=True,
                cleanup_confirmed=cleaned, reconciliation_required=not cleaned,
                model_actions=False, automatic_start=False)


def resume(state, package, digest):
    """Resume original RUNNING intent; never manufacture an activation.

    The actual Guardian re-reads intent before its own role grants, covering an
    operator stop between this preflight and process dispatch. Unknown old slots
    must go through the outer reconciliation path before this entry is called.
    """
    from tools.serve_recovery import service
    manifest = bundle.verify(package, digest)
    state = Path(state).absolute()
    with deployment.claim(state, shared=True):
        with deployment.claim(state, name='guardian.lock'):
            desc = selected_state(state, package, digest)
            pending = pending_outer(state, desc)
            with store.Store(state, desc['service_id']) as current:
                if current.state['desired'] == 'STOPPED':
                    return dict(service_id=desc['service_id'], state='STOPPED', desired='STOPPED',
                        resumed=False, automatic_start=False, model_actions=False,
                        cleanup_confirmed=not any(current.state['active'].values()) and not pending)
                ledger.require(current.state['quarantine'] is None and
                    not any(current.state['active'].values()) and not pending,
                    'resume requires original unprotected reconciled state')
        return service.launch_owned(package, digest, state, manifest=manifest, resume=True)


def reload(package, digest, state, *, component, proposal=None, wait_seconds=3):
    from tools.serve_recovery import access_manager
    ledger.require(component in ('tls', 'access', 'limits'), 'invalid reload component')
    ledger.require((proposal is not None) == (component != 'tls'),
                   'credential/limits reload needs a proposal; TLS uses sealed file paths')
    state = Path(state).absolute()
    manifest = bundle.verify(package, digest)
    with deployment.claim(state, shared=True):
        desc = selected_state(state, package, digest)
        if component == 'tls':
            ledger.require(manifest.get('tls_configuration') is not None,
                           'TLS is not configured in the selected deployment')
            snapshot = control.request(desc['service_id'], 'status', timeout=3)
            result = tls_control.execute(desc, snapshot, manifest, reload=True)
        else:
            manager = access_manager if component == 'access' else quota_manager
            result = manager.apply(state, desc, manifest, proposal, wait_seconds=wait_seconds)
            result['reloaded'] = result['live_applied']
        return dict(result, component=component, model_actions=False)
