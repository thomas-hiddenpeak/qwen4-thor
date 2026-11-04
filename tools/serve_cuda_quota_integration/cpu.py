"""Grouped CPU checks and isolated build; never starts a model or listener."""
import argparse
from pathlib import Path
import re
import shutil
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_cuda_quota_integration import common as c

ENGINE = c.BASE / 'engine'
HOST = c.BASE / 'host-off'
WRAPPERS = c.BASE / 'wrappers'


def build(folder):
    folder.mkdir(parents=True)
    c.save(folder / 'runtime-source.json', c.sources())
    common = ['-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_CXX_COMPILER=/usr/bin/g++-14',
              '-DCMAKE_EXPORT_COMPILE_COMMANDS=ON']
    commands = [
        ('configure-wrappers', ['cmake', '-S', str(Path(__file__).parent),
             '-B', str(WRAPPERS), *common]),
        ('build-wrappers', ['cmake', '--build', str(WRAPPERS), '--parallel', '2']),
        ('configure-off', ['cmake', '-S', str(ROOT / 'tests/host'),
             '-B', str(HOST), *common, '-DQ4T_ENABLE_XGRAMMAR=OFF']),
        ('build-off', ['cmake', '--build', str(HOST), '--target',
             'q4t_host_tests', '--parallel', '2']),
        ('configure-engine', ['cmake', '-S', str(ROOT), '-B', str(ENGINE),
             *common, '-DCMAKE_CUDA_HOST_COMPILER=/usr/bin/g++-14',
             '-DCMAKE_CUDA_COMPILER=/usr/local/cuda-13.3/bin/nvcc',
             '-DQ4T_CUDA_ARCHITECTURES=110a', '-DQ4T_ENABLE_XGRAMMAR=ON',
             '-DQ4T_SERVE_FAULT_INJECTION=OFF', '-DQ4T_STARTUP_RESOURCE_PLAN=OFF',
             '-DQ4T_BUILD_TESTS=ON', '-DFETCHCONTENT_SOURCE_DIR_Q4T_XGRAMMAR=' +
             str(ROOT / '.q4t-work/structured-control-20261102/xgrammar')]),
        ('build-engine', ['cmake', '--build', str(ENGINE), '--target', 'q4t',
             'q4t_host_tests', 'q4t_http_lifecycle_tests', '--parallel', '2'])]
    c.save(folder / 'commands.json', commands)
    results = {}
    for name, argv in commands:
        results[name] = c.command(folder, name, argv, timeout=2400)
        if not results[name]:
            break
    zero = not any(re.search(r'\bwarning:', p.read_text(), re.I)
                   for p in folder.glob('*.std*'))
    unchanged = c.sources() == c.read(folder / 'runtime-source.json')
    passed = len(results) == len(commands) and all(results.values()) and zero and unchanged
    binaries = {}
    if passed:
        (folder / 'bin').mkdir()
        for src, name in [(ENGINE / 'q4t', 'q4t'),
                (ENGINE / 'q4t_host_tests', 'host-on'),
                (ENGINE / 'q4t_http_lifecycle_tests', 'lifecycle'),
                (HOST / 'q4t_host_tests', 'host-off'),
                (WRAPPERS / 'quota_wrapper_contracts', 'wrappers'),
                (WRAPPERS / 'quota_wrapper_probe', 'native')]:
            target = folder / 'bin' / name
            shutil.copy2(src, target)
            binaries[name] = c.identity(target)
    c.save(folder / 'build-result.json', dict(passed=passed, commands=results,
        zero_warnings=zero, runtime_unchanged=unchanged, binaries=binaries))
    c.require(passed, 'build failed or warned; preserve this round')


def check(folder):
    result = c.read(folder / 'build-result.json')
    c.require(result['passed'] and result['zero_warnings'] and
        c.sources() == c.read(folder / 'runtime-source.json'), 'build/source gate')
    for value in result['binaries'].values():
        c.require(c.identity(value['path']) == value, 'binary identity drift')
    return result


def test(folder):
    built = check(folder)
    output = folder / 'tests'
    output.mkdir()
    binaries = {key: value['path'] for key, value in built['binaries'].items()}
    results = {}
    results['no-cuda-link'] = c.command(output, 'no-cuda-link',
        ['ldd', binaries['wrappers']])
    c.require(not re.search(r'libcuda|libnvidia|libcublas',
        (output / 'no-cuda-link.stdout').read_text()), 'CPU fixture links CUDA')
    for name in c.CASES:
        expected = 1 if name.startswith('fatal_') else 0
        ok = c.command(output, 'wrapper-' + name, [binaries['wrappers'], name],
                       expected=expected)
        raw = (output / ('wrapper-' + name + '.stdout')).read_text()
        error = (output / ('wrapper-' + name + '.stderr')).read_text()
        results[name] = ok and ('[q4t][cleanup-fatal]' in error and 'FAIL ' not in error
                               if expected else 'PASS ' + name + '\n' in raw)
    commands = [
        ('host-off', [binaries['host-off']]),
        ('host-required', ['/usr/bin/python3', '-B',
            'tools/release/check_host_contracts.py', '--test-binary',
            binaries['host-on'], '--output', str(output / 'host-gate')]),
        ('backend', [binaries['host-on'], 'structured_decoder_']),
        ('inventory', ['/usr/bin/python3', '-B',
            'tools/serve_audit/check_inventory.py', '--self-check']),
        ('generated', ['/usr/bin/python3', '-B',
            'tools/serve_protocol/build_contract.py', '--check'])]
    for name, argv in commands:
        results[name] = c.command(output, name, argv)
    c.save(output / 'result.json', dict(passed=all(results.values()),
        results=results, wrapper_cases=len(c.CASES), runtime_unchanged=
        c.sources() == c.read(folder / 'runtime-source.json'),
        gpu_tests=0, model_starts=0, http_listeners=0))
    c.require(all(results.values()), 'CPU failure; preserve and repair affected checks')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['build', 'test', 'check'])
    parser.add_argument('--round', default='cpu')
    args = parser.parse_args()
    c.require(re.fullmatch(r'cpu(?:-v[2-9][0-9]*)?', args.round), 'round name')
    globals()[args.action](c.BASE / args.round)
