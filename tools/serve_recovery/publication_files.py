"""Immutable default generations and one durable filesystem selection point."""
import os
from pathlib import Path
import stat

from tools.serve_host import native
from tools.serve_recovery import access_store, bundle, ledger, store
from tools.serve_recovery import publication_policy as policy
from tools.serve_service import package as engine

DIRECTORY = '.q4t-publication'
FILES = ('q4t', 'q4t.release.json', 'service.release.json', 'service.acceptance.json')
ALIASES = ('q4t', 'q4t.release.json', 'service', 'service.release.json')
MAX_GENERATIONS = 32


def sync(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:os.fsync(fd)
    finally:os.close(fd)


def directory(path, *, create=False):
    path = Path(path).absolute()
    if create:path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    ledger.require(path.resolve() == path and stat.S_ISDIR(info.st_mode) and
        info.st_uid == os.geteuid() and not stat.S_IMODE(info.st_mode) & 0o022,
        'owned canonical publication directory required')
    return path


def immutable_json(path, value):
    """Resume only a byte prefix of this immutable metadata, never new intent."""
    raw = ledger.canonical(value)
    temporary = path.with_name(path.name+'.new')
    if os.path.lexists(path):
        ledger.require(native.private_json(path) == value, 'immutable metadata changed')
        ledger.require(not os.path.lexists(temporary), 'immutable metadata temporary collision')
        with path.open('rb') as stream:os.fsync(stream.fileno())
        sync(path.parent)
        return
    fd = os.open(temporary, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = store.check_file(fd)
        ledger.require(info.st_size <= len(raw) and raw.startswith(os.pread(fd,len(raw)+1,0)),
                       'partial immutable metadata differs')
        os.ftruncate(fd,0);store.Store.write_all(fd,raw);os.fsync(fd)
    finally:os.close(fd)
    os.replace(temporary,path);sync(path.parent)


def bind(root, state, desc, manifest, *, initialize=False):
    root = directory(root)
    repository = Path(manifest['repository'])
    ledger.require(any(root.is_relative_to(repository/p) for p in ('build', '.q4t-work')),
                   'publication root outside workspace work/build')
    binding = access_store.binding(state, desc['service_id'])
    private = root/DIRECTORY
    if initialize:
        directory(private, create=True)
    directory(private)
    ledger.require(stat.S_IMODE(private.stat().st_mode) == 0o700,
                   'publication state must be private')
    wanted = dict(version=1, service_id=desc['service_id'], state=binding,
                  root=str(root), root_identity=native.inode(root))
    marker = private/'owner.json'
    if initialize and not os.path.lexists(marker):
        # No aliases may have been changed before this permanent ownership
        # binding. A partial unpublished marker is handled only by its writer.
        ledger.require({p.name for p in private.iterdir()} <= {'owner.json.new'},
                       'unbound publication directory')
        immutable_json(marker, wanted);sync(root)
    ledger.require(native.private_json(marker) == wanted, 'publication root identity changed')
    generations = private/'generations'
    if initialize:directory(generations, create=True);sync(private)
    directory(generations)
    return root


def accepted(release, package, digest, state, desc, *, decision_copy=None):
    package = Path(package).resolve(strict=True)
    manifest = bundle.verify(package, digest)
    base = engine.verify(package/'engine', manifest['engine_manifest_sha256'])
    metadata = engine.read(release)
    engine.require(metadata.get('recovery_accepted') is True and
        metadata.get('package') == str(package) and metadata.get('manifest_sha256') == digest and
        metadata.get('engine_binary_sha256') == base['files']['q4t'] and
        metadata.get('engine_release_sha256') == base['files']['q4t.release.json'] and
        metadata.get('state') == str(Path(state).absolute()) and
        metadata.get('service_id') == desc['service_id'], 'accepted publication identity differs')
    decision = Path(metadata['acceptance_record']) if decision_copy is None else Path(decision_copy)
    engine.require(engine.sha(decision) == metadata['acceptance_sha256'], 'service acceptance changed')
    value = engine.read(decision)
    engine.accepted_release(metadata, value)
    engine.require(value.get('acceptance', metadata['acceptance']) == metadata['acceptance'],
                   'service acceptance decision differs')
    return manifest, base


def generation(root, value):
    policy.snapshot(value)
    return Path(root)/DIRECTORY/'generations'/value['generation_sha256']


def manifest_value(root, desc, package, digest, base, release):
    return dict(version=1, format='q4t-default-generation-v1', root=str(root),
        service_id=desc['service_id'], package=str(Path(package).resolve()),
        manifest_sha256=digest, files={
            'q4t': base['files']['q4t'], 'q4t.release.json': base['files']['q4t.release.json'],
            'service.release.json': engine.sha(release),
            'service.acceptance.json': engine.read(release)['acceptance_sha256']})


def record(value):
    return dict(generation_sha256=ledger.digest(value), package=value['package'],
        manifest_sha256=value['manifest_sha256'], engine_binary_sha256=value['files']['q4t'],
        engine_release_sha256=value['files']['q4t.release.json'],
        service_release_sha256=value['files']['service.release.json'],
        service_acceptance_sha256=value['files']['service.acceptance.json'])


def copy_file(source, target, wanted, *, executable=False):
    if os.path.lexists(target):
        ledger.require(target.resolve() == target and engine.sha(target) == wanted,
                       'generation file changed')
        return
    temporary = target.with_name(target.name+'.partial')
    if os.path.lexists(temporary):
        check = os.open(temporary,os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            info = os.fstat(check)
            ledger.require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and
                info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) in
                    (0o600,0o500 if executable else 0o400),
                'private generation partial required')
            if engine.sha(temporary) == wanted:
                os.fchmod(check,0o500 if executable else 0o400);os.fsync(check)
                os.replace(temporary,target);sync(target.parent)
                return
            os.fchmod(check,0o600)
        finally:os.close(check)
    fd = os.open(temporary, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        store.check_file(fd);os.ftruncate(fd, 0)
        with Path(source).open('rb') as stream:
            while part := stream.read(1024 * 1024):store.Store.write_all(fd, part)
        ledger.require(engine.sha(temporary) == wanted, 'generation source changed during copy')
        os.fchmod(fd,0o500 if executable else 0o400);os.fsync(fd)
    finally:os.close(fd)
    os.replace(temporary, target);sync(target.parent)


def snapshot(root, state, desc, package, digest, release):
    """Only qualified bytes become a generation; partial writes are resumable."""
    manifest, base = accepted(release, package, digest, state, desc)
    root = bind(root, state, desc, manifest, initialize=True)
    value = manifest_value(root, desc, package, digest, base, release)
    selected = record(value);target = generation(root, selected)
    if (target/'generation.json').exists():
        verify(root, state, desc, selected)
        return selected
    if not target.exists():
        ledger.require(len(list(target.parent.iterdir())) < MAX_GENERATIONS,
                       'publication generation capacity reached')
        target.mkdir(mode=0o700);sync(target.parent)
    directory(target)
    intent = target/'intent.json'
    if not os.path.lexists(intent):
        ledger.require({p.name for p in target.iterdir()} <= {'intent.json.new'},
                       'unbound partial generation')
        immutable_json(intent, value)
    ledger.require(native.private_json(intent) == value, 'generation preparation changed')
    for name, source in (('q4t', Path(package)/'engine/q4t'),
                         ('q4t.release.json', Path(package)/'engine/q4t.release.json'),
                         ('service.release.json', Path(release)),
                         ('service.acceptance.json',Path(engine.read(release)['acceptance_record']))):
        copy_file(source, target/name, value['files'][name], executable=name == 'q4t')
    link = target/'service'
    if not os.path.lexists(link):os.symlink(selected['package'], link);sync(target)
    ledger.require(link.is_symlink() and os.readlink(link) == selected['package'],
                   'generation package link changed')
    immutable_json(target/'generation.json', value)
    sync(target);sync(target.parent)
    verify(root, state, desc, selected)
    return selected


def verify(root, state, desc, selected):
    target = directory(generation(root, selected))
    value = native.private_json(target/'generation.json')
    ledger.require({p.name for p in target.iterdir()} ==
        {*FILES,'service','intent.json','generation.json'} and
        native.private_json(target/'intent.json') == value, 'generation inventory or intent changed')
    ledger.keys(value, ('version','format','root','service_id','package','manifest_sha256','files'))
    ledger.require(type(value['files']) is dict and set(value['files']) == set(FILES),
                   'invalid generation file inventory')
    ledger.require(record(value) == selected and value['root'] == str(root) and
        value['service_id'] == desc['service_id'] and value['format'] == 'q4t-default-generation-v1' and
        type(value['version']) is int and value['version'] == 1 and set(value['files']) == set(FILES),
        'publication generation identity differs')
    for name in FILES:
        ledger.require((target/name).resolve() == target/name and
            engine.sha(target/name) == value['files'][name], 'publication generation file changed')
    link = target/'service'
    ledger.require(link.is_symlink() and os.readlink(link) == selected['package'] and
        link.resolve(strict=True) == Path(selected['package']), 'publication package changed')
    manifest, _ = accepted(target/'service.release.json', selected['package'],
        selected['manifest_sha256'], state, desc, decision_copy=target/'service.acceptance.json')
    bind(root, state, desc, manifest)
    return target


def selected(root):
    path = Path(root)/DIRECTORY/'current'
    if not os.path.lexists(path):return None
    ledger.require(path.is_symlink(), 'publication current is not a link')
    target = os.readlink(path)
    prefix = 'generations/'
    ledger.require(target.startswith(prefix) and ledger.HASH.fullmatch(target[len(prefix):]),
                   'publication current target invalid')
    return target[len(prefix):]


def replace_link(path, target):
    temporary = path.with_name(path.name+'.publication-switch')
    if os.path.lexists(temporary):
        ledger.require(temporary.is_symlink() and os.readlink(temporary) == target,
                       'publication temporary link changed')
    else:os.symlink(target, temporary)
    os.replace(temporary, path);sync(path.parent)


def aliases(root):
    root = Path(root)
    return all((root/name).is_symlink() and
        os.readlink(root/name) == DIRECTORY+'/current/'+name for name in ALIASES)


def publish(root, state, desc, previous, target, *, fresh=False):
    """Called while deployment is prepared and all model ownership is held."""
    root = Path(root)
    old = verify(root, state, desc, previous)
    verify(root, state, desc, target)
    current = selected(root)
    ledger.require(current in (None, previous['generation_sha256'], target['generation_sha256']),
                   'publication predecessor changed')
    if current is None:
        replace_link(root/DIRECTORY/'current', 'generations/'+previous['generation_sha256'])
    elif current == target['generation_sha256'] and current != previous['generation_sha256']:
        ledger.require(aliases(root), 'published aliases are inconsistent')
        # A previous rename may be visible even though its directory fsync
        # failed. Repeating the selection must re-establish durability.
        sync(root);sync(root/DIRECTORY)
        return
    for name in ALIASES:
        path = root/name;desired = DIRECTORY+'/current/'+name
        if path.is_symlink() and os.readlink(path) == desired:continue
        if os.path.lexists(path):
            if name == 'service':
                ledger.require(path.is_symlink() and path.resolve() == Path(previous['package']),
                               'previous service pointer changed')
            else:
                ledger.require(path.resolve() == path and engine.sha(path) == engine.sha(old/name),
                               'previous default file changed')
        else:ledger.require(fresh, 'previous default entry missing')
        replace_link(path, desired)
    ledger.require(aliases(root), 'default aliases incomplete')
    sync(root)
    replace_link(root/DIRECTORY/'current', 'generations/'+target['generation_sha256'])
    ledger.require(selected(root) == target['generation_sha256'] and aliases(root),
                   'publication selection changed')
