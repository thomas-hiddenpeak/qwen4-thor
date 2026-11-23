"""Bounded last-state checkpoints owned by STOPPED deployment transactions.

Each cycle has a permanent identity marker and one replaceable snapshot. Loss
of the snapshot never permits fallback to an older registration seed. These
files do not grant a role launch and must move with retired registrations.
"""
import copy
import os
from pathlib import Path

from tools.serve_host import native
from tools.serve_recovery import ledger, publication_files as files, store
from tools.serve_recovery import version_policy, version_registration

DIRECTORY = 'version-checkpoints'
RESERVE_DIRECTORY = 'version-checkpoints-reserve'
SUFFIXES = ('.bound', '.json', '.bound.new', '.json.new')
FORMAT = 'q4t-version-checkpoint-v1'
MAX_BYTES = 24*1024


def directory(state, *, create=False, reserve=False):
    path = Path(state)/(RESERVE_DIRECTORY if reserve else DIRECTORY)
    if not os.path.lexists(path) and not create:
        return None
    files.directory(path, create=create)
    ledger.require(path.stat().st_mode & 0o777 == 0o700,
                   'private version checkpoints required')
    if create: files.sync(path.parent)
    names = list(path.iterdir())
    limit = version_registration.capacity(reserve)
    ledger.require(len(names) <= limit*4,
                   'version checkpoint file bound')
    for name in names:
        cycle, suffix = name.name[:36], name.name[36:]
        ledger.require(ledger.identifier(cycle) and suffix in SUFFIXES,
            'unexpected version checkpoint file')
    ledger.require(len({name.name[:36] for name in names}) <= limit,
                   'version checkpoint cycle bound')
    return path


def cycle_directory(state, cycle, *, create=False):
    entries = version_registration.intents(state)
    ledger.require(cycle in entries and set(entries[cycle]) == {'committed'},
                   'checkpoint requires a committed registration')
    reserve = entries[cycle]['committed'].parent.name == version_registration.RESERVE_DIRECTORY
    other = directory(state, reserve=not reserve)
    ledger.require(other is None or not any(os.path.lexists(other/(cycle+s)) for s in SUFFIXES),
                   'checkpoint in wrong registration pool')
    return directory(state, create=create, reserve=reserve)


def identity(binding, registration_sha256, cycle):
    ledger.require(ledger.identifier(cycle) and
        type(registration_sha256) is str and ledger.HASH.fullmatch(registration_sha256),
        'invalid checkpoint identity')
    return dict(format=FORMAT, binding=binding, cycle_id=cycle,
                registration_sha256=registration_sha256)


def validate(value, marker):
    ledger.keys(value, ('identity', 'snapshot'))
    selected = version_policy.validate(value['snapshot'])
    ledger.require(value['identity'] == marker and
        selected['registry']['cycle_id'] == marker['cycle_id'] and
        len(ledger.canonical(value)) <= MAX_BYTES, 'checkpoint identity differs')
    return value


def read(state, marker):
    path = cycle_directory(state, marker['cycle_id'])
    if path is None: return None
    base = path/marker['cycle_id']
    names = [base.with_suffix(s) for s in SUFFIXES]
    if not any(os.path.lexists(p) for p in names): return None
    ledger.require(all(os.path.lexists(p) for p in names[:2]) and
        not any(os.path.lexists(p) for p in names[2:]),
        'checkpoint is missing or needs its original transaction')
    ledger.require(native.private_json(names[0]) == marker, 'checkpoint binding differs')
    return copy.deepcopy(validate(native.private_json(names[1], MAX_BYTES), marker)['snapshot'])


def extends(previous, following):
    version_policy.validate(previous); version_policy.validate(following)
    ledger.require(previous['registry'] == following['registry'] and
        previous['revision'] <= following['revision'], 'checkpoint revision or registry regressed')
    if previous['revision'] == following['revision']:
        ledger.require(previous == following, 'checkpoint revision collision')
    for name in ('switch', 'completed', 'terminal_fault'):
        ledger.require(previous[name] is None or previous[name] == following[name],
                       'checkpoint would refund protected recovery history')
    ledger.require(not previous['faults'] or previous['faults'] == following['faults'],
                   'checkpoint would erase observed faults')


def write(state, marker, selected):
    """Caller holds deployment/core/version writers and an exact saved intent."""
    value = validate(dict(identity=marker, snapshot=copy.deepcopy(selected)), marker)
    path = cycle_directory(state, marker['cycle_id'], create=True)
    cycles = {name.name[:36] for name in path.iterdir()}
    limit = version_registration.capacity(path.name == RESERVE_DIRECTORY)
    ledger.require(marker['cycle_id'] in cycles or len(cycles) < limit,
                   'version checkpoint capacity reached')
    base = path/marker['cycle_id']
    bound, target = base.with_suffix('.bound'), base.with_suffix('.json')
    pending = target.with_name(target.name+'.new')
    if os.path.lexists(target):
        ledger.require(native.private_json(bound) == marker, 'checkpoint binding differs')
        previous = validate(native.private_json(target, MAX_BYTES), marker)['snapshot']
        extends(previous, selected)
    # A partial marker can only be completed by this exact original intent.
    files.immutable_json(bound, marker)
    raw = ledger.canonical(value)
    fd = os.open(pending, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = store.check_file(fd)
        ledger.require(info.st_size <= len(raw) and raw.startswith(os.pread(fd, len(raw)+1, 0)),
                       'pending checkpoint belongs to another transaction')
        os.ftruncate(fd, 0); store.Store.write_all(fd, raw); os.fsync(fd)
    finally: os.close(fd)
    os.replace(pending, target); files.sync(path)
    ledger.require(read(state, marker) == selected, 'checkpoint commit differs')
    return value
