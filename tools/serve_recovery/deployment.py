"""Explicit, resumable STOPPED package switching with a durable legacy fence.

No model is started or signalled. A source-bound package is not by itself a
business acceptance claim. The caller supplies the selected package digest.
Rollback uses the same transaction and the CURRENT credential/limits/usage
journals, never a backup of mutable state.
"""
from contextlib import ExitStack, contextmanager
import fcntl
import os
from pathlib import Path
import uuid

from tools.serve_recovery import access_manager, access_policy, access_store, bundle
from tools.serve_recovery import deployment_policy as policy, ledger, quota_manager, quota_store
from tools.serve_recovery import store, usage_policy, usage_store
from tools.serve_service import package as engine


class Store(access_store.Store):
    contract = policy
    format = 'q4t-service-deployment-store-v1'
    marker_name = 'service-deployment.json'
    binding_format = 'q4t-service-deployment-binding-v1'
    directory_name = 'service-deployment'


@contextmanager
def claim(state, *, shared=False, name='deployment.lock', create=True):
    path = Path(state)/name
    fd = os.open(path, os.O_RDWR | (os.O_CREAT if create else 0) |
                 os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        held = store.check_file(fd)
        fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        named = path.stat(follow_symlinks=False)
        ledger.require((held.st_dev, held.st_ino) == (named.st_dev, named.st_ino),
                       'deployment lock changed')
        yield
    finally:os.close(fd)


def read_descriptor(state):
    value = policy.descriptor(access_manager.private_json(Path(state)/'service.json'))
    access_store.binding(state, value['service_id'])
    return value


def ready(state, desc):
    if (os.path.lexists(Path(state)/'service-publication.json') or
            os.path.lexists(Path(state)/'service-publication') or
            os.path.lexists(Path(state)/'service-publication-intents')):
        from tools.serve_recovery import publication
        publication.ready(state, desc)
    if desc['version'] == 1 and not os.path.lexists(Path(state)/Store.marker_name):
        return
    with access_store.open_bound(access_store.binding(state, desc['service_id']), Store) as current:
        value = current.head['state']['policy']
        ledger.require(value['phase'] == 'stable' and value['current'] == desc,
                       'deployment needs resume')


def check_target(current, target):
    old = bundle.verify(current['package'], current['manifest_sha256'])
    new = bundle.verify(target['package'], target['manifest_sha256'])
    ledger.require(old.get('ingress_mode') == 'native' and new.get('ingress_mode') == 'native' and
        new['version'] >= 8 and 'source/tools/serve_recovery/deployment.py' in new['files'],
        'target must preserve managed client state and deployment contract')
    ledger.require(all(old[k] == new[k] for k in ('repository', 'lock_path')),
                   'deployment authority changed')
    old_core = engine.verify(Path(current['package'])/'engine', old['engine_manifest_sha256'])
    new_core = engine.verify(Path(target['package'])/'engine', new['engine_manifest_sha256'])
    ledger.require(old_core['model_dir'] == new_core['model_dir'] and
        all(old_core['settings'].get(k) == new_core['settings'].get(k)
            for k in ('application_state', 'model_names')), 'deployment application identity changed')
    embedding = old_core['settings'].get('native_embedding')
    if embedding is not None:
        selected = new_core['settings'].get('native_embedding', {})
        ledger.require(all(selected.get(k) == embedding[k]
                           for k in engine.NATIVE_EMBEDDING),
                       'deployment would remove or change accepted embedding model')
    from tools.serve_recovery import profile_manager
    profile_manager.compatible(old, new, current['package'], target['package'])
    return old, new


def write_descriptor(state, value):
    """Prepared journal is the authority for rebuilding this disposable file."""
    state = Path(state)
    path = state/'service.switch'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        store.check_file(fd);os.ftruncate(fd, 0)
        store.Store.write_all(fd, ledger.canonical(value));os.fsync(fd)
    finally:os.close(fd)
    os.replace(path, state/'service.json')
    fd = os.open(state, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:os.fsync(fd)
    finally:os.close(fd)


FAMILIES = {'access': access_store.Store, 'limits': quota_store.Store, 'usage': usage_store.Store}


def seeds(state, manifest):
    result = []
    for name, writer in FAMILIES.items():
        if not os.path.lexists(Path(state)/writer.marker_name):
            ledger.require(manifest['version'] < (7 if name == 'access' else 8) and
                not os.path.lexists(Path(state)/writer.directory_name), 'missing established client state')
            result.append(name)
    return sorted(result)


def initial_policy(name, desc):
    if name == 'access':
        value = access_manager.private_json(Path(desc['package'])/'gateway-policy.json')
        initial = access_policy.from_keys([dict(id=c['id'], api_keys=[c['api_key']]) for c in value['clients']])
    elif name == 'limits':initial = quota_manager.capacity(quota_manager.base(desc), desc)
    else:initial = usage_policy.initial()
    return initial


def initialize_family(binding, name, desc):
    return access_store.bootstrap_bound(binding, initial_policy(name, desc), FAMILIES[name])


def consistent_clients(access, limits, target):
    base = quota_manager.base(target)
    quota_manager.capacity(limits, target)
    ids = [c['id'] for c in limits['clients']]
    known = [c['id'] for c in base['clients']]
    ledger.require(ids[:len(known)] == known and limits['fair_queue'] == base['fair_queue'] and
        (limits['revision'] > 0 or limits == base) and
        {c['id'] for c in access['clients']} <= set(ids), 'target client contract changed')


def switch(state, package, digest, *, expected, request_id, rollback=False,
           _publication=None, _writer=None, _locked_core=None):
    ledger.require(ledger.identifier(request_id) and type(expected) is str and
                   ledger.HASH.fullmatch(expected), 'deployment request identity')
    state = Path(state).absolute()
    desc = read_descriptor(state)
    binding = access_store.binding(state, desc['service_id'])
    target_path = str(Path(package).resolve(strict=True))
    writer = Store if _writer is None else _writer
    ledger.require(_writer is None or _publication is not None,
                   'deployment journal recovery requires publication ownership')
    # These locks exclude both new launchers and an already running Guardian.
    # Core writer ownership also excludes a stale pre-upgrade launcher worker.
    with ExitStack() as stack:
        if _publication is None:
            stack.enter_context(claim(state, shared=True, name='publication.lock'))
            ledger.require(not os.path.lexists(state/'service-publication.json') and
                not os.path.lexists(state/'service-publication') and
                not os.path.lexists(state/'service-publication-intents'),
                'published service requires coordinated default publication')
        if _locked_core is None:
            stack.enter_context(claim(state))
            stack.enter_context(claim(state, name='guardian.lock'))
            core = stack.enter_context(store.Store(state, desc['service_id']))
        else:
            ledger.require(_publication is not None and _locked_core.directory == state and
                _locked_core.service_id == desc['service_id'], 'publication ownership differs')
            held = os.fstat(_locked_core.lock_fd);named = (state/store.LOCK).lstat()
            directory = os.fstat(_locked_core.dir_fd);current_directory = state.lstat()
            ledger.require(not _locked_core.poisoned and
                (held.st_dev,held.st_ino) == (named.st_dev,named.st_ino) and
                (directory.st_dev,directory.st_ino) ==
                    (current_directory.st_dev,current_directory.st_ino) and
                _locked_core.read_state(store.STATE) == _locked_core.state,
                'publication core ownership changed')
            core = _locked_core
        ledger.require(core.state['desired'] == 'STOPPED' and not any(core.state['active'].values()),
                       'deployment requires durable STOPPED')
        ledger.require(read_descriptor(state) == desc, 'deployment descriptor raced')
        if not os.path.lexists(state/Store.marker_name):
            ledger.require(desc['version'] == 1 and desc['manifest_sha256'] == expected,
                           'missing established deployment journal')
            # Verify the new package before creating a durable transaction.
            check_target(desc, dict(desc, package=target_path, manifest_sha256=digest))
            access_store.bootstrap_bound(binding, policy.initial(desc), writer)
        journal = stack.enter_context(access_store.open_bound(binding, writer))
        value = journal.head['state']['policy']
        journals = {}
        if value['phase'] == 'stable':
            ledger.require(value['current'] == desc, 'deployment selection differs')
            if desc.get('deployment_id') == request_id:
                ledger.require(desc['package'] == target_path and desc['manifest_sha256'] == digest and
                    value['history'] and value['history'][-1]['manifest_sha256'] == expected,
                    'deployment idempotency conflict')
                bundle.verify(desc['package'], desc['manifest_sha256'])
                if _publication is not None:
                    _publication(value['history'][-1], desc)
                return dict(policy.describe(value), switched=True, replayed=True, automatic_start=False)
            ledger.require(desc['manifest_sha256'] == expected and
                all(d.get('deployment_id') != request_id for d in value['history']),
                'deployment predecessor changed')
            if rollback:
                direct = any(d['package'] == target_path and d['manifest_sha256'] == digest
                             for d in value['history'])
                if not direct:
                    from tools.serve_recovery import profile_compat
                    direct = profile_compat.retained_origin(value['history'], target_path, digest) is not None
                ledger.require(direct, 'rollback target is not retained deployment history')
            target = dict(version=2, service_id=desc['service_id'], package=target_path,
                manifest_sha256=digest, deployment_revision=desc.get('deployment_revision', 0)+1,
                deployment_id=request_id)
            old, selected = check_target(desc, target)
            bootstrap = seeds(state, old)
            checked = {}
            for name, writer in FAMILIES.items():
                if name in bootstrap:checked[name] = initial_policy(name, desc)
                else:
                    journals[name] = stack.enter_context(access_store.open_bound(binding, writer))
                    checked[name] = journals[name].head['state']['policy']
            consistent_clients(checked['access'], checked['limits'], target)
            from tools.serve_recovery import profile_manager
            profile_manager.preflight(binding, old, selected)
            candidate = policy.prepare(value, target, request_id, bootstrap)
            journal.transact(str(uuid.uuid5(uuid.UUID(request_id), 'prepare')), value['revision'], candidate)
            value = candidate
        else:
            ledger.require(value['request'] == request_id and value['current']['manifest_sha256'] == expected and
                value['target']['package'] == target_path and value['target']['manifest_sha256'] == digest and
                desc in (value['current'], value['target']), 'deployment resume identity changed')
            check_target(value['current'], value['target'])
        # Fence is durable before migration, so a stale old package cannot
        # restore static keys even if this operator dies after migration.
        core.require_service_contract()
        for name, writer in FAMILIES.items():
            if name in value['seeds']:initialize_family(binding, name, value['current'])
            if name not in journals:
                journals[name] = stack.enter_context(access_store.open_bound(binding, writer))
        consistent_clients(journals['access'].head['state']['policy'],
                           journals['limits'].head['state']['policy'], value['target'])
        from tools.serve_recovery import profile_manager
        old, selected = check_target(value['current'], value['target'])
        profile_manager.deploy(binding, value, old, selected, core.state)
        write_descriptor(state, value['target'])
        # All launchers that understand v8/v9 already reject this prepared
        # phase. Publish the coherent default generation before finalizing it.
        # An exception deliberately leaves a resumable prepared transaction.
        if _publication is not None:
            _publication(value['current'], value['target'])
        candidate = policy.finish(value)
        journal.transact(str(uuid.uuid5(uuid.UUID(request_id), 'finish')), value['revision'], candidate)
        ledger.require(read_descriptor(state) == candidate['current'], 'deployment commit differs')
        return dict(policy.describe(candidate), switched=True, replayed=False, automatic_start=False)


def status(state):
    desc = read_descriptor(state)
    with access_store.open_bound(access_store.binding(state, desc['service_id']), Store) as journal:
        value = journal.head['state']['policy']
        return dict(policy.describe(value), selected_descriptor=desc,
                    ready=value['phase'] == 'stable' and value['current'] == desc)
