"""Retain a CPU-verified candidate; this is not GPU acceptance or deployment."""
from pathlib import Path
import re
import shutil
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_media_integration import correction as fix
from tools.serve_media_failure import stage as old

c, h, BASE = fix.c, fix.h, fix.BASE
PACKAGE = BASE/'cpu-accepted'
FAILED = 'chat_tool_history_media_order_guard'


def hosts(first, corrected, count):
    raw, tail = first.read_text(), corrected.read_text()
    passed = re.findall(r'^\[PASS\] (.+)$', raw, re.M)
    failed = re.findall(r'^\[FAIL\] (.+)$', raw, re.M)
    h.require(len(passed) == count - 1 and len(set(passed)) == len(passed)
              and failed == [FAILED] and '[SKIP]' not in raw, 'first host shape')
    h.require(re.findall(r'^\[PASS\] (.+)$', tail, re.M) == [FAILED] and
              '1 tests, 1 passed, 0 failed, 0 skipped' in tail, 'host correction')
    return set(passed) | {FAILED}


def gate():
    original = h.read(fix.FIRST/'build-result.json')
    corrected = h.read(fix.OUT/'build-result.json')
    h.require(original['passed'] and original['zero_warnings'] and
              original['sources_unchanged'] and original['bindings_unchanged'] and
              corrected['passed'] and corrected['zero_warnings'] and
              corrected['runtime_unchanged'] and corrected['bindings_unchanged'],
              'build gate')
    h.require(c.sources() == h.read(fix.FIRST/'build-sources.json'), 'runtime drift')
    for path, digest in h.read(fix.OUT/'bindings.json').items():
        h.require(h.sha(ROOT/path) == digest, 'corrected binding drift: '+path)
    first, final = fix.FIRST/'tests', fix.OUT/'tests'
    failed_commands = {k for k, v in h.read(first/'result.json')['commands'].items() if not v}
    h.require(failed_commands == {'host-off', 'host-on', 'host-required',
                                  'processor-quota'}, 'unreviewed first failure')
    h.require(h.read(final/'result.json')['passed'], 'correction failed')
    hosts(first/'host-off.stdout', final/'host-host-off.stdout', 258)
    hosts(first/'host-on.stdout', final/'host-host-on.stdout', 267)
    names = hosts(first/'host-gate/run.log', final/'host-engine.stdout', 224)
    required = h.read(first/'host-gate/result.json')['required_tests']
    h.require(names == set(required) and len(required) == 224, 'required host set')
    h.require((first/'host-gate/required_host_tests.txt').read_bytes() ==
              (ROOT/'tools/release/required_host_tests.txt').read_bytes(), 'manifest drift')
    h.require('134 processor calls; 30 quota failures; 80 cancellation boundaries; '
              'ownership empty; concurrent budgets independent' in
              (final/'processor-quota.stdout').read_text(), 'quota/cancel coverage')
    h.require('cpp_failures=41 c_failures=27 successes=8' in
              (final/'persistent-host.stdout').read_text(), 'disjoint allocator coverage')
    for path in (first/'config/result.json', first/'startup-config/result.json',
                 final/'pixels/reference.json'):
        h.require(h.read(path)['passed'], 'configuration/pixel gate')
    h.require(h.read(first/'config/result.json')['schema_sha256'] ==
              h.sha(ROOT/'docs/serve-config-v1.schema.json'), 'schema drift')
    binaries = original['binaries'] | corrected['binaries']
    binaries['config/config_probe'] = h.identity(first/'config/config_probe')
    for row in binaries.values(): h.check_identity(row)
    for mode, injection in (('engine', 'OFF'), ('validation', 'ON')):
        cache = (BASE/mode/'CMakeCache.txt').read_text()
        h.require('Q4T_SERVE_FAULT_INJECTION:BOOL='+injection in cache and
                  'Q4T_STARTUP_RESOURCE_PLAN:BOOL=OFF' in cache, 'build mode')
        old.a.verify_hashes(ROOT, h.read(BASE/mode/
            'generated/q4t/server/chat_protocol_sources.json'))
    h.require(h.sha(ROOT/'build/q4t') == old.DEFAULT_SHA and
              h.sha(ROOT/'build/q4t.release.json') == old.SIDECAR_SHA, 'default changed')
    h.verify_seal(old.PACKAGE)
    h.require(not old.OUTPUT.exists(), 'old GPU candidate executed unexpectedly')
    return binaries


def create():
    binaries = gate()
    PACKAGE.mkdir()
    for name, row in binaries.items():
        target = PACKAGE/'artifacts'/name
        target.parent.mkdir(parents=True, exist_ok=True)
        h.copy_checked(row, target)
    for name in ('q4t', 'q4t.release.json'):
        target = PACKAGE/'previous'/name
        target.parent.mkdir(parents=True, exist_ok=True)
        h.copy_checked(h.identity(ROOT/'build'/name), target)
    for mode in ('engine', 'validation', 'host-off', 'host-on', 'core', 'processor-quota'):
        target = PACKAGE/'build-inputs'/mode; target.mkdir(parents=True)
        h.copy_checked(h.identity(BASE/mode/'CMakeCache.txt'), target/'CMakeCache.txt')
        commands = BASE/mode/'compile_commands.json'
        if commands.exists():
            h.copy_checked(h.identity(commands), target/'compile_commands.json')
        else:
            h.require(mode in ('host-off', 'host-on'), 'missing engine compile commands')
            h.save(target/'compile-commands-unavailable.json', dict(
                reason='Public host configure did not request CMAKE_EXPORT_COMPILE_COMMANDS.',
                raw_build_logs_retained=True))
        if (BASE/mode/'generated').exists():
            shutil.copytree(BASE/mode/'generated', target/'generated')
    sources = c.sources()
    bindings = fix.bindings() | {
        'docs/serve-config-v1.schema.json': h.sha(ROOT/'docs/serve-config-v1.schema.json'),
        'docs/serve-input-semantics-v1.json': h.sha(ROOT/'docs/serve-input-semantics-v1.json')}
    for path, digest in (sources | bindings).items():
        target = PACKAGE/'source'/path; target.parent.mkdir(parents=True, exist_ok=True)
        h.copy_checked(h.identity(ROOT/path), target)
        h.require(h.sha(target) == digest, 'snapshot changed')
    h.save(PACKAGE/'runtime-sources.json', sources)
    h.save(PACKAGE/'bindings.json', bindings)
    h.save(PACKAGE/'result.json', dict(status='CPU_ACCEPTED_GPU_PENDING',
        candidate=h.identity(PACKAGE/'artifacts/engine/q4t'),
        validation=h.identity(PACKAGE/'artifacts/validation/q4t'),
        previous_binary=old.DEFAULT_SHA, previous_sidecar=old.SIDECAR_SHA,
        runtime_sources=len(sources), public_host=[258, 267], required_host=224,
        host_reuse='First run 257/266/223 passed plus one corrected test per mode. '
                   'First FAILED remains; no full host rerun.',
        core_cases=80, processor_geometry=36, processor_quota_calls=134,
        quota_failures=30, cancellation_boundaries=80, backend_cases=9,
        fixed_response_cases=22, cpp_failures=41, c_failures=27,
        old_c_count_correction='68 included 41 C++ calls; 27 disjoint C allocator '
                               'boundaries are now independently checked.',
        pixel_reference=h.identity(fix.OUT/'tests/pixels/reference.json'),
        old_package_seal=h.identity(old.PACKAGE/'sealed-files.json'),
        model_or_http_started=False, gpu_tests=0, default_replaced=False,
        physical_ram_bound_proven=False, whole_goal_complete=False,
        next='Freeze composite 02h+02i GPU matrix and driver before a single '
             'exclusive lease. Preserve unrelated vLLM. No launch from this package.'))
    h.seal(PACKAGE)
    print('CPU_FROZEN', h.sha(PACKAGE/'artifacts/engine/q4t'))


if __name__ == '__main__':
    if sys.argv[1:] == ['create']: create()
    elif sys.argv[1:] == ['verify']:
        h.verify_seal(PACKAGE); gate(); print('CPU_FROZEN_VERIFIED')
    else: raise SystemExit('create | verify')
