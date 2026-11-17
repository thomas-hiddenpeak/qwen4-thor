"""Sealed, explicitly selected offline retention through the service CLI.

Queries describe candidates, not permission to move them. The original archive
transactions recheck every live dependency, identity and capacity under locks.
"""
import os
from pathlib import Path

from tools.serve_host import native
from tools.serve_recovery import access_store, bundle, ledger, service
from tools.serve_recovery import publication_archive as generations
from tools.serve_recovery import publication_intent_archive as intents
from tools.serve_recovery import version_archive as versions
from tools.serve_service import package as engine

KINDS = {'intents': intents, 'generations': generations, 'versions': versions}


def authority(state, package, digest):
    """Capture the caller's selected identity, then recheck it while locked."""
    state = Path(state).absolute()
    bundle.verify(package, digest)
    expected = service.descriptor(state, package, digest)
    identity = native.inode(state)

    def check(locked_state, desc):
        ledger.require(locked_state == state and native.inode(state) == identity
            and desc == expected and
            service.descriptor(state, package, digest) == expected,
            'maintenance service identity or deployment changed')
    return check


def pool(ids, keep, capacity, *, initialized=True):
    ids = set(ids)
    ledger.require(len(ids) <= capacity, 'maintenance pool capacity exceeded')
    return dict(capacity=capacity, used=len(ids), available=capacity-len(ids),
        protected_ids=sorted(ids & keep), candidate_ids=sorted(ids-keep),
        candidates_require_plan=True, initialized=initialized)


def status(state, package, digest):
    check = authority(state, package, digest)
    with generations.locked(state, authority=check) as (state, desc, _, current):
        records = generations.intents(state, desc)
        publication = pool([r['request_id'] for r in records],
            intents.protected(current, records), intents.publication.MAX_INTENTS)
        directory = generations.files.directory(
            Path(current['root'])/generations.files.DIRECTORY/'generations')
        names = list(directory.iterdir())
        ledger.require(len(names) <= generations.files.MAX_GENERATIONS,
                       'maintenance generation count bound')
        for path in names:
            ledger.require(ledger.HASH.fullmatch(path.name),
                           'unknown generation requires resolution')
            generations.files.directory(path)
        copies = pool([p.name for p in names],
            generations.protected(current, records), generations.files.MAX_GENERATIONS)
        registration = versions.registration
        registered = pool([], set(), registration.MAX_CYCLES, initialized=False)
        if registration.present(state):
            binding = access_store.binding(state, desc['service_id'])
            with versions.version_store.open_bound(binding) as writer:
                selected = writer.head['state']['policy']
                registration.for_state(state, desc, selected)
                entries = versions.entries(state, binding, selected)
                registered = pool(entries, versions.protected(
                    state, desc, current, selected, entries), registration.MAX_CYCLES)
        return dict(format='q4t-service-maintenance-v1', state=str(state),
            service_id=desc['service_id'], package=desc['package'],
            manifest_sha256=desc['manifest_sha256'],
            publication_revision=current['revision'], desired='STOPPED',
            pools=dict(intents=publication, generations=copies, versions=registered),
            model_actions=False, deleted_files=0)


def prepare(state, package, digest, kind, output, selected):
    ledger.require(kind in KINDS, 'unknown maintenance pool')
    return KINDS[kind].prepare(state, output, selected,
        authority=authority(state, package, digest))


def apply(state, package, digest, kind, plan, expected, *, restore=False):
    ledger.require(kind in KINDS, 'unknown maintenance pool')
    check = authority(state, package, digest)
    ledger.require(engine.sha(plan) == expected, 'reviewed archive plan changed')
    value = native.private_json(plan)
    ledger.require(value.get('state') == str(Path(state).absolute()) and
        value.get('format') == KINDS[kind].FORMAT,
        'maintenance plan service or pool differs')
    return KINDS[kind].apply(plan, expected, restore=restore, authority=check)


def execute(args, package, digest):
    os.umask(0o077)
    if args.operation == 'maintenance-status':
        return status(args.state, package, digest)
    if args.operation == 'maintenance-plan':
        return prepare(args.state, package, digest, args.kind, args.output, args.select)
    ledger.require(args.operation in ('maintenance-apply', 'maintenance-restore'),
                   'unknown maintenance operation')
    return apply(args.state, package, digest, args.kind, args.plan, args.plan_sha256,
                 restore=args.operation == 'maintenance-restore')
