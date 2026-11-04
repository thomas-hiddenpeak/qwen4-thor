"""Compile two probes against unchanged production archives; CPU fixture gate only."""
from pathlib import Path
import re
import shlex
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_media_integration import freeze_cpu as frozen
from tools.serve_observation_capacity.cpu import command

h = frozen.h
BASE = ROOT/'.q4t-work/serve-media-composite-20261103'
CPU = BASE/'cpu'
ENGINE = frozen.BASE/'engine'


def bindings():
    paths = [p for p in (ROOT/'tools/serve_media_composite').glob('*') if p.is_file()]
    paths += [ROOT/'docs/SERVE_MEDIA_COMPOSITE_2026-11-03.md']
    return {str(p.relative_to(ROOT)): h.sha(p) for p in paths}


def build():
    frozen.gate()
    CPU.mkdir()
    h.save(CPU/'bindings.json', bindings())
    entries = h.read(ENGINE/'compile_commands.json')
    row = next(r for r in entries if r['file'].endswith('/tools/serve_preflight/prompt_probe.cpp'))
    original_object = 'CMakeFiles/q4t_preflight_probe.dir/tools/serve_preflight/prompt_probe.cpp.o'
    original_link = shlex.split((ENGINE/'CMakeFiles/q4t_preflight_probe.dir/link.txt').read_text())
    archives = {str((ENGINE/arg).resolve()): h.sha(ENGINE/arg)
                for arg in original_link if arg.endswith('.a')}
    h.save(CPU/'link-archives.json', archives)
    results = {}
    for name in ('fixture_contract', 'device_contract'):
        source = ROOT/'tools/serve_media_composite'/(name+'.cpp')
        obj, binary = CPU/(name+'.o'), CPU/name
        compile_args = shlex.split(row['command'])
        compile_args[compile_args.index(row['file'])] = str(source)
        compile_args[compile_args.index('-o')+1] = str(obj)
        if '-Werror' not in compile_args: compile_args.append('-Werror')
        link = original_link.copy()
        link[link.index(original_object)] = str(obj)
        link[link.index('-o')+1] = str(binary)
        link = [str((ENGINE/arg).resolve()) if arg.endswith('.a') else
                '-Wl,--dependency-file='+str(CPU/(name+'.d'))
                if arg.startswith('-Wl,--dependency-file=') else arg for arg in link]
        if name == 'device_contract':
            link += ['-Wl,--wrap='+symbol for symbol in
                     ('cudaGetDevice', 'cudaDeviceGetAttribute', 'cudaFuncGetAttributes')]
        results[name+'-compile'] = command(CPU, name+'-compile', compile_args, timeout=180)
        if results[name+'-compile']:
            results[name+'-link'] = command(CPU, name+'-link', link, timeout=180)
    zero = not any(re.search(r'\bwarning:', p.read_text(), re.I) for p in CPU.glob('*.std*'))
    passed = all(results.values()) and len(results) == 4 and zero
    h.save(CPU/'build-result.json', dict(passed=passed, zero_warnings=zero, commands=results,
        runtime_unchanged=frozen.c.sources() == h.read(frozen.fix.FIRST/'build-sources.json'),
        bindings_unchanged=bindings() == h.read(CPU/'bindings.json'),
        binaries={name: h.identity(CPU/name) for name in ('fixture_contract', 'device_contract')
                  if (CPU/name).exists()}))
    h.require(passed and bindings() == h.read(CPU/'bindings.json'), 'probe build gate')


def check():
    frozen.gate()
    built = h.read(CPU/'build-result.json')
    h.require(built['passed'] and built['zero_warnings'] and built['runtime_unchanged'] and
              built['bindings_unchanged'] and bindings() == h.read(CPU/'bindings.json'), 'build binding drift')
    for row in built['binaries'].values(): h.check_identity(row)
    for path, sha in h.read(CPU/'link-archives.json').items():
        h.require(h.sha(path) == sha, 'linked archive changed')


def test(reuse=False):
    check()
    out = CPU/'tests'; out.mkdir()
    results = {'driver': command(out, 'driver', ['/usr/bin/python3', '-B', '-m', 'unittest',
        'tools.serve_media_composite.test_driver', '-v'], timeout=180)}
    if results['driver'] and reuse:
        prior = BASE/'cpu-before-deadline-review'
        before = h.read(prior/'build-result.json')['binaries']['fixture_contract']
        h.require(h.sha(CPU/'fixture_contract') == before['sha256'] and
                  h.sha(prior/'fixture_contract') == before['sha256'] and
                  h.read(prior/'tests/result.json')['passed'], 'fixture identity reuse gate')
        results['fixture'] = True
        h.save(out/'fixture-reuse.json', dict(passed=True, previous=h.identity(prior/'tests/result.json'),
            binary=h.identity(CPU/'fixture_contract'), same_elf=True,
            reason='Only C++ line wrapping and Python driver deadline/transaction changes.'))
        for name in ('fixture.stdout', 'fixture.stderr', 'fixture-command.json', 'fixture-exit.json'):
            h.copy_checked(h.identity(prior/'tests'/name), out/name)
    elif results['driver']:
        from tools.serve_media_composite.fixtures import prepare
        prepare(BASE/'fixtures')
        results['fixture'] = command(out, 'fixture',
            [str(CPU/'fixture_contract'), str(BASE/'fixtures')], timeout=60)
    h.save(out/'result.json', dict(passed=all(results.values()), commands=results,
        gpu_tests=0, model_or_http_started=False,
        previous_cpu_reused=True, physical_oom_or_damage=False))
    h.require(all(results.values()), 'CPU fixture/driver failure; preserve and repair affected checks')


if __name__ == '__main__':
    if sys.argv[1:] == ['build']: build()
    elif sys.argv[1:] == ['test']: test()
    elif sys.argv[1:] == ['test-reuse-fixture']: test(reuse=True)
    else: raise SystemExit('build | test | test-reuse-fixture')
