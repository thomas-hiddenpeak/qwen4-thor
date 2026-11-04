"""Grouped build/host checks for SRV-10c. No CUDA execution or listeners."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT/'.q4t-work/serve-clients-20261104'
ENGINE = BASE/'engine'
HOST = BASE/'host'
STORAGE = BASE/'storage-build'


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def save(path, value):
    with Path(path).open('x') as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write('\n')


def sources():
    sidecar = json.loads((ROOT/'build/q4t.release.json').read_text())
    paths = set(json.loads(Path(sidecar['tested_source_manifest']).read_text()))
    for prefix in ('src/server', 'include/q4t/server', 'tools/serve_clients',
                   'tools/serve_gateway', 'tools/serve_service'):
        paths.update(str(p.relative_to(ROOT)) for p in (ROOT/prefix).rglob('*')
                     if p.suffix in ('.cpp', '.h', '.py') or p.name == 'CMakeLists.txt')
    paths.update(['tests/server_client_policy_test.cpp',
        'tests/server_http_response_test.cpp', 'tools/release/required_host_tests.txt',
        'tools/serve_protocol/build_contract.py', 'tools/serve_audit/inventory.json',
        'docs/serve-openapi-v1.json', 'docs/serve-config-v1.schema.json',
        'docs/serve-preflight-v1.schema.json'])
    paths.update(['docs/serve-transport-v1.schema.json',
                  'tools/serve_reliability/http_lifecycle_test.cpp',
                  'tools/serve_reliability/contract_access.h'])
    return {name: sha(ROOT/name) for name in sorted(paths)}


def command(out, name, argv, timeout=180):
    save(out/(name+'-command.json'), argv)
    with (out/(name+'.stdout')).open('xb') as stdout, \
         (out/(name+'.stderr')).open('xb') as stderr:
        result = subprocess.run(argv, cwd=ROOT, stdout=stdout, stderr=stderr,
                                timeout=timeout)
    save(out/(name+'-exit.json'), dict(returncode=result.returncode))
    print(name, result.returncode, flush=True)
    return result.returncode == 0


def build(out):
    common = ['-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_CXX_COMPILER=/usr/bin/g++-14',
              '-DCMAKE_EXPORT_COMPILE_COMMANDS=ON']
    commands = [
        ('configure-host', ['cmake', '-S', 'tests/host', '-B', str(HOST), *common,
                            '-DQ4T_ENABLE_XGRAMMAR=OFF']),
        ('build-host', ['cmake', '--build', str(HOST), '--target', 'q4t_host_tests',
                        '--parallel', '2']),
        ('configure-storage', ['cmake', '-S', 'tools/serve_clients', '-B', str(STORAGE), *common]),
        ('build-storage', ['cmake', '--build', str(STORAGE), '--parallel', '2']),
        ('configure-engine', ['cmake', '-S', '.', '-B', str(ENGINE), *common,
            '-DCMAKE_CUDA_HOST_COMPILER=/usr/bin/g++-14',
            '-DCMAKE_CUDA_COMPILER=/usr/local/cuda-13.3/bin/nvcc',
            '-DQ4T_CUDA_ARCHITECTURES=110a', '-DQ4T_ENABLE_XGRAMMAR=ON',
            '-DQ4T_SERVE_FAULT_INJECTION=OFF', '-DQ4T_STARTUP_RESOURCE_PLAN=OFF',
            '-DQ4T_BUILD_TESTS=ON', '-DFETCHCONTENT_SOURCE_DIR_Q4T_XGRAMMAR=' +
            str(ROOT/'.q4t-work/structured-control-20261102/xgrammar')]),
        ('build-engine', ['cmake', '--build', str(ENGINE), '--target', 'q4t',
                          'q4t_host_tests', 'q4t_http_lifecycle_tests', '--parallel', '2'])]
    results = {}
    for name, argv in commands:
        results[name] = command(out, name, argv, 2400)
        if not results[name]:
            break
    zero = not any(re.search(r'\bwarning:', p.read_text(), re.I) for p in out.glob('*.std*'))
    unchanged = sources() == json.loads((out/'sources.json').read_text())
    passed = len(results) == len(commands) and all(results.values()) and zero and unchanged
    binaries = {str(p.relative_to(ROOT)): sha(p) for p in
                (ENGINE/'q4t', ENGINE/'q4t_host_tests', ENGINE/'q4t_http_lifecycle_tests',
                 HOST/'q4t_host_tests', STORAGE/'client_storage') if passed}
    save(out/'result.json', dict(passed=passed, commands=results, zero_warnings=zero,
                                sources_unchanged=unchanged, binaries=binaries))
    return passed


def test(out, built):
    record = json.loads((built/'result.json').read_text())
    if not record['passed'] or sources() != json.loads((built/'sources.json').read_text()):
        raise ValueError('build source drift or failed build')
    if any(sha(ROOT/name) != digest for name, digest in record['binaries'].items()):
        raise ValueError('build binary drift')
    commands = [
        ('host-off', [str(HOST/'q4t_host_tests')]),
        ('host-required', ['/usr/bin/python3', '-B', 'tools/release/check_host_contracts.py',
            '--test-binary', str(ENGINE/'q4t_host_tests'), '--output', str(out/'host-gate')]),
        ('structured', [str(ENGINE/'q4t_host_tests'), 'structured_decoder_']),
        ('storage', [str(STORAGE/'client_storage'), str(out/'ssd-storage')]),
        ('packages', ['/usr/bin/python3', '-B', '-m', 'unittest', '-v',
            'tools.serve_clients.test_packages.Clients',
            'tools.serve_gateway.test_gateway', 'tools.serve_gateway.test_managed',
            'tools.serve_service.test_service']),
        ('inventory', ['/usr/bin/python3', '-B', 'tools/serve_audit/check_inventory.py', '--self-check']),
        ('generated', ['/usr/bin/python3', '-B', 'tools/serve_protocol/build_contract.py', '--check'])]
    results = {name: command(out, name, argv) for name, argv in commands}
    unchanged = sources() == json.loads((out/'sources.json').read_text())
    passed = all(results.values()) and unchanged
    save(out/'result.json', dict(passed=passed, checks=results, sources_unchanged=unchanged,
                                models=0, gpu_execution=0, http_listeners=0))
    return passed


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('build', 'test'))
    parser.add_argument('--round', required=True)
    parser.add_argument('--build-record', type=Path)
    args = parser.parse_args()
    if not re.fullmatch('[a-z][a-z0-9_-]{0,63}', args.round):
        parser.error('invalid round')
    out = BASE/args.round
    out.mkdir(parents=True)
    save(out/'sources.json', sources())
    save(out/'default.json', {name: sha(ROOT/name) for name in
                             ('build/q4t', 'build/q4t.release.json')})
    passed = build(out) if args.action == 'build' else test(out, args.build_record)
    raise SystemExit(0 if passed else 1)


if __name__ == '__main__':
    main()
