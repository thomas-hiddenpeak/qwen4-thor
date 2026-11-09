"""Source-bound recovery bundle around an already accepted native engine."""
import hashlib
import os
from pathlib import Path
import shutil
import stat
import sys

from tools.serve_gateway import client_policy as cp, gateway as ingress
from tools.serve_recovery import binding, core_http, gateway_config, ledger, tls_control
from tools.serve_service import package as engine

ROOT = Path(__file__).resolve().parents[2]
LEGACY_RECOVERY = ('binding', 'bundle', 'child', 'control', 'controller', 'core_http', 'cpu_exec',
            'gateway', 'gateway_config', 'jobs', 'journal', 'ledger',
            'monitor', 'ownership', 'policy', 'service', 'store')
LEGACY_SOURCES = tuple(sorted(set(engine.V9_SOURCES) | {
    'tools/serve_gateway/gateway.py', 'tools/serve_gateway/tls.py',
    *('tools/serve_recovery/'+name+'.py' for name in LEGACY_RECOVERY)}))
RECOVERY = (*LEGACY_RECOVERY, 'native_ingress')
V2_SOURCES = tuple(sorted(set(LEGACY_SOURCES) | {'tools/serve_recovery/native_ingress.py'}))
V3_SOURCES = tuple(sorted(set(V2_SOURCES) | {'tools/serve_recovery/observation_io.py'}))
V4_SOURCES = tuple(sorted(set(V3_SOURCES) | {'tools/serve_recovery/tls_control.py'}))
V5_SOURCES = tuple(sorted(set(V4_SOURCES) | {'tools/serve_recovery/progress.py'}))
V6_SOURCES = tuple(sorted(set(V5_SOURCES) | {'tools/serve_host/native.py',
    'tools/serve_host/native_exporter.py', 'tools/serve_recovery/retention.py',
    'tools/serve_recovery/retention_epoch.py'}))
V7_SOURCES = tuple(sorted(set(V6_SOURCES) | {'tools/serve_recovery/'+name+'.py' for name in
    ('access_policy', 'access_store', 'access_control', 'access_io', 'access_manager')}))
SOURCES = tuple(sorted(set(V7_SOURCES) | {'tools/serve_recovery/'+name+'.py' for name in
    ('quota_policy', 'quota_store', 'quota_control', 'quota_runtime', 'quota_manager',
     'usage_policy', 'usage_store', 'usage_io', 'usage_control', 'usage_runtime',
     'model_admin', 'usage_manager', 'deployment_policy', 'deployment', 'initialization')}))
LEGACY_EXECUTABLES = ('q4t-guardian', 'q4t-cuda-probe', 'nginx')
EXECUTABLES = ('q4t-guardian', 'q4t-cuda-probe', 'q4t-ingress')
NATIVE_SOURCES = frozenset(('CMakeLists.txt', 'tests/host/CMakeLists.txt',
    'cmake/GuardianProcess.cmake', 'cmake/RecoveryProbe.cmake',
    'include/q4t/status.h', 'include/q4t/io/json.h', 'src/io/json.cpp',
    'src/service/cuda_probe.cu', 'src/service/guardian_main.cpp',
    *('include/q4t/service/guardian_'+name+'.h' for name in
      ('process', 'journal', 'wire', 'quota')),
    *('src/service/guardian_'+name+'.cpp' for name in ('process', 'journal', 'wire')),
    'cmake/NativeIngress.cmake', 'src/service/ingress_main.cpp',
    'src/service/ingress_transport.cpp', 'include/q4t/service/ingress_transport.h',
    'include/q4t/service/ingress_access.h', 'src/service/ingress_access.cpp',
    'include/q4t/service/ingress_access_admin.h', 'src/service/ingress_access_admin.cpp',
    'include/q4t/trace/sha256.h', 'src/trace/sha256.cpp', 'include/q4t/trace/router_trace.h',
    *('include/q4t/service/ingress_'+name+'.h' for name in
      ('address', 'tls', 'credentials', 'cors', 'admin')),
    *('src/service/ingress_'+name+'.cpp' for name in
      ('tls', 'credentials', 'cors', 'admin')),
    'src/server/http_request.cpp',
    *('include/q4t/server/'+name+'.h' for name in ('http_request', 'http_listener',
        'http_observation', 'http_connection', 'model_catalog',
        'client_identity', 'serve_fault', 'application_routes'))))


def file_identity(path):
    info = Path(path).lstat()
    ledger.require(stat.S_ISREG(info.st_mode), 'model asset must be regular')
    return {name: getattr(info, 'st_'+name) for name in
            ('dev', 'ino', 'size', 'mtime_ns', 'ctime_ns')}


def assets(model):
    result = {}
    for path in sorted(Path(model).rglob('*')):
        if path.suffix not in ('.json', '.bin', '.safetensors', '.tiktoken', '.txt'):
            continue
        ledger.require(len(result) < 256, 'model asset inventory bound')
        result[str(path.relative_to(model))] = file_identity(path)
    ledger.require(result and any(name.endswith('.safetensors') for name in result)
                   and any(name.startswith('ple/') for name in result),
                   'incomplete native model asset inventory')
    return result


def check_assets(model, expected, *, read_edges=False):
    ledger.require(type(expected) is dict and 1 <= len(expected) <= 256,
                   'invalid model asset inventory')
    for name, wanted in expected.items():
        path = Path(model)/name
        ledger.require(not Path(name).is_absolute() and '..' not in Path(name).parts and
                       path.resolve(strict=True).is_relative_to(Path(model)),
                       'model asset escaped')
        ledger.require(file_identity(path) == wanted, 'model asset identity changed')
        if read_edges:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                size = wanted['size']
                for offset in {0, max(0, size-4096)}:
                    wanted_bytes = min(4096, size-offset)
                    ledger.require(len(os.pread(fd, wanted_bytes, offset)) == wanted_bytes,
                                   'model dependency short read')
            finally:
                os.close(fd)
            ledger.require(file_identity(path) == wanted, 'model asset changed during probe')


def launcher(digest, python):
    # The launcher is derived from the immutable manifest digest, avoiding a
    # circular self-hash. verify() checks its exact bytes as well.
    return f'''#!{python}
import os
from pathlib import Path
import sys
sys.dont_write_bytecode = True
package = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(package/'source'))
from tools.serve_recovery import service
if len(sys.argv) < 2 or sys.argv[1] != 'service':
    raise SystemExit('Usage: q4t service init|run|start|status|stop ...')
raise SystemExit(service.main(sys.argv[2:], package=package, digest='{digest}'))
'''.encode()


def gateway_config_for(package, manifest, directory, *, access=None):
    package = Path(package)
    base = engine.verify(package/'engine', manifest['engine_manifest_sha256'])
    policy = ingress.parse(ingress.read_file(package/'gateway-policy.json', private=True))
    _, backend = cp.read(package/'engine/client-policy.json')
    if manifest['version'] >= 8 and manifest.get('ingress_mode') == 'native':
        from tools.serve_recovery import quota_policy
        backend = quota_policy.from_base(backend)
    tls = None
    if manifest['version'] >= 4 and manifest.get('ingress_mode') == 'native':
        tls = manifest['tls_configuration']
    elif manifest['tls']:
        tls = dict(certificate=str(package/'certificate.pem'), key=str(package/'private-key.pem'))
    native = manifest.get('ingress_mode', 'nginx') == 'native'
    executable = 'q4t-ingress' if native else 'nginx'
    name = 'native_ingress' if native else 'nginx'
    ledger.require(access is None or (native and manifest['version'] >= 7),
                   'credential binding requires native access profile')
    return gateway_config.validate(dict(version=(4 if manifest['version'] >= 8 else 3)
        if access is not None else 2 if native else 1,
        directory=str(directory), **({'access': access} if access is not None else {}),
        **{name: str(package/'bin'/executable),
           name+'_sha256': manifest['files']['bin/'+executable]},
        policy=policy, backend_policy=backend, binary_sha256=base['files']['q4t'],
        protocol_sha256=base['protocol_sha256'], tls=tls))


def prepare(binary, release, model, guardian, cuda_probe, nginx, policy_path,
            backend_policy_path, output, *, native_receipt, certificate=None, key=None,
            native_ingress=None, ingress_mode='native', client_ca=None, client_crl=None,
            model_names=None, application_state=None):
    ledger.require(ingress_mode in ('native', 'nginx'), 'invalid ingress mode')
    native = ingress_mode == 'native'
    executable = native_ingress if native else nginx
    ledger.require(executable is not None, 'selected entrypoint binary is required')
    policy = ingress.policy(ingress.parse(ingress.read_file(policy_path, private=True)))
    ledger.require('upstream_operations_port' in policy,
                   'recovery bundle requires distinct operations port')
    _, backend = cp.read(backend_policy_path)
    cp.match_clients(policy, backend)
    core_http.self_check_client(backend)
    ledger.require((certificate is None) == (key is None), 'incomplete TLS configuration')
    if native:
        ledger.require(policy['version'] in (2, 3), 'native ingress requires source CIDRs')
    else:
        ledger.require(client_ca is None and client_crl is None and policy['version'] in (1, 2),
                       'nginx package requires legacy policy and no native client CA')
    ledger.require(client_ca is None or certificate is not None,
                   'client CA requires TLS certificate and key')
    ledger.require(client_crl is None or client_ca is not None,
                   'client CRL requires client CA')
    ledger.require(certificate is not None or policy['listen_host'] in ('127.0.0.1', '::1'),
                   'plaintext bundle must remain loopback')
    tls = None
    if native and certificate is not None:
        tls = dict(certificate=str(Path(certificate).absolute()),
            key=str(Path(key).absolute()), ca=str(Path(client_ca).absolute()) if client_ca else '',
            crl=str(Path(client_crl).absolute()) if client_crl else '',
            require_client_certificate=client_ca is not None)
    elif certificate is not None:
        ingress.certificate_check(certificate, key, policy['server_name'])
    build = engine.read(native_receipt)
    ledger.require(type(build.get('version')) is int and build['version'] == 1 and
        type(build.get('source_files')) is dict and
        build['source_files'].keys() == NATIVE_SOURCES and
        build.get('guardian_sha256') == engine.sha(guardian) and
        build.get('cuda_probe_sha256') == engine.sha(cuda_probe) and
        (not native or build.get('ingress_sha256') == engine.sha(native_ingress)),
        'native build receipt mismatch')
    for name, digest in build['source_files'].items():
        ledger.require(not Path(name).is_absolute() and '..' not in Path(name).parts and
                       engine.sha(ROOT/name) == digest, 'native source changed since build')
    if tls is not None:
        tls_control.check(native_ingress, policy, tls, Path(output).parent)
    target = engine.private_directory(output)
    receipt = engine.prepare(binary, release, model, target/'engine',
        port=policy['upstream_port'], operations_port=policy['upstream_operations_port'],
        client_policy=backend_policy_path, nvml_policy='degrade', model_names=model_names,
        application_state=application_state)
    (target/'bin').mkdir(mode=0o700)
    executables = EXECUTABLES if native else LEGACY_EXECUTABLES
    for name, source in zip(executables, (guardian, cuda_probe, executable)):
        shutil.copyfile(source, target/'bin'/name)
        (target/'bin'/name).chmod(0o500)
    for name in SOURCES:
        destination = target/'source'/name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT/name, destination)
        destination.chmod(0o400)
    ingress.save(target/'gateway-policy.json', ledger.canonical(policy)+b'\n')
    shutil.copyfile(native_receipt, target/'native-build.json')
    (target/'native-build.json').chmod(0o400)
    names = [*('bin/'+name for name in executables),
             *('source/'+name for name in SOURCES), 'gateway-policy.json', 'native-build.json']
    if certificate is not None and not native:
        for name, path in (('certificate.pem', certificate), ('private-key.pem', key)):
            ingress.save(target/name, ingress.read_file(path, private=name.startswith('private')))
            names.append(name)
    base = engine.verify(target/'engine', receipt['manifest_sha256'])
    # A fixed cooperative lease path is part of the deployment identity, not
    # resolved from a copied bundle's source directory at runtime.
    import subprocess
    common = Path(subprocess.check_output(['git', 'rev-parse', '--path-format=absolute',
        '--git-common-dir'], cwd=ROOT, text=True).strip())
    value = dict(version=8, profile='q4t-service-recovery-v8', repository=str(ROOT),
        ingress_mode=ingress_mode,
        engine_manifest_sha256=receipt['manifest_sha256'],
        files={name: engine.sha(target/name) for name in names}, python=base['python'],
        model_assets=assets(Path(base['model_dir'])), weight_shards_rehashed=False,
        resource_mode='process', require_hard_memory_limit=False,
        lock_path=str(common.parent/'.q4t-work/gpu-exclusive.lock'),
        quota_enforced=bool(native or binding.quota_enforced(backend)), tls=certificate is not None,
        tls_configuration=tls)
    engine.save(target/'manifest.json', value)
    digest = engine.sha(target/'manifest.json')
    ingress.save(target/'bin/q4t', launcher(digest, value['python']['path']))
    (target/'bin/q4t').chmod(0o500)
    verify(target, digest)
    return dict(package=str(target), manifest_sha256=digest,
                engine_binary_sha256=receipt['binary_sha256'], recovery_accepted=False)


def verify(package, digest):
    package = Path(package).resolve(strict=True)
    ledger.require(engine.sha(package/'manifest.json') == digest,
                   'recovery manifest identity changed')
    value = engine.read(package/'manifest.json')
    version = value.get('version')
    ledger.keys(value, ('version', 'profile', 'repository', 'engine_manifest_sha256',
        'files', 'python', 'model_assets', 'weight_shards_rehashed', 'resource_mode',
        'require_hard_memory_limit', 'lock_path', 'quota_enforced', 'tls',
        *(('ingress_mode',) if version in (2, 3, 4, 5, 6, 7, 8) else ()),
        *(('tls_configuration',) if version in (4, 5, 6, 7, 8) else ())))
    ledger.require(type(version) is int and version in (1, 2, 3, 4, 5, 6, 7, 8) and
        value['profile'] == 'q4t-service-recovery-v'+str(version) and
        value.get('ingress_mode', 'nginx') in ('native', 'nginx') and
        value['resource_mode'] == 'process' and
        value['require_hard_memory_limit'] is False and value['weight_shards_rehashed'] is False and
        type(value['quota_enforced']) is bool and type(value['tls']) is bool,
        'invalid recovery profile')
    native = value.get('ingress_mode', 'nginx') == 'native'
    executables = EXECUTABLES if native else LEGACY_EXECUTABLES
    sources = (SOURCES if version == 8 else V7_SOURCES if version == 7 else V6_SOURCES if version == 6 else
               V5_SOURCES if version == 5 else V4_SOURCES if version == 4 else
               V3_SOURCES if version == 3 else
               V2_SOURCES if version == 2 else LEGACY_SOURCES)
    names = {*('bin/'+name for name in executables), *('source/'+name for name in sources),
             'gateway-policy.json', 'native-build.json'}
    if version in (4, 5, 6, 7, 8):
        ledger.require((value['tls_configuration'] is not None) == (native and value['tls']),
                       'TLS configuration and native mode differ')
    if value['tls'] and not (version in (4, 5, 6, 7, 8) and native):
        names |= {'certificate.pem', 'private-key.pem'}
    ledger.require(type(value['files']) is dict and value['files'].keys() == names,
                   'recovery source inventory changed')
    for name, wanted in value['files'].items():
        engine.regular(package/name)
        ledger.require((package/name).resolve().is_relative_to(package) and
                       engine.sha(package/name) == wanted, 'recovery file identity changed')
    base = engine.verify(package/'engine', value['engine_manifest_sha256'])
    ledger.require(base['python'] == value['python'] and
        (package/'bin/q4t').read_bytes() == launcher(digest, value['python']['path']),
        'recovery launcher/interpreter changed')
    ledger.require(Path(value['lock_path']).is_absolute() and
                   Path(value['repository']).is_absolute(), 'invalid deployment paths')
    gateway = gateway_config_for(package, value, package)
    core_http.self_check_client(gateway['backend_policy'])
    ledger.require(value['quota_enforced'] == bool((version >= 8 and native) or
        binding.quota_enforced(gateway['backend_policy'])),
                   'quota gate differs from immutable client limits')
    check_assets(Path(base['model_dir']), value['model_assets'])
    return value
