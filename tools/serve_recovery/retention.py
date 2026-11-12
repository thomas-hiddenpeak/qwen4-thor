"""Private immutable run archives and explicit retirement; never controls a model.

Only UUID run directories created by this service are candidates. An intact
external archive is required even for a successful run, so an unarchived fault
cannot be discarded by age/count/bytes. State/ledger/packages are not candidates.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import itertools
import os
from pathlib import Path
import stat
import time
import uuid

from tools.serve_host import native
from tools.serve_ops.identity import bounded_file
from tools.serve_recovery import journal, ledger, store

MAX_FILES = 4096
MAX_BYTES = 256 * 1024**2
MAX_RUNS = 1024
MAX_PROCESSES = 65536
MAX_MANIFEST = 4 * 1024**2
FORMAT = 'q4t.service.run-archive.v1'


def private_directory(path):
    path = Path(path).absolute()
    ledger.require(path.resolve(strict=True) == path, 'noncanonical retention directory')
    info = path.lstat()
    ledger.require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
        and stat.S_IMODE(info.st_mode) == 0o700, 'private retention directory required')
    return path, [info.st_dev, info.st_ino]


def file_identity(info):
    return dict(device=info.st_dev, inode=info.st_ino, bytes=info.st_size,
        allocated_bytes=info.st_blocks * 512, mode=stat.S_IMODE(info.st_mode),
        modified_ns=info.st_mtime_ns, changed_ns=info.st_ctime_ns)


def hash_fd(fd, expected):
    digest, count = hashlib.sha256(), 0
    while chunk := os.read(fd, min(65536, expected + 1 - count)):
        count += len(chunk)
        ledger.require(count <= expected, 'retention source grew')
        digest.update(chunk)
    ledger.require(count == expected, 'retention source truncated')
    return digest.hexdigest()


def snapshot(path):
    path, root_identity = private_directory(path)
    entries, total, allocated = {}, 0, 0
    def walk(directory, prefix='', depth=0, parent_fd=None):
        nonlocal total, allocated
        ledger.require(depth <= 8, 'retention depth bound')
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
        try:
            with os.scandir(fd) as scan:
                children = sorted(itertools.islice(scan, MAX_FILES + 1), key=lambda e: e.name)
            ledger.require(len(children) <= MAX_FILES, 'retention entry bound')
            for child in children:
                ledger.require(len(entries) < MAX_FILES and len(child.name) <= 255,
                               'retention entry bound')
                name = prefix + child.name
                info = os.stat(child.name, dir_fd=fd, follow_symlinks=False)
                ledger.require(info.st_uid == os.geteuid() and not info.st_mode & 0o077,
                               'private retention object required')
                if stat.S_ISDIR(info.st_mode):
                    ledger.require(stat.S_IMODE(info.st_mode) == 0o700, 'private nested directory')
                    entries[name] = dict(kind='directory', identity=[info.st_dev, info.st_ino])
                    walk(child.name, name + '/', depth + 1, fd)
                else:
                    ledger.require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
                                   'retention refuses links or special files')
                    total += info.st_size; allocated += info.st_blocks * 512
                    ledger.require(total <= MAX_BYTES, 'retention byte bound')
                    source = os.open(child.name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW |
                                     os.O_NONBLOCK, dir_fd=fd)
                    try:
                        before = file_identity(os.fstat(source))
                        ledger.require(before == file_identity(info), 'retention file replaced')
                        digest = hash_fd(source, info.st_size)
                        ledger.require(file_identity(os.fstat(source)) == before,
                                       'retention file changed while reading')
                        entries[name] = dict(kind='file', sha256=digest, **before)
                    finally:
                        os.close(source)
        finally:
            os.close(fd)
    walk(path)
    ledger.require(native.inode(path) == root_identity, 'retention root replaced')
    return dict(root_identity=root_identity, entries=entries,
                bytes=total, allocated_bytes=allocated)


def open_relative(root, name, flags):
    parts = Path(name).parts
    ledger.require(parts and not Path(name).is_absolute() and
        all(p not in ('.', '..') for p in parts), 'relative archive name required')
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW |
                              os.O_CLOEXEC, dir_fd=fd)
            os.close(fd); fd = next_fd
        return os.open(parts[-1], flags | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=fd)
    finally:
        os.close(fd)


def state_descriptor(state):
    state, identity = private_directory(state)
    desc = native.private_json(state / 'service.json')
    from tools.serve_recovery import deployment_policy
    deployment_policy.descriptor(desc)
    return state, identity, desc


def repository(state):
    state, _, desc = state_descriptor(state)
    manifest_path = Path(desc['package']) / 'manifest.json'
    manifest = native.private_json(manifest_path, MAX_MANIFEST)
    raw = bounded_file(manifest_path, MAX_MANIFEST)
    ledger.require(hashlib.sha256(raw).hexdigest() == desc['manifest_sha256'] and
        ledger.digest(manifest) == ledger.digest(store.decode(raw)), 'deployment manifest changed')
    root = Path(manifest['repository'])
    ledger.require(root.is_absolute() and root.resolve(strict=True) == root and
        any(state.is_relative_to(root / name) for name in ('build', '.q4t-work')),
        'retention outside deployment repository')
    return root


def run_directory(state, run_id):
    ledger.require(ledger.identifier(run_id), 'canonical run UUID required')
    state, _, desc = state_descriptor(state)
    root, _ = private_directory(state / 'runs')
    path, _ = private_directory(root / run_id)
    cfg = native.private_json(path / 'controller.json')
    guardian = native.private_json(path / 'guardian.json')
    ledger.require(cfg.get('version') in (2, 3, 4, 5) and cfg.get('directory') == str(path) and
        cfg.get('state_directory') == str(state) and type(cfg.get('package')) is str and
        ledger.HASH.fullmatch(cfg.get('manifest_sha256', '')) and
        guardian.get('directory') == str(state) and guardian.get('service_id') == desc['service_id'],
        'run is not bound to this service')
    if cfg['version'] >= 4:
        from tools.serve_recovery import access_store
        expected = access_store.binding(state, desc['service_id'])
        ledger.require(cfg.get('limits') == expected and
            (cfg['version'] < 5 or cfg.get('profiles') == expected),
            'run managed state binding differs')
    ledger.require(guardian.get('controller', [])[-2:] == ['--config', str(path / 'controller.json')]
        and guardian.get('gateway', [])[-2:] == ['--config', str(path / 'gateway.json')],
        'run launch identity mismatch')
    if guardian.get('version') == 3:
        from tools.serve_recovery import access_store, version_policy
        versions=guardian['version_recovery'];registry=version_policy.registry(versions['registry'])
        ledger.require(registry['root_manifest_sha256']==cfg['manifest_sha256'] and
            set(versions['payloads'])==set(version_policy.PAYLOADS),'version run root differs')
        expected=access_store.binding(state,desc['service_id'])
        for name,payload in versions['payloads'].items():
            directory=path/name
            nested=native.private_json(directory/'controller.json')
            ledger.require(payload['controller'][-2:]==['--config',str(directory/'controller.json')] and
                payload['gateway'][-2:]==['--config',str(directory/'gateway.json')] and
                nested.get('version')==5 and nested.get('directory')==str(directory) and
                nested.get('state_directory')==str(state) and nested.get('limits')==expected and
                nested.get('profiles')==expected and nested.get('manifest_sha256')==
                    registry['payloads'][name]['manifest_sha256'],'version run payload binding differs')
    return path


def references(root, *, retired_context=None):
    """Conservative current-UID process references, no signal or lifecycle action.

    Native launch always allocates a new UUID; it cannot reactivate a sealed
    old directory. A live launch/controller/file worker holding an old path
    makes it ineligible. An unreadable possible post-launch actor makes safety unknown.
    Protected pre-launch session helpers cannot be native descendants; their
    readable argv references are still retained.
    """
    root = Path(root)
    with os.scandir(root) as scan:
        runs = list(itertools.islice(scan, MAX_RUNS + 1))
    ledger.require(len(runs) <= MAX_RUNS, 'run directory bound')
    if not runs:
        return set()
    contexts = [] if retired_context is None else [retired_context]
    recognized = 0
    for entry in runs:
        if not ledger.identifier(entry.name):
            continue
        try:
            path = run_directory(root.parent, entry.name)
            recognized += 1
            context = run_context(path)
            if context is not None:
                contexts.append(dict(boot_id=context['boot_id'],
                    cutoff_start_ticks=context['parent_start_ticks']))
            else:
                from tools.serve_recovery import retention_epoch
                try:
                    contexts.append(retention_epoch.read(root.parent, entry.name))
                except (OSError, ValueError, KeyError, TypeError):
                    pass
        except (OSError, ValueError, KeyError, TypeError):
            pass  # Unknown layouts are protected, never deletion candidates.
    if not recognized and retired_context is None:
        return set()
    boot = native.stamp()['boot_id']
    starts = [value['cutoff_start_ticks'] for value in contexts
        if type(value) is dict and value.get('boot_id') == boot and
        ledger.integer(value.get('cutoff_start_ticks'), minimum=1)]
    earliest = min(starts) if starts else None
    found = set()
    with os.scandir('/proc') as scan:
        entries = list(itertools.islice(scan, MAX_PROCESSES + 1))
    ledger.require(len(entries) <= MAX_PROCESSES, 'process inventory bound')
    processes = [e.name for e in entries if e.name.isdecimal()]
    def capture(raw):
        for part in raw.split(b'\0'):
            try:
                path = Path(os.fsdecode(part))
                if path.is_absolute() and path.is_relative_to(root):
                    relative = path.relative_to(root)
                    if relative.parts:
                        found.add(relative.parts[0])
            except (ValueError, UnicodeError):
                continue
    for name in processes:
        if int(name) == os.getpid():
            continue
        process = Path('/proc') / name
        born = None
        try:
            if process.stat().st_uid != os.geteuid():
                continue
            born = journal.process_start(int(name))
            capture(bounded_file(process / 'cmdline', 65536))
            for link in ('cwd', 'exe', 'root'):
                try:
                    capture(os.fsencode(os.readlink(process / link)))
                except FileNotFoundError:
                    pass
            with os.scandir(process / 'fd') as scan:
                descriptors = list(itertools.islice(scan, MAX_FILES + 1))
            ledger.require(len(descriptors) <= MAX_FILES, 'process descriptor bound')
            for descriptor in descriptors:
                try:
                    target = os.readlink(process / 'fd' / descriptor.name)
                    capture(os.fsencode(target.removesuffix(' (deleted)')))
                except FileNotFoundError:
                    pass
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            # A pre-existing protected user-session helper cannot be a child
            # of these later launchers. Keep any readable argv reference, but
            # do not make (sd-pam) a mandatory dependency of run retention.
            # Every potentially new opaque actor still makes safety unknown.
            if earliest is None or born is None or born >= earliest:
                raise
    return found


def dependency_json(path, expected=None):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        ledger.require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and
            info.st_nlink == 1 and info.st_size <= MAX_MANIFEST,
            'owned release metadata required')
        with os.fdopen(os.dup(fd), 'rb') as stream:
            raw = stream.read(MAX_MANIFEST + 1)
        ledger.require(len(raw) <= MAX_MANIFEST, 'release metadata bound')
        actual = hashlib.sha256(raw).hexdigest()
        ledger.require(expected is None or expected == actual, 'release dependency digest changed')
        # Historical lineage records may be group writable. They are read-only
        # protection inputs, never deletion grants or executable acceptance.
        # Parent receipts remain digest checked; every observed metadata digest
        # is also frozen into the plan and revalidated before retirement.
        ledger.require(file_identity(os.fstat(fd)) == file_identity(info),
                       'release metadata changed during read')
        return store.decode(raw), actual
    finally:
        os.close(fd)


def rollback_dependencies(path, value):
    paths, metadata = set(), []
    if 'files' in value:
        files = value['files']
        ledger.require(type(files) is dict and files and all(type(n) is str and
            Path(n).name == n and n not in ('.', '..') and type(h) is str and
            ledger.HASH.fullmatch(h) for n, h in files.items()), 'rollback file inventory changed')
        unchanged = value.get('engine_unchanged')
        unchanged = unchanged is True or (type(unchanged) is dict and
            set(unchanged) == {'binary_sha256', 'release_sha256'} and
            all(type(h) is str and ledger.HASH.fullmatch(h) for h in unchanged.values()))
        required = {'service.release.json'} if unchanged else {'q4t.release.json', 'service.release.json'}
        ledger.require(required <= files.keys(), 'rollback metadata digest absent')
        paths.update(path.parent / n for n in files)
        metadata.extend((path.parent / n, h, False) for n, h in files.items()
                        if n in ('q4t.release.json', 'service.release.json', 'service.json'))
    elif 'metadata_sha256' in value and 'descriptor_sha256' in value:
        # The observation-only v3 delivery retained the complete previous
        # package and these two service records; engine bytes did not change.
        for name, key in (('service.release.json', 'metadata_sha256'),
                          ('service.json', 'descriptor_sha256')):
            digest = value[key]
            ledger.require(type(digest) is str and ledger.HASH.fullmatch(digest), 'legacy rollback digest')
            paths.add(path.parent / name); metadata.append((path.parent / name, digest, False))
    elif 'preserved' in value:
        rows = value['preserved']
        ledger.require(type(rows) is list and 1 <= len(rows) <= 32, 'legacy rollback inventory')
        for row in rows:
            ledger.keys(row, ('path', 'rollback', 'sha256'))
            ledger.require(type(row['path']) is str and Path(row['path']).is_absolute() and
                type(row['rollback']) is str and Path(row['rollback']).is_absolute() and
                type(row['sha256']) is str and ledger.HASH.fullmatch(row['sha256']),
                'legacy rollback record')
            source, target = Path(row['path']), Path(row['rollback'])
            paths.update((source, target))
            if source.name in ('q4t.release.json', 'service.release.json', 'service.json'):
                metadata.append((target, row['sha256'], False))
    else:
        raise ledger.LedgerError('unknown rollback receipt inventory')
    return paths, metadata


def pinned_digest(value, target):
    pending, found, count = [value], set(), 0
    while pending:
        node = pending.pop(); count += 1
        ledger.require(count <= 100000, 'release metadata traversal bound')
        if type(node) is dict:
            if node.get('path') == str(target) and 'sha256' in node:
                digest = node['sha256']
                ledger.require(type(digest) is str and ledger.HASH.fullmatch(digest),
                               'release source digest malformed')
                found.add(digest)
            pending.extend(v for v in node.values() if type(v) in (dict, list))
        elif type(node) is list:
            pending.extend(v for v in node if type(v) in (dict, list))
    ledger.require(len(found) <= 1, 'ambiguous release source digest')
    return next(iter(found), None)


def dependencies(state, *, anchors=None, records=None):
    """Keep every local release/rollback dependency, including previous releases."""
    root = repository(state)
    queue = [(Path(p), None, False) for p in (anchors if anchors is not None else (
        root / 'build/q4t.release.json', root / 'build/service.release.json',
        Path(state) / 'service.json'))]
    from tools.serve_recovery import deployment
    desc = state_descriptor(state)[2]
    deployed = Path(state)/deployment.Store.marker_name
    if desc['version'] == 2 or os.path.lexists(deployed):
        queue.extend((p, None, False) for p in (deployed,
            Path(state)/deployment.Store.directory_name/'policy.json'))
    seen, paths = {}, set()
    follow = {'previous_release', 'previous_service_release', 'rollback_receipt'}
    keep = follow | {'package', 'previous_binary', 'build_cache',
        'tested_source_manifest', 'acceptance_decision', 'acceptance_record',
        'previous_acceptance_record', 'control_review', 'previous_service_entry',
        'previous_service_metadata', 'previous_state'}
    while queue:
        path, expected, rollback = queue.pop()
        path = path.absolute()
        if path in seen:
            ledger.require(expected is None or expected == seen[path], 'conflicting release dependency')
            continue
        ledger.require(len(seen) < 256, 'release dependency bound')
        value, digest = dependency_json(path, expected)
        seen[path] = digest; paths.add(path)
        if value.get('format') == deployment.Store.format:
            deployment.Store.validate_state(value['state'])
            ledger.require(value['sha256'] == ledger.digest(value['state']) and
                value['service_id'] == desc['service_id'] and
                value['state_identity'] == native.inode(state), 'deployment retention identity')
            selected = value['state']['policy']
            for item in [selected['current'], selected['target'], *selected['history']]:
                if item is not None:
                    package = Path(item['package']);paths.add(package)
                    queue.append((package/'manifest.json', item['manifest_sha256'], False))
        inherited = value.get('inherited_release')
        for key in keep:
            raw = value.get(key)
            if type(raw) is str and Path(raw).is_absolute():
                target = Path(raw); paths.add(target)
                if key in follow:
                    checksum = value.get('rollback_sha256' if key == 'rollback_receipt' else key + '_sha256')
                    if key == 'previous_release' and type(inherited) is dict and inherited.get('path') == raw:
                        checksum = inherited.get('sha256')
                    queue.append((target, checksum or pinned_digest(value, target), key == 'rollback_receipt'))
        if type(inherited) is dict and type(inherited.get('path')) is str:
            target = Path(inherited['path'])
            ledger.require(target.is_absolute(), 'release dependency path')
            paths.add(target); queue.append((target, inherited.get('sha256') or pinned_digest(value, target), False))
        if rollback:
            preserved, metadata = rollback_dependencies(path, value)
            paths.update(preserved); queue.extend(metadata)
        for key in ('release_declarations', 'third_party_notices', 'application_notices'):
            block = value.get(key)
            if type(block) is dict:
                for field in ('directory', 'source_directory'):
                    if type(block.get(field)) is str:
                        paths.add(Path(block[field]))
    if records is not None:
        records.update({str(p): digest for p, digest in seen.items()})
    return paths | {p.resolve() for p in paths}


@contextmanager
def claim(state):
    from tools.serve_recovery import deployment
    with deployment.claim(state, shared=True):
        with catalog_claim(state) as catalog:yield catalog


@contextmanager
def catalog_claim(state):
    state, _, _ = state_descriptor(state)
    directory = state / 'retention'
    directory.mkdir(mode=0o700, exist_ok=True)
    private_directory(directory)
    fd = os.open(directory / 'operator.lock', os.O_CREAT | os.O_RDWR |
                 os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        ledger.require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1,
            'retention operator lock identity')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield directory
    finally:
        os.close(fd)


def write_new(path, data):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data); stream.flush(); os.fsync(stream.fileno())


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def run_context(path):
    value = native.private_json(Path(path) / 'controller.json').get('retention_context')
    if (type(value) is not dict or set(value) != {'boot_id', 'parent_pid', 'parent_start_ticks'} or
            not ledger.identifier(value.get('boot_id')) or
            not ledger.integer(value.get('parent_pid'), minimum=2) or
            not ledger.integer(value.get('parent_start_ticks'), minimum=1) or
            value['boot_id'] != native.stamp()['boot_id']):
        return None
    return value


def effective_context(state, run_id, path, *, original=None):
    context = run_context(path)
    if context is not None:
        return dict(boot_id=context['boot_id'], cutoff_start_ticks=context['parent_start_ticks'])
    from tools.serve_recovery import retention_epoch
    try:
        return retention_epoch.read(state, run_id,
            original=snapshot(path) if original is None else original)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def eligible(state, run_id, *, pins=None, active=None):
    path = run_directory(state, run_id)
    ledger.require(effective_context(state, run_id, path) is not None,
                   'run owner epoch needs verified migration')
    active = references(Path(state) / 'runs') if active is None else active
    ledger.require(run_id not in active, 'run is active')
    pins = dependencies(state) if pins is None else pins
    ledger.require(not any(p == path or p.is_relative_to(path) for p in pins),
                   'run retains a release or rollback dependency')
    return path


def archive(state, run_id, destination, *, pins=None):
    """Copy an inactive run before granting retirement eligibility; never overwrite."""
    state, state_inode, desc = state_descriptor(state)
    repository_root = repository(state)
    destination = Path(destination).absolute()
    ledger.require(destination.resolve() == destination and
        any(destination.is_relative_to(repository_root / n) for n in ('build', '.q4t-work')) and
        not destination.is_relative_to(state), 'archive must be outside service state in work/build')
    private_directory(destination.parent)
    with claim(state) as catalog:
        path = eligible(state, run_id, pins=pins)
        original = snapshot(path)
        destination.mkdir(mode=0o700)
        tree = destination / 'files'; tree.mkdir(mode=0o700)
        for name, row in original['entries'].items():
            target = tree / name
            if row['kind'] == 'directory':
                target.mkdir(mode=0o700)
                continue
            source = open_relative(path, name, os.O_RDONLY)
            try:
                ledger.require(file_identity(os.fstat(source)) == {k: row[k] for k in
                    ('device', 'inode', 'bytes', 'allocated_bytes', 'mode', 'modified_ns', 'changed_ns')},
                    'archive source changed')
                output = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                digest, count = hashlib.sha256(), 0
                with os.fdopen(output, 'wb') as stream:
                    while chunk := os.read(source, min(65536, row['bytes'] + 1 - count)):
                        count += len(chunk)
                        ledger.require(count <= row['bytes'], 'archive source grew')
                        digest.update(chunk); stream.write(chunk)
                    stream.flush(); os.fsync(stream.fileno())
                ledger.require(count == row['bytes'] and digest.hexdigest() == row['sha256'],
                               'archive copy integrity')
            finally:
                os.close(source)
        # Detect writers/changes during the copy before making an eligibility receipt.
        eligible(state, run_id, pins=pins)
        ledger.require(snapshot(path) == original and native.inode(state) == state_inode,
                       'archive source or service changed')
        manifest = dict(format=FORMAT, service_id=desc['service_id'], state_identity=state_inode,
            run_id=run_id, created_unix=time.time(), original=original)
        raw = ledger.canonical(manifest)
        ledger.require(len(raw) <= MAX_MANIFEST, 'archive manifest bound')
        write_new(destination / 'manifest.json', raw)
        for name, row in reversed(list(original['entries'].items())):
            if row['kind'] == 'directory':
                sync_directory(tree / name)
        sync_directory(tree);sync_directory(destination);sync_directory(destination.parent)
        receipt = dict(format=FORMAT, run_id=run_id, service_id=desc['service_id'],
            archive=str(destination), archive_manifest_sha256=hashlib.sha256(raw).hexdigest())
        temporary = catalog / ('.receipt-' + str(uuid.uuid4()))
        write_new(temporary, ledger.canonical(receipt))
        os.replace(temporary, catalog / (run_id + '.json'))
        sync_directory(catalog)
        return receipt


def verified_archive(state, run_id):
    ledger.require(ledger.identifier(run_id), 'canonical run UUID required')
    state, state_inode, desc = state_descriptor(state)
    receipt = native.private_json(state / 'retention' / (run_id + '.json'))
    ledger.keys(receipt, ('format', 'run_id', 'service_id', 'archive', 'archive_manifest_sha256'))
    ledger.require(receipt['format'] == FORMAT and receipt['run_id'] == run_id and
        receipt['service_id'] == desc['service_id'], 'foreign archive receipt')
    archive_path, _ = private_directory(receipt['archive'])
    ledger.require(not archive_path.is_relative_to(state), 'archive inside deletion scope')
    manifest = native.private_json(archive_path / 'manifest.json', MAX_MANIFEST)
    ledger.require(hashlib.sha256(ledger.canonical(manifest)).hexdigest() ==
        receipt['archive_manifest_sha256'] and manifest['format'] == FORMAT and
        manifest['run_id'] == run_id and manifest['service_id'] == desc['service_id'] and
        manifest['state_identity'] == state_inode, 'archive manifest changed')
    copied = snapshot(archive_path / 'files')
    original = manifest['original']
    ledger.require(copied['entries'].keys() == original['entries'].keys(), 'archive inventory changed')
    for name, row in copied['entries'].items():
        old = original['entries'][name]
        ledger.require(row['kind'] == old['kind'] and (row['kind'] == 'directory' or
            (row['bytes'], row['sha256']) == (old['bytes'], old['sha256'])), 'archive contents changed')
    return receipt, manifest


def policy(max_age_days=14, max_runs=100, max_bytes=1024**3):
    ledger.require(type(max_age_days) is int and 1 <= max_age_days <= 3650 and
        type(max_runs) is int and 1 <= max_runs <= MAX_RUNS and
        type(max_bytes) is int and 1 <= max_bytes <= 2**40, 'retention policy bound')
    return dict(max_age_days=max_age_days, max_runs=max_runs, max_bytes=max_bytes)


def plan(state, *, limits=None, now=None, pins=None, active=None):
    state, state_inode, desc = state_descriptor(state)
    root, runs_inode = private_directory(state / 'runs')
    limits = policy(**(limits or {}))
    now = time.time() if now is None else now
    ledger.require(type(now) in (int, float) and 0 < now <= time.time() + 60,
                   'retention clock invalid')
    with os.scandir(root) as scan:
        entries = sorted(itertools.islice(scan, MAX_RUNS + 1), key=lambda e: e.name)
    ledger.require(len(entries) <= MAX_RUNS, 'run directory bound')
    active = references(root) if active is None else active
    dependency_sources = {}
    pins = dependencies(state, records=dependency_sources) if pins is None else pins
    protected, candidates, all_bytes_known = [], [], True
    for entry in entries:
        reason, observed = None, None
        try:
            path = run_directory(state, entry.name)
            observed = snapshot(path)
            if effective_context(state, entry.name, path, original=observed) is None:
                reason = 'owner_epoch_unverified'
            elif entry.name in active:
                reason = 'active'
            elif any(p == path or p.is_relative_to(path) for p in pins):
                reason = 'release_dependency'
            else:
                try:
                    receipt, archived = verified_archive(state, entry.name)
                    ledger.require(archived['original'] == observed, 'run changed since archive')
                except (OSError, ValueError, KeyError, TypeError):
                    reason = 'unarchived_or_archive_changed'
            if reason is None:
                created = observed['entries']['controller.json']['modified_ns'] / 1e9
                ledger.require(0 < created <= now + 60, 'run creation time invalid')
                candidates.append(dict(run_id=entry.name, source=observed,
                    receipt=receipt, created=created, allocated_bytes=observed['allocated_bytes']))
        except (OSError, ValueError, KeyError, TypeError):
            reason = 'unrecognized_or_unreadable'
        if reason is not None:
            size = None if observed is None else observed['allocated_bytes']
            if size is None:
                all_bytes_known = False
            protected.append(dict(name=entry.name, reason=reason, allocated_bytes=size))
    count = len(protected)
    total = sum(r['allocated_bytes'] or 0 for r in protected)
    retained, remove = [], []
    for row in sorted(candidates, key=lambda r: (r['created'], r['run_id']), reverse=True):
        if now - row['created'] > limits['max_age_days'] * 86400 or count >= limits['max_runs'] or total + row['allocated_bytes'] > limits['max_bytes']:
            remove.append(row)
        else:
            retained.append(row['run_id']); count += 1; total += row['allocated_bytes']
    result = dict(format='q4t.service.retention-plan.v1', service_id=desc['service_id'],
        state_identity=state_inode, runs_identity=runs_inode, at_unix=now,
        policy=limits, dependency_sources=dependency_sources,
        protected=protected, retain=retained, remove=remove,
        retained_known_allocated_bytes=total, retained_bytes_complete=all_bytes_known,
        protected_excess_runs=max(0, len(protected) - limits['max_runs']),
        protected_excess_known_bytes=max(0, sum(r['allocated_bytes'] or 0 for r in protected) - limits['max_bytes']),
        bytes_scope='allocated filesystem blocks in run files; archives/state/packages excluded',
        destructive=False)
    ledger.require(len(ledger.canonical(result)) <= MAX_MANIFEST, 'retention plan bound')
    return result


def save_record(catalog, name, value):
    temporary = catalog / ('.commit-' + str(uuid.uuid4()))
    write_new(temporary, ledger.canonical(value))
    os.replace(temporary, catalog / name)
    sync_directory(catalog)


def resume_locked(state, run_id, catalog, *, pins=None):
    """Resume only a durable retirement intent backed by the exact intact archive."""
    ledger.require(ledger.identifier(run_id), 'canonical run UUID required')
    record_path = catalog / (run_id + '.retire.json')
    record = native.private_json(record_path, MAX_MANIFEST)
    ledger.keys(record, ('format', 'service_id', 'run_id', 'archive_receipt', 'state', 'source_identity'))
    _, _, desc = state_descriptor(state)
    ledger.require(record['format'] == 'q4t.service.retirement.v1' and
        record['service_id'] == desc['service_id'] and record['run_id'] == run_id and
        record['state'] in ('PREPARED', 'RENAMED', 'RETIRED'), 'invalid retirement intent')
    receipt, manifest = verified_archive(state, run_id)
    ledger.require(record['archive_receipt'] == receipt and
        manifest['original']['root_identity'] == record['source_identity'], 'retirement archive changed')
    root, _ = private_directory(Path(state) / 'runs')
    source, trash = root / run_id, root / ('.retired-' + run_id)
    pins = dependencies(state) if pins is None else pins
    ledger.require(not any(p == path or p.is_relative_to(path) for p in pins
                           for path in (source, trash)), 'retirement dependency protected')
    archived_context = effective_context(state, run_id, Path(receipt['archive']) / 'files',
                                         original=manifest['original'])
    ledger.require(archived_context is not None, 'retirement epoch needs verified migration')
    active = references(root, retired_context=archived_context)
    ledger.require(run_id not in active and trash.name not in active, 'retirement is active')
    if record['state'] == 'RETIRED':
        ledger.require(not os.path.lexists(source) and not os.path.lexists(trash), 'retired path reused')
        return dict(run_id=run_id, state='RETIRED', resumed=True)
    if os.path.lexists(source):
        ledger.require(record['state'] == 'PREPARED' and not os.path.lexists(trash), 'retirement path conflict')
        ledger.require(snapshot(source) == manifest['original'], 'retirement source changed')
        os.rename(source, trash)
        sync_directory(root)
    if os.path.lexists(trash):
        remaining = snapshot(trash)
        original = manifest['original']
        ledger.require(remaining['root_identity'] == record['source_identity'] and
            remaining['entries'].keys() <= original['entries'].keys() and
            all(row == original['entries'][name] for name, row in remaining['entries'].items()),
            'retirement residue changed')
        record['state'] = 'RENAMED'
        save_record(catalog, record_path.name, record)
        # Validate all residue before deleting any of this invocation. Every
        # ancestor is opened without following symlinks; only sealed names go.
        for name, row in sorted(remaining['entries'].items(), key=lambda x: x[0].count('/'), reverse=True):
            parent_name, _, leaf = name.rpartition('/')
            directory = (open_relative(trash, parent_name, os.O_RDONLY | os.O_DIRECTORY)
                         if parent_name else os.open(trash, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
            try:
                info = os.stat(leaf, dir_fd=directory, follow_symlinks=False)
                if row['kind'] == 'file':
                    ledger.require(file_identity(info) == {k: row[k] for k in
                        ('device', 'inode', 'bytes', 'allocated_bytes', 'mode', 'modified_ns', 'changed_ns')},
                        'retirement file replaced')
                    os.unlink(leaf, dir_fd=directory)
                else:
                    ledger.require(stat.S_ISDIR(info.st_mode) and [info.st_dev, info.st_ino] == row['identity'],
                                   'retirement directory replaced')
                    os.rmdir(leaf, dir_fd=directory)
                os.fsync(directory)
            finally:
                os.close(directory)
        os.rmdir(trash); sync_directory(root)
    record['state'] = 'RETIRED'
    save_record(catalog, record_path.name, record)
    return dict(run_id=run_id, state='RETIRED', archive_verified=True)


def resume(state, run_id, *, pins=None):
    with claim(state) as catalog:
        return resume_locked(state, run_id, catalog, pins=pins)


def apply(state, expected, *, pins=None):
    ledger.require(type(expected) is dict and expected.get('format') == 'q4t.service.retention-plan.v1',
                   'retention plan required')
    with claim(state) as catalog:
        current = plan(state, limits=expected['policy'], now=expected['at_unix'], pins=pins)
        ledger.require(current == expected, 'retention plan or protections changed')
        # No deletion occurs before every selected archive/source is revalidated.
        results = []
        for row in current['remove']:
            run_id = row['run_id']
            path = catalog / (run_id + '.retire.json')
            ledger.require(not os.path.lexists(path), 'pending retirement: resume explicitly')
            save_record(catalog, path.name, dict(format='q4t.service.retirement.v1',
                service_id=current['service_id'], run_id=run_id, archive_receipt=row['receipt'],
                state='PREPARED', source_identity=row['source']['root_identity']))
            results.append(resume_locked(state, run_id, catalog, pins=pins))
        return dict(applied=True, retired=results, protected_untouched=True,
                    archives_retained=True, model_signalled=False)
