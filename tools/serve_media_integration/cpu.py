"""Grouped SRV-02i integration build/CPU evidence, without a GPU or HTTP run."""
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_media_failure import cpu as old
from tools.serve_observation_capacity.cpu import command

h = old.h
BASE = ROOT/'.q4t-work/serve-media-integration-20261103'


def sources():
    paths = set(h.read(old.CPU/'build-sources.json')) | {
        'include/q4t/vision/media_plan.h', 'src/vision/media_plan.cpp',
        'include/q4t/server/media_admission.h', 'src/server/media_admission.cpp'}
    return {p: h.sha(ROOT/p) for p in sorted(paths)}


def bindings():
    paths = [*sorted((ROOT/'tools/serve_media_integration').glob('*')),
             *sorted((ROOT/'tools/serve_media_resource').glob('*')),
             ROOT/'tests/server_media_admission_test.cpp',
             ROOT/'tools/release/required_host_tests.txt',
             ROOT/'tools/serve_audit/check_inventory.py',
             ROOT/'tools/serve_audit/inventory.json',
             ROOT/'tools/serve_protocol/build_contract.py',
             ROOT/'docs/SERVE_MEDIA_INTEGRATION_2026-11-03.md']
    return {str(p.relative_to(ROOT)): h.sha(p) for p in paths if p.is_file()}


def build(folder):
    folder.mkdir()
    initial = sources()
    h.save(folder/'build-sources.json', initial)
    h.save(folder/'bindings.json', bindings())
    commands = [(name, [v.replace(str(old.BASE), str(BASE)) for v in argv])
                for name, argv in h.read(old.CPU/'commands.json')['commands']]
    for name, argv in commands:
        if '--parallel' in argv:
            argv[argv.index('--parallel')+1] = '2'
    for name, directory in (('core', 'serve_media_resource'),
                             ('processor-quota', 'serve_media_integration')):
        commands += [('configure-'+name, ['cmake', '-S', str(ROOT/'tools'/directory),
            '-B', str(BASE/name), '-DCMAKE_CXX_COMPILER=g++-14',
            '-DCMAKE_BUILD_TYPE=RelWithDebInfo', '-DCMAKE_EXPORT_COMPILE_COMMANDS=ON']),
            ('build-'+name, ['cmake', '--build', str(BASE/name), '--parallel', '2'])]
    h.save(folder/'commands.json', dict(commands=commands))
    passed = True
    for name, argv in commands:
        if not command(folder, name, argv, timeout=1800):
            passed = False
            break
    logs = '\n'.join(p.read_text() for p in folder.glob('*.std*'))
    zero = re.search(r'\bwarning:', logs, re.I) is None
    binaries = [BASE/'engine'/name for name in ('q4t', 'q4t_host_tests',
        'q4t_http_lifecycle_tests', 'q4t_host_memory_contract', 'q4t_vision_contract',
        'q4t_vision_upload')]
    binaries += [BASE/'validation/q4t', BASE/'host-off/q4t_host_tests',
        BASE/'host-on/q4t_host_tests', BASE/'core/media_plan_contract',
        BASE/'core/media_plan_processor', BASE/'processor-quota/processor_contract']
    h.save(folder/'build-result.json', dict(passed=passed and zero,
        zero_warnings=zero, sources_unchanged=sources() == initial,
        bindings_unchanged=bindings() == h.read(folder/'bindings.json'),
        binaries={str(p.relative_to(BASE)): h.identity(p) for p in binaries if p.exists()}))
    h.require(passed and zero and sources() == initial, 'build/source/warning gate')


def test(folder):
    build_result = h.read(folder/'build-result.json')
    h.require(build_result['passed'] and sources() == h.read(folder/'build-sources.json') and
              bindings() == h.read(folder/'bindings.json'), 'build/source/binding gate')
    for row in build_result['binaries'].values(): h.check_identity(row)
    out = folder/'tests'; out.mkdir()
    commands = [('host-'+mode, [str(BASE/('host-'+mode)/'q4t_host_tests')])
                for mode in ('off', 'on')]
    commands += [
        ('host-required', ['/usr/bin/python3', '-B', 'tools/release/check_host_contracts.py',
            '--test-binary', str(BASE/'engine/q4t_host_tests'), '--output', str(out/'host-gate')]),
        ('backend', [str(BASE/'engine/q4t_host_tests'), 'structured_decoder_']),
        ('config', ['/usr/bin/python3', '-B', 'tools/serve_admission/config_check.py',
            '--engine', str(BASE/'engine'), '--output', str(out/'config')]),
        ('inventory', ['/usr/bin/python3', '-B', 'tools/serve_audit/check_inventory.py', '--self-check']),
        ('generated', ['/usr/bin/python3', '-B', 'tools/serve_protocol/build_contract.py', '--check']),
        ('core', [str(BASE/'core/media_plan_contract')]),
        ('fixtures', [str(ROOT/'.q4t-work/refenv/bin/python'), '-B',
            'tools/serve_media_resource/fixtures.py', str(out/'fixtures')])]
    results = {name: command(out, name, argv, timeout=180) for name, argv in commands}
    if results['fixtures']:
        fixtures = h.read(out/'fixtures/manifest.json')['fixtures']
        results['processor'] = command(out, 'processor',
            [str(BASE/'core/media_plan_processor'), *[r['path'] for r in fixtures]], timeout=180)
        results['processor-quota'] = command(out, 'processor-quota',
            [str(BASE/'processor-quota/processor_contract'),
             str(out/'fixtures/140x100.png'), str(out/'fixtures/272x272.jpg')], timeout=180)
    if results['config']:
        from tools.serve_startup_memory.config_check import run
        run(out/'config/config_probe', out/'startup-config')
        results['startup-config'] = True
    pixels = out/'pixels'; pixels.mkdir()
    results['persistent-host'] = command(out, 'persistent-host',
        [str(BASE/'engine/q4t_host_memory_contract'), str(ROOT/'tools/vision_test_a.png'),
         str(ROOT/'tools/vision_test_b.png'), str(pixels)], timeout=180)
    if results['persistent-host']:
        results['processor-reference'] = command(out, 'processor-reference',
            [str(ROOT/'.q4t-work/refenv/bin/python'), '-B',
             'tools/serve_media_failure/processor_reference.py', str(pixels)], timeout=180)
    h.save(out/'result.json', dict(passed=all(results.values()), commands=results,
        sources_unchanged=sources() == h.read(folder/'build-sources.json'),
        model_or_http_started=False, gpu_tests=0))
    h.require(all(results.values()), 'CPU failure; repair only affected checks')


if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[1] not in ('build', 'test'):
        raise SystemExit('build|test ATTEMPT_NAME')
    name = sys.argv[2]
    if not re.fullmatch(r'cpu-[a-z0-9-]+', name): raise SystemExit('invalid attempt')
    (build if sys.argv[1] == 'build' else test)(BASE/name)
