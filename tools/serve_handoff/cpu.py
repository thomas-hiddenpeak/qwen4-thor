"""Build isolated artifacts and verify the terminal handoff contracts."""
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_observation_capacity.cpu import command, h

BASE = ROOT/'.q4t-work/serve-structured-handoff-20261103'
CPU = BASE/'cpu'
ENGINE = BASE/'engine'


def sources():
    prior = h.read(ROOT/'build/q4t.release.json')
    return {p: h.sha(ROOT/p) for p in h.read(prior['tested_source_manifest'])}


def bindings():
    paths = [*sorted((ROOT/'tools/serve_handoff').glob('*.py')),
        ROOT/'tests/server_structured_admission_test.cpp',
        ROOT/'tests/server_http_response_test.cpp',
        ROOT/'tests/server_chat_capabilities_test.cpp',
        ROOT/'tools/release/required_host_tests.txt',
        ROOT/'docs/SERVE_STRUCTURED_HANDOFF_2026-11-03.md']
    return {str(p.relative_to(ROOT)): h.sha(p) for p in paths}


def build():
    CPU.mkdir(parents=True)
    h.save(CPU/'runtime-source.json', sources())
    h.save(CPU/'bindings.json', bindings())
    dependency = str(ROOT/'.q4t-work/structured-control-20261102/xgrammar')
    common = ['-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_CXX_COMPILER=/usr/bin/g++-14',
              '-DCMAKE_EXPORT_COMPILE_COMMANDS=ON']
    commands = [
        ('configure-off', ['cmake', '-S', str(ROOT/'tests/host'), '-B',
            str(BASE/'host-off'), *common, '-DQ4T_ENABLE_XGRAMMAR=OFF']),
        ('build-off', ['cmake', '--build', str(BASE/'host-off'), '--target',
            'q4t_host_tests', '--parallel', '2']),
        ('configure-engine', ['cmake', '-S', str(ROOT), '-B', str(ENGINE), *common,
            '-DCMAKE_CUDA_HOST_COMPILER=/usr/bin/g++-14',
            '-DCMAKE_CUDA_COMPILER=/usr/local/cuda-13.3/bin/nvcc',
            '-DQ4T_CUDA_ARCHITECTURES=110a', '-DQ4T_ENABLE_XGRAMMAR=ON',
            '-DQ4T_SERVE_FAULT_INJECTION=OFF', '-DQ4T_STARTUP_RESOURCE_PLAN=OFF',
            '-DQ4T_BUILD_TESTS=ON', '-DFETCHCONTENT_SOURCE_DIR_Q4T_XGRAMMAR='+dependency]),
        ('build-engine', ['cmake', '--build', str(ENGINE), '--target', 'q4t',
            'q4t_host_tests', 'q4t_http_lifecycle_tests', '--parallel', '2'])]
    h.save(CPU/'commands.json', commands)
    results = {}
    for name, argv in commands:
        results[name] = command(CPU, name, argv, timeout=1800)
        if not results[name]:
            break
    zero = not any(re.search(r'\bwarning:', p.read_text(), re.I)
                   for p in CPU.glob('*.std*'))
    passed = all(results.values()) and len(results) == len(commands) and zero
    h.save(CPU/'build-result.json', dict(passed=passed, commands=results,
        zero_warnings=zero, runtime_unchanged=sources() == h.read(CPU/'runtime-source.json'),
        bindings_unchanged=bindings() == h.read(CPU/'bindings.json'),
        binaries={str(p.relative_to(BASE)): h.identity(p) for p in
            [ENGINE/'q4t', ENGINE/'q4t_host_tests', ENGINE/'q4t_http_lifecycle_tests',
             BASE/'host-off/q4t_host_tests'] if p.exists()}))
    h.require(passed, 'build failed/warned; preserve original evidence')


def check():
    built = h.read(CPU/'build-result.json')
    h.require(built['passed'] and built['zero_warnings'] and built['runtime_unchanged']
        and sources() == h.read(CPU/'runtime-source.json'), 'build or source drift')
    for identity in built['binaries'].values():
        h.check_identity(identity)


def test():
    check()
    folder = CPU/'tests'; folder.mkdir()
    commands = [
        ('host-off', [str(BASE/'host-off/q4t_host_tests')]),
        ('host-required', ['/usr/bin/python3', '-B', 'tools/release/check_host_contracts.py',
            '--test-binary', str(ENGINE/'q4t_host_tests'), '--output', str(folder/'host-gate')]),
        ('backend', [str(ENGINE/'q4t_host_tests'), 'structured_decoder_']),
        ('inventory', ['/usr/bin/python3', '-B', 'tools/serve_audit/check_inventory.py', '--self-check']),
        ('generated', ['/usr/bin/python3', '-B', 'tools/serve_protocol/build_contract.py', '--check'])]
    results = {name: command(folder, name, argv, timeout=180) for name, argv in commands}
    h.save(folder/'result.json', dict(passed=all(results.values()), commands=results,
        runtime_unchanged=sources() == h.read(CPU/'runtime-source.json'),
        model_or_http_started=False, gpu_tests=0))
    h.require(all(results.values()), 'CPU failure; repair only affected checks')


if __name__ == '__main__':
    if sys.argv[1:] == ['build']: build()
    elif sys.argv[1:] == ['test']: test()
    else: raise SystemExit('build | test')
