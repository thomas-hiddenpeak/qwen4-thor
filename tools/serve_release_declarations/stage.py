"""Bounded metadata-only repair using archived observations; no model/HTTP."""
import argparse
import copy
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
from tools.serve_service import package as pkg
from tools.verify.startup_delivery import deploy

BASE = ROOT/'.q4t-work/serve-release-declarations-20261104'
ARCHIVE = ROOT/'docs/evidence/serve-operations-listener-20261104'
OLD_PACKAGE = ROOT/'.q4t-work/serve-operations-listener-20261104/accepted-backend'
ELF = '7eff020de66b96f3175b3e123909be9e2f58142d54d1dd5992750d23cbe9c769'
SIDECAR = 'abfaeff90bf460f1ff0cf0ee1f32da70d186e5ada0ab735e4576fc0d51c78e53'
MANIFEST = 'c4775a10cf6c2d308729a0e6decd0f1178d4aa8f27f50391d7b090e96e1a349a'
SOURCES = ('tools/serve_service/package.py',
    'tools/serve_service/test_service.py',
    'tools/serve_service/test_declarations.py',
    'tools/verify/startup_delivery/deploy.py',
    'tools/serve_release_declarations/stage.py')
TESTS = ('tools.serve_service.test_declarations.Declarations',
    'tools.serve_service.test_service.PackageTests',
    'tools.serve_service.test_resources.ResourceContracts',
    'tools.serve_clients.test_packages.Clients',
    'tools.serve_operations_listener.test_tools.Packages',
    'tools.verify.startup_delivery.test_deploy.DeployTest')


def identity(path):
    return dict(path=str(path), sha256=pkg.sha(path), bytes=path.stat().st_size)


def snapshot(source, target):
    pkg.regular(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('xb') as stream, source.open('rb') as original:
        shutil.copyfileobj(original, stream)
    target.chmod(0o400)
    pkg.require(pkg.sha(source) == pkg.sha(target), 'snapshot changed')
    return identity(target)


def archived(relative):
    rows = [r for r in pkg.read(ARCHIVE/'manifest.json')['records']
            if r.get('artifact') == relative]
    pkg.require(len(rows) == 1, 'missing archived observation')
    path = ARCHIVE/relative
    pkg.require(pkg.sha(path) == rows[0]['sha256'] and
                path.stat().st_size == rows[0]['bytes'], 'archive changed')
    return path


def freeze():
    pkg.require(pkg.sha(ROOT/'build/q4t') == ELF and
                pkg.sha(ROOT/'build/q4t.release.json') == SIDECAR,
                'default pair changed before freeze')
    pkg.verify(OLD_PACKAGE, MANIFEST)
    pkg.private_directory(BASE)
    metadata = pkg.read(ROOT/'build/q4t.release.json')
    previous = {name: snapshot(source, BASE/'previous'/name) for name, source in (
        ('q4t', ROOT/'build/q4t'),
        ('q4t.release.json', ROOT/'build/q4t.release.json'),
        ('CMakeCache.txt', Path(metadata['build_cache'])),
        ('acceptance.json', Path(metadata['acceptance_decision'])))}
    (BASE/'previous/q4t').chmod(0o500)
    raw = {
        'capabilities.json': archived(
            'records/execution-03/business/saturated-capabilities-wire.bin'),
        'configuration.json': archived(
            'records/execution-04/business/scoped-model-config-wire.bin')}
    for name in pkg.DECLARATION_SCHEMAS:
        raw['schemas/'+name+'.json'] = Path(
            metadata['chat_protocol'][name+'_schema'])
    declarations = {name: snapshot(path, BASE/'declarations'/name)
                    for name, path in raw.items()}
    source = pkg.read(metadata['tested_source_manifest'])
    pkg.require(all(pkg.sha(ROOT/n) == digest for n, digest in source.items()),
                'accepted ELF runtime source changed')
    snapshot(Path(metadata['tested_source_manifest']), BASE/'runtime-source.json')
    caps = pkg.read(BASE/'declarations/capabilities.json')
    protocol = metadata['chat_protocol']
    stale = sorted(k for k in protocol.keys() & caps.keys()
                   if not pkg.same_json(protocol[k], caps[k]))
    stale_schemas = [n for n in pkg.DECLARATION_SCHEMAS
                    if protocol[n+'_schema_sha256'] !=
                    declarations['schemas/'+n+'.json']['sha256']]
    pkg.require(stale == ['errors', 'http', 'observability', 'structured_output']
                and stale_schemas == ['config', 'preflight', 'response'],
                'unexpected declaration discrepancy')
    sources = {n: snapshot(ROOT/n, BASE/'source'/n) for n in SOURCES}
    pkg.save(BASE/'freeze.json', dict(previous=previous, sources=sources,
        declarations=declarations, original_sources={n: identity(p) for n,p in raw.items()},
        original_archive_manifest=identity(ARCHIVE/'manifest.json'),
        legacy_manifest=identity(OLD_PACKAGE/'manifest.json'),
        runtime_sources=len(source), stale_sections=stale,
        stale_schemas=stale_schemas, created_unix=time.time(),
        model_starts=0, http_requests=0, gpu_tests=0))


def check():
    record = pkg.read(BASE/'freeze.json')
    for name, item in record['sources'].items():
        pkg.require(pkg.sha(ROOT/name) == item['sha256'] ==
                    pkg.sha(item['path']), 'development source changed: '+name)
    for item in (*record['previous'].values(), *record['declarations'].values()):
        pkg.require(pkg.sha(item['path']) == item['sha256'], 'frozen input changed')
    runtime = pkg.read(BASE/'runtime-source.json')
    pkg.require(all(pkg.sha(ROOT/n) == digest for n,digest in runtime.items()),
                'runtime source changed')
    return record


def cpu():
    check()
    output = pkg.private_directory(BASE/'cpu')
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(n)
                               for n in TESTS)
    def ids(test):
        if isinstance(test, unittest.TestSuite):
            return [v for child in test for v in ids(child)]
        return [test.id()]
    names = ids(suite)
    pkg.require(len(names) == len(set(names)), 'duplicate test cases')
    with (output/'unittest.log').open('x') as log:
        result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
    pkg.save(output/'result.json', dict(status='PASS_CPU' if result.wasSuccessful()
        else 'FAIL_CPU', tests=result.testsRun, errors=len(result.errors),
        failures=len(result.failures), skipped=len(result.skipped), names=names))
    pkg.require(result.wasSuccessful() and not result.skipped, 'CPU gate failed')
    from jsonschema import Draft202012Validator
    for name in pkg.DECLARATION_SCHEMAS:
        Draft202012Validator.check_schema(pkg.read(
            BASE/'declarations/schemas'/(name+'.json')))
    Draft202012Validator(pkg.read(BASE/'declarations/schemas/config.json')).validate(
        pkg.read(BASE/'declarations/configuration.json'))
    pkg.verify(OLD_PACKAGE, MANIFEST)
    pkg.save(output/'offline.json', dict(status='PASS_OFFLINE', schemas=7,
        archived_config_valid=True, legacy_v6_verified=True,
        legacy_manifest_sha256=MANIFEST, model_starts=0, http_requests=0))


def prepare():
    check()
    pkg.require(pkg.read(BASE/'cpu/result.json')['status'] == 'PASS_CPU' and
                pkg.read(BASE/'cpu/offline.json')['status'] == 'PASS_OFFLINE',
                'CPU and offline gates required')
    metadata = pkg.read(BASE/'previous/q4t.release.json')
    root = BASE/'declarations'
    caps, config = (pkg.read(root/n) for n in pkg.DECLARATION_FILES[:2])
    protocol = metadata['chat_protocol']
    for key in caps.keys() & protocol.keys():
        protocol[key] = copy.deepcopy(caps[key])
    for name in pkg.DECLARATION_SCHEMAS:
        path = root/'schemas'/(name+'.json')
        protocol[name+'_schema'] = str(path)
        protocol[name+'_schema_sha256'] = pkg.sha(path)
    protocol['declaration_evidence'] = (
        'Frozen SRV-12g actual capabilities/config, seven schema snapshots; '
        'metadata-only correction, no additional runtime acceptance.')
    metadata['observed_configuration'] = config
    metadata['release_declarations'] = dict(profile=pkg.DECLARATION_PROFILE,
        directory=str(root), binary_sha256=ELF,
        protocol_sha256=protocol['protocol_sha256'],
        files={n: pkg.sha(root/n) for n in pkg.DECLARATION_FILES})
    metadata['metadata_correction'] = dict(version='serve-release-declarations-20261104',
        previous_sidecar_sha256=SIDECAR, freeze=identity(BASE/'freeze.json'),
        cpu=identity(BASE/'cpu/result.json'), offline=identity(BASE/'cpu/offline.json'))
    pkg.verify_declarations(metadata, required=True)
    target = pkg.private_directory(BASE/'candidate')
    pkg.save(target/'q4t.release.json', metadata)
    legacy = pkg.read(OLD_PACKAGE/'manifest.json')
    receipt = pkg.prepare(BASE/'previous/q4t', target/'q4t.release.json',
        legacy['model_dir'], BASE/'prepared-backend', port=legacy['settings']['port'],
        restart_limit=legacy['settings']['restart_limit'],
        critical_memory_mib=legacy['settings']['critical_memory_mib'],
        client_policy=OLD_PACKAGE/'client-policy.json',
        operations_port=legacy['settings']['operations_port'])
    pkg.save(BASE/'prepared.json', receipt)


def deliver():
    check()
    receipt = pkg.read(BASE/'prepared.json')
    pkg.verify(receipt['package'], receipt['manifest_sha256'])
    candidate = BASE/'candidate/q4t.release.json'
    deploy.verify_pair(ROOT/'build/q4t', candidate, ELF)
    lock = ROOT/'.q4t-work/gpu-exclusive.lock'
    lock_sha = pkg.sha(lock)
    fd = os.open(lock, os.O_RDWR | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        info = os.fstat(fd)
        pkg.require((info.st_dev, info.st_ino) == (66305, 3294067),
                    'shared lock identity changed')
        pkg.require(not (BASE/'delivery.json').exists(), 'already delivered')
        deploy.verify_pair(ROOT/'build/q4t', ROOT/'build/q4t.release.json', ELF)
        pkg.require(pkg.sha(ROOT/'build/q4t.release.json') == SIDECAR,
                    'default sidecar changed')
        deploy.replace_checked(candidate, ROOT/'build/q4t.release.json', SIDECAR)
        try:
            deploy.verify_pair(ROOT/'build/q4t', ROOT/'build/q4t.release.json', ELF)
            version = subprocess.run([str(ROOT/'build/q4t'), 'version'],
                capture_output=True, timeout=10, check=False)
            (BASE/'version.stdout').write_bytes(version.stdout)
            (BASE/'version.stderr').write_bytes(version.stderr)
            pkg.require(version.returncode == 0, 'CLI version check failed')
        except BaseException:
            deploy.replace_checked(BASE/'previous/q4t.release.json',
                ROOT/'build/q4t.release.json', pkg.sha(candidate))
            pkg.save(BASE/'rolled-back.json', dict(status='ROLLED_BACK'))
            raise
        pkg.require(pkg.sha(lock) == lock_sha, 'shared lock contents changed')
        pkg.save(BASE/'delivery.json', dict(status='DELIVERED_METADATA_ONLY',
            binary=identity(ROOT/'build/q4t'), sidecar=identity(ROOT/'build/q4t.release.json'),
            previous_sidecar_sha256=SIDECAR, lock_device=info.st_dev,
            lock_inode=info.st_ino, lock_contents_sha256=lock_sha,
            version_returncode=version.returncode, model_starts=0,
            http_requests=0, gpu_tests=0, service_started=False))
    finally:
        os.close(fd)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('freeze', 'cpu', 'prepare', 'deliver', 'check'))
    args = parser.parse_args()
    globals()[args.action]()
    print(json.dumps(dict(action=args.action, status='PASS')))
