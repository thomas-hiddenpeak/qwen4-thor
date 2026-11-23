"""Resume a registered root's last real cycle under STOPPED deployment locks.

One latest transfer intent and one checkpoint per retained cycle bound storage.
The immutable registration remains the payload authority. The caller owns the
deployment, Guardian and core locks; this module also holds the version writer
through profile migration and commit. No model/role operations are exposed.
"""
import copy
from contextlib import contextmanager
import os
from pathlib import Path
import uuid

from tools.serve_host import native
from tools.serve_recovery import access_store, bundle, deployment_policy, ledger
from tools.serve_recovery import publication_files as files, root_checkpoints as checkpoints
from tools.serve_recovery import root_version_policy as policy, store, version_contract
from tools.serve_recovery import version_policy, version_registration as registration, version_store
from tools.serve_service import package as engine

INTENT = 'root-version-transfer.json'
FORMAT = 'q4t-root-version-transaction-v1'
MAX_BYTES = policy.MAX_BYTES+4096


def records(binding, *, read_bundle=bundle.verify):
    result = {}
    for cycle, paths in registration.intents(binding['state_directory']).items():
        ledger.require(set(paths) == {'committed'}, 'version registration needs resume')
        path = paths['committed']
        value = registration.intent(native.private_json(path, registration.MAX_BYTES), binding,
                                    read_bundle=read_bundle)
        ledger.require(value['registration']['registry']['cycle_id'] == cycle,
                       'registration filename differs')
        result[cycle] = dict(value=value, sha256=engine.sha(path))
    return result


def marker(binding, row):
    cycle = row['value']['registration']['registry']['cycle_id']
    return checkpoints.identity(binding, row['sha256'], cycle)


def latest(binding, rows, target, current):
    matches = [row for row in rows.values() if row['value']['registration']['root'] ==
               dict(package=target['package'], manifest_sha256=target['manifest_sha256'])]
    if not matches: return None
    row = max(matches, key=lambda r:r['value']['candidate']['revision'])
    registry = row['value']['registration']['registry']
    saved = checkpoints.read(binding['state_directory'], marker(binding, row))
    candidates = [saved] if saved is not None else []
    if current['registry'] == registry: candidates.append(current)
    for item in rows.values():
        previous = item['value']['previous']
        if previous is not None and previous['registry'] == registry:
            candidates.append(previous)
    # An inactive root's initialization seed is never proof of its last state.
    ledger.require(candidates, 'retained root has no authenticated final checkpoint')
    selected = max(candidates, key=lambda c:c['revision'])
    ledger.require(selected['revision'] <= current['revision'], 'checkpoint is ahead of live state')
    for value in candidates: checkpoints.extends(value, selected)
    return row, copy.deepcopy(selected)


def validate(value, binding, rows):
    ledger.keys(value, ('format', 'binding', 'transfer', 'source_sha256', 'target_sha256'))
    ledger.require(value['format'] == FORMAT and value['binding'] == binding,
                   'root transfer binding differs')
    transfer = policy.validate(value['transfer'])
    ledger.require(transfer['deployment']['current']['service_id'] == binding['service_id'],
                   'root transfer service differs')
    for field, key, desc in [('before', 'source_sha256', 'current'),
                             ('checkpoint', 'target_sha256', 'target')]:
        selected = transfer[field]
        row = rows.get(selected['registry']['cycle_id'])
        target = transfer['deployment'][desc]
        ledger.require(row is not None and row['sha256'] == value[key] and
            row['value']['registration']['registry'] == selected['registry'] and
            row['value']['registration']['root'] == dict(package=target['package'],
                manifest_sha256=target['manifest_sha256']), 'root transfer registration differs')
    ledger.require(len(ledger.canonical(value)) <= MAX_BYTES, 'root transfer intent byte bound')
    return value


def prepare(binding, intent, current, rows):
    deployment_policy.validate(intent)
    ledger.require(intent['phase'] == 'prepared', 'prepared root deployment required')
    ledger.require(all(row['value']['candidate']['revision'] <= current['revision']
                       for row in rows.values()), 'version registration is not committed')
    if intent['current']['manifest_sha256'] == intent['target']['manifest_sha256']:
        return None
    target = latest(binding, rows, intent['target'], current)
    if target is None: return None  # Explicit new deployment registers its new cycle separately.
    row, saved = target
    transfer = policy.prepare(intent, current, saved, row['value']['registration']['registry'])
    source = rows.get(current['registry']['cycle_id'])
    ledger.require(source is not None, 'current root registration is missing')
    return validate(dict(format=FORMAT, binding=binding, transfer=transfer,
        source_sha256=source['sha256'], target_sha256=row['sha256']), binding, rows)


def read_intent(binding, rows):
    path = Path(binding['state_directory'])/INTENT
    if not os.path.lexists(path): return None
    return validate(native.private_json(path, MAX_BYTES), binding, rows)


def save(binding, rows, value):
    validate(value, binding, rows)
    path = Path(binding['state_directory'])/INTENT
    previous = read_intent(binding, rows)
    if previous is not None and previous != value:
        old = previous['transfer']['deployment']
        current = value['transfer']['deployment']['current']
        ledger.require(old['target']['deployment_revision'] <= current.get('deployment_revision', 0),
                       'previous root transfer has not completed')
        for field in ('before', 'candidate'):
            selected = previous['transfer'][field]
            actual = checkpoints.read(binding['state_directory'],
                marker(binding, rows[selected['registry']['cycle_id']]))
            ledger.require(actual is not None, 'previous transfer checkpoint is missing')
            checkpoints.extends(selected, actual)
    raw = ledger.canonical(value)
    pending = path.with_name(path.name+'.new')
    fd = os.open(pending, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = store.check_file(fd)
        ledger.require(info.st_size <= len(raw) and raw.startswith(os.pread(fd, len(raw)+1, 0)),
                       'partial root transfer belongs to another request')
        os.ftruncate(fd, 0); store.Store.write_all(fd, raw); os.fsync(fd)
    finally: os.close(fd)
    os.replace(pending, path); files.sync(path.parent)


def authorized_store(value):
    transfer = value['transfer']
    request = policy.request_id(transfer)

    class Contract:
        MAX_BYTES = version_policy.MAX_BYTES
        validate = staticmethod(version_policy.validate)
        digest = staticmethod(version_policy.digest)
        revision = staticmethod(version_policy.revision)

        @staticmethod
        def transition(before, following):
            if before['registry'] != following['registry']:
                return policy.authorized(transfer, before, following)
            return version_policy.transition(before, following)

    class AuthorizedStore(version_store.Store):
        contract = Contract

        def reconcile(self):
            if self.exists('policy.pending') and self.exists('policy.json'):
                before = self.envelope('policy.json')['state']['policy']
                pending = self.envelope('policy.pending')['state']
                if before['registry'] != pending['policy']['registry']:
                    ledger.require(pending['receipts'][-1]['id'] == request,
                                   'pending root transfer belongs to another request')
                    policy.authorized(transfer, before, pending['policy'])
            super().reconcile()
    return AuthorizedStore


def profile_context(value, rows, read_previous=bundle.verify):
    transfer = value['transfer']
    state = transfer['before']
    source = rows[state['registry']['cycle_id']]['value']['registration']
    primary = version_contract.payload(source['payloads']['primary']['descriptor'], read_bundle=read_previous)
    sources = [(primary['descriptor']['package'], primary['manifest'])]
    grants = {}
    if state['switch'] is not None:
        fallback = version_contract.payload(source['payloads']['fallback']['descriptor'], read_bundle=read_previous)
        sources.append((fallback['descriptor']['package'], fallback['manifest']))
        request = str(uuid.uuid5(uuid.UUID(state['switch']['request_id']),
                                'version-profile-migration'))
        grants[request] = [version_contract.registered_compatibility(primary, fallback, source, read_bundle=read_previous)]
    target = rows[transfer['candidate']['registry']['cycle_id']]['value']['registration']
    if read_previous is not bundle.verify:
        # A historical proof must never admit a stale target or its fallback.
        version_contract.verify(target)
    name = version_policy.launch_payload(transfer['candidate'])
    payload = version_contract.payload(target['payloads'][name]['descriptor'])
    return dict(sources=sources, grants=grants, target=payload['manifest'],
                target_package=payload['descriptor']['package'])


def preview(binding, intent, core, *, read_previous=bundle.verify):
    ledger.require(core['desired'] == 'STOPPED' and not any(core['active'].values()),
                   'root transfer requires durable stopped ownership')
    if not registration.present(binding['state_directory']): return None
    rows = records(binding, read_bundle=read_previous)
    with version_store.open_bound(binding) as writer:
        value = prepare(binding, intent, writer.head['state']['policy'], rows)
    return None if value is None else profile_context(value, rows, read_previous)


@contextmanager
def transaction(binding, intent, core, *, read_previous=bundle.verify):
    ledger.require(core['desired'] == 'STOPPED' and not any(core['active'].values()),
                   'root transfer requires durable stopped ownership')
    state = binding['state_directory']
    if not registration.present(state):
        yield None
        return
    rows = records(binding, read_bundle=read_previous)
    saved = read_intent(binding, rows)
    replay = saved is not None and saved['transfer']['deployment']['request'] == intent['request']
    if replay:
        ledger.require(saved['transfer']['deployment'] == intent, 'root transfer retry changed')
    writer_type = authorized_store(saved) if replay else version_store.Store
    with access_store.open_bound(binding, writer_type) as writer:
        current = writer.head['state']['policy']
        value = saved if replay else prepare(binding, intent, current, rows)
        if value is None:
            # Existing unregistered deployment behavior may read the version
            # journal in profile_manager; no cross-cycle transition is needed.
            writer.close()
            yield None
            return
        transfer = value['transfer']
        ledger.require(current in (transfer['before'], transfer['candidate']),
                       'root transfer was superseded')
        save(binding, rows, value)
        checkpoints.write(state, marker(binding, rows[transfer['before']['registry']['cycle_id']]),
                          transfer['before'])
        context = profile_context(value, rows, read_previous)
        yield context
        # Profile migration succeeded before this change can be made durable.
        writer.contract = authorized_store(value).contract
        if current == transfer['before']:
            writer.transact(policy.request_id(transfer), current['revision'], transfer['candidate'])
        checkpoints.write(state, marker(binding, rows[transfer['candidate']['registry']['cycle_id']]),
                          transfer['candidate'])
        ledger.require(writer.head['state']['policy'] == transfer['candidate'],
                       'root transfer commit differs')
