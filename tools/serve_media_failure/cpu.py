"""Grouped SRV-02h build and CPU checks; no model/HTTP or CUDA execution."""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_device_memory import stage as prior
from tools.serve_observation_capacity.cpu import command

h, a = prior.h, prior.a
BASE = ROOT/'.q4t-work/serve-media-failure-20261103'
CPU = BASE/'cpu'
ADDED = {'include/q4t/runtime/device_memory.h',
         'include/q4t/vision/vision.h', 'include/q4t/vision/processor.h',
         'src/vision/vision.cu', 'src/vision/processor.cpp', 'CMakeLists.txt'}


def sources():
    release = h.read(ROOT/'build/q4t.release.json')
    paths = set(h.read(release['tested_source_manifest'])) | ADDED
    return {p: h.sha(ROOT/p) for p in sorted(paths)}


def build():
    CPU.mkdir(parents=True)
    initial = sources()
    h.save(CPU/'build-sources.json', initial)
    old_base = ROOT/'.q4t-work/serve-backend-cleanup-20261103'
    commands = [(n, [v.replace(str(old_base), str(BASE)) for v in args])
                for n, args in h.read(old_base/'cpu/commands.json')['commands']]
    for name, argv in commands:
        if name == 'build-engine':
            argv.remove('q4t_cleanup_probe'); argv.remove('q4t_mtp_lifecycle')
            argv[argv.index('--parallel'):argv.index('--parallel')] = [
                'q4t_vision_contract', 'q4t_vision_upload', 'q4t_host_memory_contract']
        if name == 'build-validation': argv.remove('q4t_cleanup_probe')
    h.save(CPU/'commands.json', dict(commands=commands))
    passed = True
    for name, argv in commands:
        if not command(CPU, name, argv):
            passed = False
            break
    logs = '\n'.join(p.read_text() for p in CPU.glob('*.std*'))
    zero = re.search(r'\bwarning:', logs, re.I) is None
    h.save(CPU/'build-result.json', dict(passed=passed, zero_warnings=zero,
        runtime_sources_unchanged_during_build=sources() == initial,
        binaries={str(p.relative_to(BASE)): h.identity(p) for p in (
            BASE/'engine/q4t', BASE/'validation/q4t',
            BASE/'engine/q4t_vision_contract', BASE/'engine/q4t_vision_upload',
            BASE/'engine/q4t_host_memory_contract', BASE/'engine/q4t_host_tests',
            BASE/'engine/q4t_http_lifecycle_tests') if p.exists()}))
    h.require(passed and zero and sources() == initial, 'build/warning/source gate')


def test():
    h.require(h.read(CPU/'build-result.json')['passed'] and
              sources() == h.read(CPU/'build-sources.json'), 'build/source gate')
    folder = CPU/'tests'
    folder.mkdir()
    commands = [('host-'+mode, [str(BASE/('host-'+mode)/'q4t_host_tests')])
                for mode in ('off', 'on')]
    commands += [
        ('host-required', ['/usr/bin/python3', '-B', 'tools/release/check_host_contracts.py',
         '--test-binary', str(BASE/'engine/q4t_host_tests'), '--output', str(folder/'host-gate')]),
        ('backend', [str(BASE/'engine/q4t_host_tests'), 'structured_decoder_']),
        ('driver', ['/usr/bin/python3', '-B', '-m', 'unittest',
                    'tools.serve_media_failure.test_driver', '-v']),
        ('config', ['/usr/bin/python3', '-B', 'tools/serve_admission/config_check.py',
                    '--engine', str(BASE/'engine'), '--output', str(folder/'config')]),
        ('inventory', ['/usr/bin/python3', '-B', 'tools/serve_audit/check_inventory.py', '--self-check']),
        ('generated', ['/usr/bin/python3', '-B', 'tools/serve_protocol/build_contract.py', '--check'])]
    results = {name: command(folder, name, argv, timeout=180) for name, argv in commands}
    if results['config']:
        from tools.serve_startup_memory.config_check import run
        try:
            run(folder/'config/config_probe', folder/'startup-config')
            results['startup-config'] = True
        except Exception as error:
            results['startup-config'] = False
            h.save(folder/'startup-config-error.json', dict(error=repr(error)))
    if all(results.values()):
        from tools.serve_media_failure.fixtures import prepare
        prepare(BASE/'fixtures')
        pixels = folder/'pixels'; pixels.mkdir()
        results['persistent-host'] = command(folder, 'persistent-host', [
            str(BASE/'engine/q4t_host_memory_contract'),
            str(ROOT/'tools/vision_test_a.png'), str(ROOT/'tools/vision_test_b.png'),
            str(pixels)], timeout=180)
        if results['persistent-host']:
            results['processor-reference'] = command(folder, 'processor-reference', [
                str(ROOT/'.q4t-work/refenv/bin/python'), '-B',
                'tools/serve_media_failure/processor_reference.py', str(pixels)], timeout=180)
    h.save(folder/'result.json' , dict(passed=all(results.values()), commands=results,
                                    http_or_model_started=False))
    h.require(all(results.values()), 'CPU failure; repair only affected checks')


if __name__ == '__main__':
    if sys.argv[1:] == ['build']: build()
    elif sys.argv[1:] == ['test']: test()
    else: raise SystemExit('build | test')
