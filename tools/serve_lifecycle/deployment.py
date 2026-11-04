"""Known-pair switch/recovery; no restart, request replay or qualification claim."""
import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import math
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_lifecycle import catalog

h, shared = catalog.h, catalog.shared
NAMES = ('q4t', 'q4t.release.json')
ROLES = ('current', 'previous')


def writable_output(root, path):
    root, path = Path(root).resolve(), Path(path).absolute()
    parent = path.parent.resolve(strict=True)
    h.require(parent.is_relative_to(root/'.q4t-work') or parent.is_relative_to(root/'build'),
              'output must be below build or .q4t-work')
    h.require(not os.path.lexists(path), 'preserve existing output')
    return parent/path.name


def freeze(root, bundle, *, current=None):
    root = Path(root).resolve()
    bundle = writable_output(root, bundle)
    build = Path(current or root/'build').resolve()
    first = catalog.pair(build/'q4t', build/'q4t.release.json')
    h.require(not first['previous_release_was_stale'], 'legacy history is not a matching rollback pair')
    h.require(first['previous_binary'] and first['previous_release'], 'no direct rollback pair')
    previous = catalog.pair(first['previous_binary'], first['previous_release'])
    bundle.mkdir(mode=0o700)
    rows = {}
    for role, source in zip(ROLES, (first, previous)):
        version = source['observability_version']
        h.require(type(version) is int and version in (3, 4), 'unsupported operational version')
        h.require(isinstance(source['protocol_sha256'], str) and
                  catalog.HEX.fullmatch(source['protocol_sha256']), 'missing protocol identity')
        folder = bundle/role;folder.mkdir()
        for name, record in zip((*NAMES, 'CMakeCache.txt'),
                                (source['binary'], source['sidecar'], source['cache'])):
            h.copy_checked(record, folder/name)
        rules = root/'deploy/serve-ops'/('q4t.rules.yml' if version == 4 else 'q4t-v3.rules.yml')
        h.copy_checked(h.identity(rules), folder/'q4t.rules.yml')
        row = catalog.pair(folder/'q4t', folder/'q4t.release.json', cache=folder/'CMakeCache.txt')
        rows[role] = dict(pair=row, hashes={n:h.sha(folder/n) for n in NAMES},
                         rules=h.identity(folder/'q4t.rules.yml'), source=source)
    manifest = dict(version=1, root=str(root), destination=str(root/'build'), releases=rows,
        semantics='individual atomic replaces with a durable recovery marker; not a cross-file power-loss transaction',
        qualification='frozen existing identities only; business acceptance is independent')
    h.save(bundle/'manifest.json', manifest)
    h.seal(bundle)
    return manifest


def verify_bundle(bundle, root):
    bundle = Path(bundle).resolve(strict=True)
    h.verify_seal(bundle)
    value = h.read(bundle/'manifest.json')
    h.require(value.get('version') == 1 and value.get('root') == str(Path(root).resolve()) and
              value.get('destination') == str(Path(root).resolve()/'build') and
              set(value.get('releases', {})) == set(ROLES), 'foreign or malformed release bundle')
    for role in ROLES:
        row = value['releases'][role]
        h.require(set(row['hashes']) == set(NAMES), 'invalid pair hashes')
        shared.verify_hashes(bundle/role, row['hashes'])
        current = catalog.pair(bundle/role/'q4t', bundle/role/'q4t.release.json',
                               cache=bundle/role/'CMakeCache.txt')
        h.require(current == row['pair'], 'frozen release identity changed')
        h.check_identity(row['rules'])
    return value


def state(root, manifest):
    build = Path(root)/'build'
    h.require(build.is_dir() and not build.is_symlink(), 'build directory must be local')
    values = {}
    for name in NAMES:
        h.require_regular(build/name)
        digest = h.sha(build/name)
        h.require(digest in {manifest['releases'][r]['hashes'][name] for r in ROLES},
                  'unknown installed identity: '+name)
        values[name] = digest
    role = next((r for r in ROLES if values == manifest['releases'][r]['hashes']), None)
    return dict(hashes=values, role=role, consistent=role is not None)


def marker_path(root):return Path(root)/'build/serve-lifecycle-recovery.json'


def checked_marker(path, binding, manifest):
    raw = catalog.bounded_file(path, 65536)
    saved = catalog.json_object(raw)
    h.require(type(saved.get('version')) is int and saved['version'] == 1 and
              all(saved.get(k) == v for k, v in binding.items()) and
              saved.get('target') in ROLES, 'foreign recovery marker')
    initial = saved.get('initial', {})
    role = initial.get('role')
    h.require(role in ROLES and initial.get('consistent') is True and
              initial.get('hashes') == manifest['releases'][role]['hashes'],
              'unknown initial recovery identity')
    stamp = saved.get('started')
    h.require(type(stamp) in (int, float) and math.isfinite(stamp) and stamp > 0 and
              isinstance(saved.get('journal'), str) and
              Path(saved['journal']).is_absolute(), 'malformed recovery journal')
    return saved, catalog.hashlib.sha256(raw).hexdigest()


def publish_marker(path, value):
    """Publish a complete, durable marker exclusively before either replacement."""
    fd, name = tempfile.mkstemp(prefix='.release-marker-', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write((json.dumps(value, indent=2)+'\n').encode())
            stream.flush();os.fsync(stream.fileno())
        os.link(temporary, path)
        h.fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)
        h.fsync_directory(path.parent)


@contextmanager
def deployment_lock(root):
    path = Path(root)/'.q4t-work/serve-release.lock'
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:os.close(fd)


def _apply(root, bundle, target, output, *, recovering=False):
    """Filesystem transaction core; caller holds GPU and deployment locks."""
    root, bundle, output = Path(root).resolve(), Path(bundle).resolve(), Path(output).resolve()
    h.require(target in ROLES and (not recovering or target == 'current'), 'invalid recovery target')
    manifest = verify_bundle(bundle, root)
    before = state(root, manifest)
    marker = marker_path(root)
    binding = dict(bundle=str(bundle), manifest_sha256=h.sha(bundle/'manifest.json'), destination=str(root/'build'))
    other_markers = [p for p in (root/'build').glob('*recovery*.json') if p != marker]
    h.require(not other_markers, 'another deployment has a recovery marker')
    if recovering:
        saved, marker_sha = checked_marker(marker, binding, manifest)
        h.save(output/'original-marker.json', saved)
    else:
        h.require(not os.path.lexists(marker), 'recovery required before another switch')
        h.require(before['consistent'], 'unmarked mixed pair is not an installable release')
        publish_marker(marker, dict(version=1, **binding, target=target,
                                   initial=before, journal=str(output), started=time.time()))
        _, marker_sha = checked_marker(marker, binding, manifest)
    h.save(output/'before.json', before)
    hashes = manifest['releases'][target]['hashes']
    for name in NAMES:
        h.require(checked_marker(marker, binding, manifest)[1] == marker_sha,
                  'recovery marker changed during transaction')
        if before['hashes'][name] != hashes[name]:
            h.replace_checked(bundle/target/name, root/'build'/name, before['hashes'][name])
    after = state(root, manifest)
    h.require(after['consistent'] and after['hashes'] == hashes, 'replacement did not converge')
    h.verify_pair(root/'build/q4t', root/'build/q4t.release.json', hashes['q4t'],
                  bundle/target/'CMakeCache.txt')
    h.save(output/'after.json', dict(**after, selected_rules=manifest['releases'][target]['rules']))
    # Fail closed if a marker changed during the replacement window.
    h.require(checked_marker(marker, binding, manifest)[1] == marker_sha,
              'recovery marker changed')
    marker.unlink();h.fsync_directory(marker.parent)
    return after


def transact(root, bundle, target, output, lease_root, *, recovering=False):
    root = Path(root).resolve()
    h.require(root == ROOT, 'operator transactions require this workspace')
    h.require_guard(Path(lease_root).resolve())
    output = writable_output(root, output);output.mkdir(mode=0o700)
    result = dict(status='FAILED', target=target, recovering=recovering,
                  automatic_restart=False, requests_replayed=0)
    try:
        with deployment_lock(root):
            shared.require_idle_gpu(output/'gpu-before.json')
            result['installed'] = _apply(root, bundle, target, output, recovering=recovering)
            result['status'] = 'PAIR_RESTORED' if recovering else 'PAIR_SWITCHED'
        return result
    except BaseException as error:
        result['error'] = repr(error)
        if isinstance(error, shared.EnvironmentAbort):result['status'] = 'ENV_ABORT'
        raise
    finally:h.save(output/'result.json', result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('freeze', 'inspect', 'switch', 'recover'))
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--lease-root', type=Path)
    parser.add_argument('--target', choices=ROLES)
    args = parser.parse_args()
    if args.action == 'freeze':
        freeze(ROOT, args.bundle);print('FROZEN_IDENTITIES_ONLY');return
    if args.action == 'inspect':
        value = verify_bundle(args.bundle, ROOT)
        print(json.dumps(state(ROOT, value), indent=2));return
    h.require(args.output is not None and args.lease_root is not None, 'output and lease root required')
    if args.action == 'switch':h.require(args.target in ROLES, 'explicit switch target required')
    value = transact(ROOT, args.bundle, 'current' if args.action=='recover' else args.target,
                     args.output, args.lease_root, recovering=args.action=='recover')
    print(value['status'])


if __name__ == '__main__':main()
