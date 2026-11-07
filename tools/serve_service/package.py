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
V4_SOURCES = (*SOURCES, 'tools/serve_gateway/client_policy.py')
V5_SOURCES = (*V4_SOURCES, 'tools/serve_service/resources.py')
V7_SOURCES = (*V5_SOURCES, 'tools/serve_service/telemetry.py')
V8_SOURCES = (*V7_SOURCES, 'tools/serve_service/nvml_policy.py',
              'tools/serve_service/monitor_control.py')
V9_SOURCES = (*V8_SOURCES, 'tools/serve_service/auxiliary.py')
FILES = ('q4t', 'q4t.release.json', 'CMakeCache.txt', 'acceptance.json')
DECLARATION_PROFILE = 'q4t-release-declarations-v1'
DECLARATION_SCHEMAS = ('config', 'message', 'preflight', 'reasoning',
                       'response', 'stop', 'thinking')
DECLARATION_FILES = ('capabilities.json', 'configuration.json',
                     *('schemas/'+s+'.json' for s in DECLARATION_SCHEMAS))
DECLARATION_SECTIONS = ('cancellation', 'errors', 'http', 'memory_admission',
                        'observability', 'preflight', 'reasoning',
                        'structured_output', 'template', 'text_stop')
# Some releases combine preserved failed attempts and targeted successful
# repairs into an explicit acceptance decision. Do not accept arbitrary
# ACCEPT_* or turn a failed underlying attempt into a PASS_* receipt.
AGGREGATE_ACCEPTANCES = frozenset({'ACCEPT_BOUNDED_CLIENT_ISOLATION'})
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


def settings(port, restart_limit, critical_memory_mib=1024, operations_port=0,
             nvml_policy='strict'):
    require(type(nvml_policy) is str and nvml_policy in ('strict', 'degrade'),
            'invalid NVML policy')
    require(type(port) is int and 1024 <= port <= 65535, 'invalid port')
    require(type(restart_limit) is int and 0 <= restart_limit <= 3,
            'restart limit must be between zero and three')
    require(nvml_policy != 'degrade' or restart_limit == 0,
            'NVML degradation requires restart-limit zero')
    require(type(critical_memory_mib) is int and
            1 <= critical_memory_mib <= 1024**2, 'invalid critical memory floor')
    require(type(operations_port) is int and (operations_port == 0 or
            1024 <= operations_port <= 65535 and operations_port != port),
            'invalid operations port')
    result = dict(profile=PROFILE.copy(), port=port, restart_limit=restart_limit,
        poll_seconds=5, startup_seconds=300, unavailable_seconds=30,
        restart_delay_seconds=5, stop_seconds=30, kill_seconds=5,
        log_bytes=4*1024**2, log_files=2,
        critical_memory_mib=critical_memory_mib, memory_poll_seconds=1,
        memory_low_seconds=2, memory_unknown_seconds=5)
    if operations_port:
        result['operations_port'] = operations_port
    if nvml_policy == 'degrade':
        result['nvml_policy'] = nvml_policy
    return result


def accepted_release(metadata, decision):
    require(type(metadata) is dict and type(decision) is dict,
            'release and acceptance must be objects')
    acceptance, status = metadata.get('acceptance'), decision.get('status')
    require(type(acceptance) is str and acceptance.startswith('ACCEPT_') and
            type(status) is str and
            (status.startswith('PASS_') or
             status in AGGREGATE_ACCEPTANCES and status == acceptance),
            'explicit accepted release required')


def same_json(left, right):
    """JSON equality must distinguish true from 1 and false from 0."""
    return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(
        right, sort_keys=True, allow_nan=False)


def verify_declarations(metadata, directory=None, *, required=False):
    """Check frozen observations, not mutable docs or a live server.

    Old sealed packages/rollback pairs may lack the profile. New preparation
    requires it. A package supplies its own copied directory, so original
    artifact paths are provenance only and need not survive distribution.
    These are observed implementation claims, not new feature acceptance.
    """
    declaration = metadata.get('release_declarations')
    if 'release_declarations' not in metadata:
        require(not required, 'release declarations required for new package')
        return None
    require(type(declaration) is dict and set(declaration) == {
        'profile', 'directory', 'binary_sha256', 'protocol_sha256', 'files'},
        'invalid release declaration fields')
    require(declaration['profile'] == DECLARATION_PROFILE,
            'unknown release declaration profile')
    require(type(declaration['directory']) is str,
            'invalid declaration directory')
    original = Path(declaration['directory'])
    require(original.is_absolute() and '..' not in original.parts,
            'declaration directory must be absolute')
    root = Path(directory) if directory is not None else original
    require(root == root.resolve(strict=True), 'symlink declaration directory')
    files = declaration['files']
    require(type(files) is dict and set(files) == set(DECLARATION_FILES),
            'declaration inventory differs')
    for name, digest in files.items():
        path = root/name
        require(path == path.resolve(strict=True), 'symlink declaration file')
        regular(path)
        require(sha(path) == digest, 'declaration file changed: '+name)
    protocol = metadata['chat_protocol']
    require(declaration['binary_sha256'] == metadata['binary_sha256'] and
            declaration['protocol_sha256'] == protocol['protocol_sha256'],
            'declaration release identity differs')
    caps, config = (read(root/name) for name in DECLARATION_FILES[:2])
    require(type(caps) is dict and type(config) is dict and
            caps.get('protocol_source_sha256') ==
            config.get('protocol_source_sha256') == protocol['protocol_sha256']
            and type(caps.get('model')) is str and
            caps['model'] == config.get('model'),
            'declaration observed identity differs')
    require(set(DECLARATION_SECTIONS) <= caps.keys() & protocol.keys(),
            'declaration sections incomplete')
    for key in caps.keys() & protocol.keys():
        require(same_json(caps[key], protocol[key]),
                'declaration capability differs: '+key)
    require(same_json(config.get('observability_version'),
                      caps['observability']['version']),
            'declaration observation versions differ')
    require(same_json(metadata.get('observed_configuration'), config),
            'declaration configuration differs')
    for name in DECLARATION_SCHEMAS:
        relative = 'schemas/'+name+'.json'
        require(protocol.get(name+'_schema') == str(original/relative) and
                protocol.get(name+'_schema_sha256') == files[relative],
                'declaration schema differs: '+name)
        require(type(read(root/relative)) is dict,
                'declaration schema must be an object')
    return root


def prepare(binary, release, model, output, port=8080, restart_limit=0,
            critical_memory_mib=1024, client_policy=None, host_resources=None,
            operations_port=0, nvml_policy='strict'):
    config = settings(port, restart_limit, critical_memory_mib, operations_port,
                      nvml_policy)
    policy_raw = None
    if host_resources is not None:
        from tools.serve_service import resources
        resources.validate(host_resources)
    if client_policy is not None:
        from tools.serve_gateway import client_policy as cp
        policy_raw, _ = cp.read(client_policy)
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
    accepted_release(metadata, read(acceptance))
    protocol = metadata['chat_protocol']['protocol_sha256']
    require(isinstance(protocol, str) and len(protocol) == 64 and
            all(c in '0123456789abcdef' for c in protocol), 'invalid protocol')
    declarations = verify_declarations(metadata, required=True)
    model_files = {p.name: sha(p) for p in sorted(model.glob('*.json'))
                   if p.is_file() and not p.is_symlink()}
    require({'config.json', 'tokenizer.json', 'tokenizer_config.json'} <=
            model_files.keys(), 'model metadata incomplete')
    target = private_directory(output)
    for source, name in zip((binary, release, cache, acceptance), FILES):
        shutil.copyfile(source, target/name)
        os.chmod(target/name, 0o500 if name == 'q4t' else 0o400)
    sources = V5_SOURCES if host_resources is not None or operations_port else (V4_SOURCES if policy_raw is not None else SOURCES)
    if nvml_policy == 'degrade':
        sources = V9_SOURCES
    for name in sources:
        source, dest = ROOT/name, target/'source'/name
        regular(source)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
        os.chmod(dest, 0o400)
    names = [*FILES, *('source/'+s for s in sources)]
    for name in DECLARATION_FILES:
        dest = target/'declarations'/name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(declarations/name, dest)
        dest.chmod(0o400)
        names.append('declarations/'+name)
    if policy_raw is not None:
        with (target/'client-policy.json').open('xb') as stream:
            stream.write(policy_raw)
        (target/'client-policy.json').chmod(0o400)
        names.append('client-policy.json')
    python = Path(sys.executable).resolve()
    manifest = dict(version=3, repository=str(ROOT), model_dir=str(model),
        model_files=model_files, protocol_sha256=protocol, settings=config,
        python=dict(path=str(python), sha256=sha(python)),
        files={name: sha(target/name) for name in names},
        weight_shards_rehashed=False, managed_service_accepted=False)
    manifest['release_declarations'] = DECLARATION_PROFILE
    if policy_raw is not None:
        manifest['version'] = 4
        manifest['client_policy'] = cp.binding(policy_raw)
    if host_resources is not None:
        manifest['version'] = 5
        manifest['host_resources'] = dict(host_resources)
        manifest['resource_dependencies'] = resources.dependencies()
    if operations_port:
        manifest['version'] = 6
    if nvml_policy == 'degrade':
        manifest['version'] = 9
    save(target/'manifest.json', manifest)
    verify(target, sha(target/'manifest.json'))
    return dict(package=str(target), manifest_sha256=sha(target/'manifest.json'),
                binary_sha256=manifest['files']['q4t'])


def verify(package, digest):
    package = Path(package).resolve(strict=True)
    require(sha(package/'manifest.json') == digest, 'package manifest changed')
    manifest = read(package/'manifest.json')
    require(type(manifest['version']) is int and manifest['version'] in (1, 2, 3, 4, 5, 6, 7, 8, 9),
            'unsupported package version')
    config = manifest['settings']
    wanted = settings(config['port'], config['restart_limit'],
                      config.get('critical_memory_mib', 1024),
                      config.get('operations_port', 0) if manifest['version'] in (6, 7, 8, 9) else 0,
                      config.get('nvml_policy', 'strict') if manifest['version'] in (7, 8, 9) else 'strict')
    require(manifest['version'] not in (7, 8, 9) or config.get('nvml_policy') == 'degrade',
            'monitor package requires explicit degradation policy')
    require(manifest['version'] != 6 or config.get('operations_port', 0) != 0,
            'operations package requires explicit port')
    if manifest['version'] == 1:
        for key in ('critical_memory_mib', 'memory_poll_seconds',
                    'memory_low_seconds', 'memory_unknown_seconds'):
            del wanted[key]
    require(config == wanted, 'unknown managed configuration')
    sources = {1: LEGACY_SOURCES, 2: V2_SOURCES, 3: SOURCES,
               4: V4_SOURCES, 5: V5_SOURCES, 6: V5_SOURCES,
               7: V7_SOURCES, 8: V8_SOURCES, 9: V9_SOURCES}[manifest['version']]
    expected = {*FILES, *('source/'+name for name in sources)}
    if 'release_declarations' in manifest:
        require(manifest['release_declarations'] == DECLARATION_PROFILE,
                'unknown package declaration profile')
        expected.update('declarations/'+n for n in DECLARATION_FILES)
    if manifest['version'] == 4 or manifest['version'] in (5, 6, 7, 8, 9) and 'client_policy' in manifest:
        from tools.serve_gateway import client_policy as cp
        expected.add('client-policy.json')
        raw, _ = cp.read(package/'client-policy.json')
        require(manifest.get('client_policy') == cp.binding(raw),
                'client policy binding differs')
    else:
        require('client_policy' not in manifest, 'legacy package cannot scope clients')
    if manifest['version'] == 5 or manifest['version'] in (6, 7, 8, 9) and 'host_resources' in manifest:
        from tools.serve_service import resources
        resources.validate(manifest['host_resources'])
        resources.verify_dependencies(manifest['resource_dependencies'])
    else:
        require('host_resources' not in manifest and 'resource_dependencies' not in manifest,
                'legacy package cannot declare host resource protection')
    require(set(manifest['files']) == expected, 'package inventory differs')
    for name, wanted in manifest['files'].items():
        path = package/name
        regular(path)
        require(path.resolve().is_relative_to(package), 'package path escaped')
        require(sha(path) == wanted, 'package file changed: '+name)
    require(sha(manifest['python']['path']) == manifest['python']['sha256'],
            'Python interpreter changed')
    metadata = read(package/'q4t.release.json')
    require(('release_declarations' in metadata) ==
            ('release_declarations' in manifest),
            'package declaration profile differs')
    verify_declarations(metadata, package/'declarations',
                        required='release_declarations' in manifest)
    accepted_release(metadata, read(package/'acceptance.json'))
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
    if 'client_policy' in manifest:
        args += ['--client-policy-file', str(Path(package)/'client-policy.json')]
    if cfg.get('operations_port', 0):
        args += ['--operations-port', str(cfg['operations_port'])]
    return args
