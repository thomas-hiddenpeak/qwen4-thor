"""Repair two CPU probe defects without repeating unchanged runtime checks."""
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_media_integration import cpu as c

h, BASE = c.h, c.BASE
FIRST = BASE/'cpu-01'
OUT = BASE/'cpu-correction-01'
FIXED = ('tests/server_chat_tool_history_test.cpp',
         'tools/serve_media_failure/host_contract.cpp',
         'tools/serve_media_integration/processor_contract.cpp')


def bindings():
    return c.bindings() | {p: h.sha(ROOT/p) for p in FIXED}


def build():
    h.require(c.sources() == h.read(FIRST/'build-sources.json'),
              'runtime changed; targeted probe correction no longer applies')
    OUT.mkdir()
    h.save(OUT/'bindings.json', bindings())
    h.save(OUT/'plan.json', dict(
        original_result=h.identity(FIRST/'tests/result.json'),
        reasons=['The user-only media contract now rejects ordered tool media too.',
                 'Optimizer removed cpp_malloc=true around builtin malloc. '
                 'C-only injection/counts included C++ allocations. '
                 'Explicit __real_malloc plus ownership tracking separates domains.'],
        runtime_unchanged=True, model_or_http_started=False,
        scopes=['one failed host case in OFF/ON/engine',
                'quota/cancellation/ledger probe',
                'persistent host allocation probe with disjoint C/C++ counts',
                'four byte-exact processor outputs after corrected probe']))
    commands = [('build-'+name, ['cmake', '--build', str(BASE/name), '--target',
                  'q4t_host_tests', '--parallel', '2'])
                for name in ('host-off', 'host-on')]
    commands += [('build-engine-probes', ['cmake', '--build', str(BASE/'engine'),
        '--target', 'q4t_host_tests', 'q4t_host_memory_contract', '--parallel', '2']),
        ('build-quota', ['cmake', '--build', str(BASE/'processor-quota'),
                        '--parallel', '2'])]
    results = {name: c.command(OUT, name, argv, timeout=300)
               for name, argv in commands}
    logs = '\n'.join(p.read_text() for p in OUT.glob('*.std*'))
    zero = re.search(r'\bwarning:', logs, re.I) is None
    binaries = [BASE/name/'q4t_host_tests'
                for name in ('host-off', 'host-on', 'engine')]
    binaries += [BASE/'engine/q4t_host_memory_contract',
                 BASE/'processor-quota/processor_contract']
    passed = all(results.values()) and zero
    h.save(OUT/'build-result.json', dict(passed=passed, zero_warnings=zero,
        commands=results, binaries={str(p.relative_to(BASE)): h.identity(p)
                                   for p in binaries},
        runtime_unchanged=c.sources() == h.read(FIRST/'build-sources.json'),
        bindings_unchanged=bindings() == h.read(OUT/'bindings.json')))
    h.require(passed and bindings() == h.read(OUT/'bindings.json'), 'build gate')


def test():
    build_result = h.read(OUT/'build-result.json')
    h.require(build_result['passed'] and build_result['runtime_unchanged'] and
              c.sources() == h.read(FIRST/'build-sources.json') and
              bindings() == h.read(OUT/'bindings.json'), 'source/binding gate')
    for row in build_result['binaries'].values(): h.check_identity(row)
    out = OUT/'tests'; out.mkdir()
    commands = [('host-'+name, [str(BASE/name/'q4t_host_tests'),
                               'chat_tool_history_media_order_guard'])
                for name in ('host-off', 'host-on', 'engine')]
    commands += [('processor-quota', [str(BASE/'processor-quota/processor_contract'),
        str(FIRST/'tests/fixtures/140x100.png'),
        str(FIRST/'tests/fixtures/272x272.jpg')])]
    results = {name: c.command(out, name, argv, timeout=180)
               for name, argv in commands}
    pixels = out/'pixels'; pixels.mkdir()
    results['persistent-host'] = c.command(out, 'persistent-host',
        [str(BASE/'engine/q4t_host_memory_contract'), str(ROOT/'tools/vision_test_a.png'),
         str(ROOT/'tools/vision_test_b.png'), str(pixels)], timeout=180)
    if results['persistent-host']:
        results['processor-reference'] = c.command(out, 'processor-reference',
            [str(ROOT/'.q4t-work/refenv/bin/python'), '-B',
             'tools/serve_media_failure/processor_reference.py', str(pixels)], timeout=180)
    h.save(out/'result.json', dict(passed=all(results.values()), commands=results,
        runtime_unchanged=c.sources() == h.read(FIRST/'build-sources.json'),
        model_or_http_started=False, gpu_tests=0,
        original_failure_retained=True,
        reuse='All other first-run successful tests, sources unchanged. '
             'Old C-only failure count 68 is superseded: it included C++ allocations.'))
    h.require(all(results.values()), 'targeted CPU correction failed')


if __name__ == '__main__':
    if sys.argv[1:] == ['build']: build()
    elif sys.argv[1:] == ['test']: test()
    else: raise SystemExit('build | test')
