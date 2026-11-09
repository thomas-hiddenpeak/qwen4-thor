"""Rebind retired run evidence across old formats/boots without model actions.

Locks are observed together only for a few nonblocking kernel operations. All
filesystem reading, hashing, and writing occurs before or after releasing them.
The unchanged STOPPED ledger brackets that observation. Native launch always
allocates a new UUID, so a proven retired run cannot be reactivated by recovery.
"""
import fcntl
import os
from pathlib import Path
import stat
import time

from tools.serve_host import native
from tools.serve_recovery import bundle, ledger, retention as r, service, store

FORMAT = 'q4t.service.retention-epoch.v1'


def stopped_snapshot(state, service_id):
    envelope = native.private_json(Path(state) / 'ledger.json')
    store.envelope_keys(envelope)
    value = envelope['state']
    ledger.require(envelope['sha256'] == ledger.digest(value), 'retention ledger checksum')
    ledger.validate(value, service_id)
    ledger.require(value['desired'] == 'STOPPED' and not any(value['active'].values()),
                   'epoch migration needs durable STOPPED with no active grants')
    ledger.require(not os.path.lexists(Path(state) / 'ledger.pending'), 'ambiguous stop commit')
    ledger.require(not os.path.lexists(Path(state) / store.CONTRACT_PENDING), 'ambiguous service contract')
    marker = native.private_json(Path(state) / 'writer.lock')
    ledger.require(marker == dict(version=1, service_id=service_id), 'writer service identity changed')
    return dict(ledger=envelope, marker=marker)


def prove_stopped(state):
    state, state_inode, desc = r.state_descriptor(state)
    manifest = bundle.verify(desc['package'], desc['manifest_sha256'])
    service.descriptor(state, desc['package'], desc['manifest_sha256'])
    before = stopped_snapshot(state, desc['service_id'])
    boot = native.stamp()['boot_id']
    paths = (Path(manifest['lock_path']), state / 'guardian.lock', state / 'writer.lock')
    fds, held, identities = [], [], []
    try:
        # Open and inspect every file before holding any core ownership lock.
        for path in paths:
            fd = os.open(path, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
            fds.append(fd)
            info = os.fstat(fd)
            ledger.require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                and info.st_nlink == 1 and r.native.inode(path) == [info.st_dev, info.st_ino],
                'retention ownership lock changed')
            identities.append([info.st_dev, info.st_ino])
        try:
            for fd in fds:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held.append(fd)
            # No filesystem/control/metric work is allowed in this interval.
            proof_ns = time.clock_gettime_ns(time.CLOCK_BOOTTIME)
        finally:
            for fd in reversed(held):
                fcntl.flock(fd, fcntl.LOCK_UN)
            held.clear()
    finally:
        for fd in fds:
            os.close(fd)
    after = stopped_snapshot(state, desc['service_id'])
    ledger.require(before == after and r.native.inode(state) == state_inode and
        r.state_descriptor(state)[2] == desc and native.stamp()['boot_id'] == boot and
        [r.native.inode(p) for p in paths] == identities,
        'ownership or durable state changed during epoch proof')
    return dict(kind='stopped_native_ownership_boundary', boot_id=boot,
        cutoff_start_ticks=proof_ns * os.sysconf('SC_CLK_TCK') // 1_000_000_000,
        proof_boottime_ns=proof_ns, service_id=desc['service_id'], state_identity=state_inode,
        ledger_sha256=ledger.digest(before['ledger']), lock_identities=identities,
        model_signalled=False, gpu_probe_started=False)


def migrate(state, run_id):
    ledger.require(ledger.identifier(run_id), 'canonical run UUID required')
    state, state_inode, desc = r.state_descriptor(state)
    with r.claim(state) as catalog:
        source = state / 'runs' / run_id
        trash = state / 'runs' / ('.retired-' + run_id)
        ledger.require(not (os.path.lexists(source) and os.path.lexists(trash)), 'ambiguous migration source')
        if os.path.lexists(source):
            source = r.run_directory(state, run_id)
            original = r.snapshot(source)
            remaining = original
        else:
            receipt, archived = r.verified_archive(state, run_id)
            intent = native.private_json(catalog / (run_id + '.retire.json'), r.MAX_MANIFEST)
            ledger.require(intent.get('service_id') == desc['service_id'] and intent.get('run_id') == run_id
                and intent.get('state') in ('PREPARED', 'RENAMED') and
                intent.get('archive_receipt') == receipt, 'no pending retirement to migrate')
            original = archived['original']
            remaining = r.snapshot(trash) if os.path.lexists(trash) else None
            if remaining is not None:
                ledger.require(remaining['root_identity'] == original['root_identity'] and
                    remaining['entries'].keys() <= original['entries'].keys() and
                    all(v == original['entries'][n] for n, v in remaining['entries'].items()),
                    'retirement residue changed before migration')
            source = trash
        proof = prove_stopped(state)
        # A new launch may begin after the proof, but must create a new UUID.
        # It cannot open this old run through the native service launch path.
        current = r.snapshot(source) if os.path.lexists(source) else None
        ledger.require(current == remaining and native.inode(state) == state_inode,
                       'run changed around ownership proof')
        value = dict(format=FORMAT, service_id=desc['service_id'], state_identity=state_inode,
            run_id=run_id, source_identity=original['root_identity'], source_sha256=ledger.digest(original),
            proof=proof)
        r.save_record(catalog, run_id + '.epoch.json', value)
        return dict(migrated=True, run_id=run_id, boot_id=proof['boot_id'],
            proof_kind=proof['kind'], model_signalled=False, gpu_probe_started=False)


def read(state, run_id, *, original=None):
    state, state_inode, desc = r.state_descriptor(state)
    value = native.private_json(state / 'retention' / (run_id + '.epoch.json'), r.MAX_MANIFEST)
    ledger.keys(value, ('format', 'service_id', 'state_identity', 'run_id',
                       'source_identity', 'source_sha256', 'proof'))
    proof = value['proof']
    ledger.keys(proof, ('kind', 'boot_id', 'cutoff_start_ticks', 'proof_boottime_ns',
        'service_id', 'state_identity', 'ledger_sha256', 'lock_identities',
        'model_signalled', 'gpu_probe_started'))
    ledger.require(ledger.integer(proof['proof_boottime_ns'], minimum=1) and
        proof['proof_boottime_ns'] <= time.clock_gettime_ns(time.CLOCK_BOOTTIME) and
        proof['cutoff_start_ticks'] == proof['proof_boottime_ns'] * os.sysconf('SC_CLK_TCK') // 1_000_000_000 and
        proof['model_signalled'] is False and proof['gpu_probe_started'] is False and
        ledger.HASH.fullmatch(proof['ledger_sha256']) and ledger.HASH.fullmatch(value['source_sha256']),
        'epoch proof shape changed')
    ledger.require(value['format'] == FORMAT and value['service_id'] == desc['service_id'] and
        value['state_identity'] == state_inode and value['run_id'] == run_id and
        proof['kind'] == 'stopped_native_ownership_boundary' and
        proof['boot_id'] == native.stamp()['boot_id'] and proof['service_id'] == desc['service_id'] and
        proof['state_identity'] == state_inode and ledger.integer(proof['cutoff_start_ticks'], minimum=1),
        'epoch proof identity or boot changed')
    if original is not None:
        ledger.require(value['source_identity'] == original['root_identity'] and
            value['source_sha256'] == ledger.digest(original), 'epoch proof source changed')
    return dict(boot_id=proof['boot_id'], cutoff_start_ticks=proof['cutoff_start_ticks'])
