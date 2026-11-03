"""Snapshot the accepted text release and its operational dependencies."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys

ROOT = Path(__file__).resolve().parents[2]
LEGACY_SOURCES = (
    'tools/serve_service/package.py', 'tools/serve_service/supervisor.py',
    'tools/serve_service/backend.py',
    'tools/serve_service/service.py', 'tools/evalscope/gpu_exclusive.py',
    'tools/evalscope/bounded_log.py',
    'tools/evalscope/nvml_process_query.py', 'tools/release/release.py',
    'tools/serve_ops/contracts.py', 'tools/serve_ops/identity.py',
    'tools/serve_ops/transport.py',
)
V2_SOURCES = (*LEGACY_SOURCES, 'tools/serve_service/memory.py')
SOURCES = (*V2_SOURCES, 'tools/serve_service/inspection.py')
FILES = ('q4t', 'q4t.release.json', 'CMakeCache.txt', 'acceptance.json')
PROFILE = dict(name='text-mtp-managed-v1', host='127.0.0.1', max_seq=1,
    max_len=8192, max_prefill=2048, max_tokens=256, mtp_k=3,
    max_inflight_requests=8, max_queued_requests=2,
    min_available_memory_mib=2048, memory_recovery_margin_mib=512,
    startup_memory_headroom_mib=2048)


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def regular(path):
    value = Path(path).lstat()
    require(stat.S_ISREG(value.st_mode), 'regular file required')


def read(path):
    regular(path)
    require(Path(path).stat().st_size <= 2*1024**2, 'metadata size limit')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, 'duplicate metadata key')
            result[key] = value
        return result
    return json.loads(Path(path).read_text(), object_pairs_hook=unique,
        parse_constant=lambda _: require(False, 'non-finite metadata'))


def save(path, value):
    """Replace our private metadata, including file and directory durability."""
    path = Path(path)
    temp = path.with_name(path.name+'.new')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temp.unlink(missing_ok=True)


def private_directory(path, repository=ROOT):
    path = Path(path).absolute()
    require(path == path.resolve(), 'symlink or relative traversal in output')
    require(any(path.is_relative_to(repository / name)
                for name in ('build', '.q4t-work')), 'output outside workspace')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.mkdir(mode=0o700, exist_ok=False)
    return path


def settings(port, restart_limit, critical_memory_mib=1024):
    require(type(port) is int and 1024 <= port <= 65535, 'invalid port')
    require(type(restart_limit) is int and 0 <= restart_limit <= 3,
            'restart limit must be between zero and three')
    require(type(critical_memory_mib) is int and
            1 <= critical_memory_mib <= 1024**2, 'invalid critical memory floor')
    return dict(profile=PROFILE.copy(), port=port, restart_limit=restart_limit,
        poll_seconds=5, startup_seconds=300, unavailable_seconds=30,
        restart_delay_seconds=5, stop_seconds=30, kill_seconds=5,
        log_bytes=4*1024**2, log_files=2,
        critical_memory_mib=critical_memory_mib, memory_poll_seconds=1,
        memory_low_seconds=2, memory_unknown_seconds=5)


def prepare(binary, release, model, output, port=8080, restart_limit=0,
            critical_memory_mib=1024):
    binary, release, model = map(lambda p: Path(p).resolve(),
                                 (binary, release, model))
    regular(binary)
    metadata = read(release)
    require(sha(binary) == metadata['binary_sha256'], 'binary/sidecar mismatch')
    cache = Path(metadata['build_cache'])
    regular(cache)
    require(sha(cache) == metadata['build_cache_sha256'], 'cache mismatch')
    acceptance = Path(metadata['acceptance_decision'])
    regular(acceptance)
    require(sha(acceptance) == metadata['acceptance_decision_sha256'],
            'acceptance receipt mismatch')
    require(metadata['acceptance'].startswith('ACCEPT_') and
            read(acceptance)['status'].startswith('PASS_'),
            'explicit accepted release required')
    protocol = metadata['chat_protocol']['protocol_sha256']
    require(isinstance(protocol, str) and len(protocol) == 64 and
            all(c in '0123456789abcdef' for c in protocol), 'invalid protocol')
    config = settings(port, restart_limit, critical_memory_mib)
    model_files = {p.name: sha(p) for p in sorted(model.glob('*.json'))
                   if p.is_file() and not p.is_symlink()}
    require({'config.json', 'tokenizer.json', 'tokenizer_config.json'} <=
            model_files.keys(), 'model metadata incomplete')
    target = private_directory(output)
    for source, name in zip((binary, release, cache, acceptance), FILES):
        shutil.copyfile(source, target/name)
        os.chmod(target/name, 0o500 if name == 'q4t' else 0o400)
    for name in SOURCES:
        source, dest = ROOT/name, target/'source'/name
        regular(source)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
        os.chmod(dest, 0o400)
    names = [*FILES, *('source/'+s for s in SOURCES)]
    python = Path(sys.executable).resolve()
    manifest = dict(version=3, repository=str(ROOT), model_dir=str(model),
        model_files=model_files, protocol_sha256=protocol, settings=config,
        python=dict(path=str(python), sha256=sha(python)),
        files={name: sha(target/name) for name in names},
        weight_shards_rehashed=False, managed_service_accepted=False)
    save(target/'manifest.json', manifest)
    verify(target, sha(target/'manifest.json'))
    return dict(package=str(target), manifest_sha256=sha(target/'manifest.json'),
                binary_sha256=manifest['files']['q4t'])


def verify(package, digest):
    package = Path(package).resolve(strict=True)
    require(sha(package/'manifest.json') == digest, 'package manifest changed')
    manifest = read(package/'manifest.json')
    require(type(manifest['version']) is int and manifest['version'] in (1, 2, 3),
            'unsupported package version')
    config = manifest['settings']
    wanted = settings(config['port'], config['restart_limit'],
                      config.get('critical_memory_mib', 1024))
    if manifest['version'] == 1:
        for key in ('critical_memory_mib', 'memory_poll_seconds',
                    'memory_low_seconds', 'memory_unknown_seconds'):
            del wanted[key]
    require(config == wanted, 'unknown managed configuration')
    sources = {1: LEGACY_SOURCES, 2: V2_SOURCES, 3: SOURCES}[manifest['version']]
    expected = {*FILES, *('source/'+name for name in sources)}
    require(set(manifest['files']) == expected, 'package inventory differs')
    for name, wanted in manifest['files'].items():
        path = package/name
        regular(path)
        require(path.resolve().is_relative_to(package), 'package path escaped')
        require(sha(path) == wanted, 'package file changed: '+name)
    require(sha(manifest['python']['path']) == manifest['python']['sha256'],
            'Python interpreter changed')
    metadata = read(package/'q4t.release.json')
    require(metadata['binary_sha256'] == manifest['files']['q4t'] and
            metadata['build_cache_sha256'] == manifest['files']['CMakeCache.txt']
            and metadata['acceptance_decision_sha256'] ==
            manifest['files']['acceptance.json'] and
            metadata['chat_protocol']['protocol_sha256'] ==
            manifest['protocol_sha256'], 'inconsistent package identities')
    model = Path(manifest['model_dir'])
    for name, wanted in manifest['model_files'].items():
        require(Path(name).name == name and name.endswith('.json'),
                'invalid model metadata path')
        regular(model/name)
        require(sha(model/name) == wanted, 'model metadata changed: '+name)
    require({'config.json', 'tokenizer.json', 'tokenizer_config.json'} <=
            manifest['model_files'].keys(), 'model metadata incomplete')
    return manifest


def command(package, manifest):
    cfg = manifest['settings']
    args = [str(Path(package)/'q4t'), 'serve', '--model-dir', manifest['model_dir'],
            '--host', '127.0.0.1', '--port', str(cfg['port']), '--mtp']
    for key, value in PROFILE.items():
        if key not in ('name', 'host'):
            args += ['--'+key.replace('_', '-'), str(value)]
    return args
