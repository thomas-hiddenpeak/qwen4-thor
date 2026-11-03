"""Freeze and deliver startup memory admission under one complete GPU lease."""
import argparse
import copy
import datetime
import hashlib
import json
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_startup_memory import cpu
from tools.serve_memory import stage as previous

h, a = previous.h, previous.a
BASE, PACKAGE, OUTPUT = cpu.BASE, cpu.BASE/'package', cpu.BASE/'execution'
DEFAULT_SHA = 'c597adc38e202bb810a2b7ace4314618bd504e76eff968bf9ea0103f4078a344'
SIDECAR_SHA = 'a350c9782c42bd2008026b35e61ed3d0acadc5e254a746caa6ee53743322fca9'
RULE_SHA = '849bf0cdeb4de4225c134183a678b8eb7985f50a5fa22ece4dd3c502067e0ff8'
LIMITS = dict(generations=5, negatives=2, starts=3, probes=4, observations=128, seconds=1800)
PLANNED = dict(generations=5, negatives=2, starts=3, probes=4)


def build_gate():
    current = cpu.sources()
    h.require(current == h.read(cpu.CPU/'build-sources.json'), 'runtime changed')
    result = h.read(cpu.CPU/'build-result.json')
    h.require(result['passed'] and result['zero_warnings'] and
        result['runtime_sources_unchanged_during_build'] and
        result['binary_sha256'] == h.sha(BASE/'engine/q4t'), 'build gate')
    cache = previous.previous.cache(BASE/'engine/CMakeCache.txt')
    h.require(cache['Q4T_SERVE_FAULT_INJECTION'] == 'OFF', 'production fault injection enabled')
    commands = h.read(BASE/'engine/compile_commands.json')
    server = [r for r in commands if r['output'].startswith('CMakeFiles/q4t_server.dir/')]
    h.require(server and all('-DQ4T_SERVE_FAULT_INJECTION=1' not in r['command'] for r in server),
              'production compile mode')
    a.verify_hashes(ROOT, h.read(BASE/'engine/generated/q4t/server/chat_protocol_sources.json'))
    probe = h.read(BASE/'probe-build/result.json')
    h.require(probe['passed'] and probe['zero_warnings'], 'probe build gate')
    h.check_identity(probe['binary']); h.check_identity(probe['source'])
    return current


def cpu_gate():
    folder = cpu.CPU/'tests'
    h.require(h.read(folder/'result.json')['passed'], 'grouped CPU checks')
    for label, count in (('host-off', 234), ('host-on', 243), ('backend', 9)):
        logs = (folder/(label+'.stdout')).read_text()+(folder/(label+'.stderr')).read_text()
        h.require(f'{count} tests, {count} passed, 0 failed, 0 skipped' in logs, 'CPU count: '+label)
    required = h.read(folder/'host-gate/result.json')
    h.require(required['passed'] and len(required['required_tests']) == 200 and
              required['binary_sha256'] == h.sha(BASE/'engine/q4t_host_tests'), 'required host identity')
    followup = cpu.CPU/'tests'
    h.require(h.read(followup/'result.json')['passed'] and
              h.read(followup/'config/result.json')['passed'] and
              h.read(followup/'startup-config/result.json')['passed'], 'native config/driver gate')
    build_gate()


def prepare():
    h.require(not PACKAGE.exists(), 'preserve frozen package')
    h.require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT),
              'commit reviewed stage before freezing')
    old = h.verify_pair(ROOT/'build/q4t', ROOT/'build/q4t.release.json', DEFAULT_SHA)
    h.require(h.sha(ROOT/'build/q4t.release.json') == SIDECAR_SHA, 'default sidecar changed')
    h.require(not list((ROOT/'build').glob('*recovery*.json')), 'recovery pending')
    cpu_gate()
    inherited = h.read(previous.PACKAGE/'plan.json')
    a.verify_hashes(inherited['model_dir'], inherited['model_inputs'])
    sdk = previous.previous.prior.sdk_identity()
    PACKAGE.mkdir()
    shutil.copytree(cpu.CPU, PACKAGE/'cpu')
    for name in ('candidate', 'previous'):
        (PACKAGE/name).mkdir()
    for name in a.NAMES:
        h.copy_checked(h.identity(ROOT/'build'/name), PACKAGE/'previous'/name)
    h.copy_checked(h.identity(old['build_cache']), PACKAGE/'previous/CMakeCache.txt')
    h.copy_checked(h.identity(ROOT/'deploy/serve-ops/q4t.rules.yml'), PACKAGE/'previous/q4t.rules.yml')
    for name in ('q4t', 'CMakeCache.txt', 'compile_commands.json', 'q4t_http_lifecycle_tests'):
        h.copy_checked(h.identity(BASE/'engine'/name), PACKAGE/'candidate'/name)
    shutil.copytree(BASE/'engine/generated', PACKAGE/'candidate/generated')
    h.copy_checked(h.identity(ROOT/'deploy/serve-ops/q4t.rules.yml'), PACKAGE/'candidate/q4t.rules.yml')
    h.copy_checked(h.identity(BASE/'probe-build/memory_probe'), PACKAGE/'candidate/memory_probe')
    shutil.copytree(BASE/'probe-build', PACKAGE/'probe-build')
    current = cpu.sources()
    h.save(PACKAGE/'candidate/runtime-source.json', current)
    old_sources = h.read(old['tested_source_manifest'])
    changed = [p for p in old_sources if current[p] != old_sources[p]]
    added = sorted(current.keys()-old_sources.keys())
    h.require(all(p.startswith(('src/server/', 'include/q4t/server/')) or
                  p in ('cmake/ChatProtocolIdentity.cmake', 'cmake/HostTestSources.cmake', 'src/main.cpp') for p in changed) and
              added == ['include/q4t/server/startup_admission.h'], 'non-serve numerical source changed')
    h.save(PACKAGE/'numerical-reuse.json', dict(changed=changed, added=added,
        previous_manifest=h.identity(old['tested_source_manifest']),
        unchanged_model_and_numerical_sources=True, performance_acceptance=False,
        scope='Startup available-memory admission, CLI and discovery only. Template, tokenizer, grammar, model math and MTP unchanged.'))
    paths = subprocess.check_output(['git', 'ls-files', 'tools', 'deploy/serve-ops'], cwd=ROOT, text=True).splitlines()
    paths += ['docs/SERVE_STARTUP_MEMORY_2026-11-03.md', 'docs/SERVE_OPERATIONS.md', 'docs/MODEL.md']
    paths += [str(p.relative_to(ROOT)) for p in (ROOT/'docs').glob('serve-*.json')]
    h.save(PACKAGE/'sources.json', {p:h.sha(ROOT/p) for p in sorted(set(paths)) if (ROOT/p).is_file()})
    h.save(PACKAGE/'sdk.json', sdk)
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    subprocess.run(['git', 'bundle', 'create', str(PACKAGE/'source.bundle'), '0af90eb8..HEAD'],
                   cwd=ROOT, timeout=60, check=True)
    protocol = h.read(PACKAGE/'candidate/generated/q4t/server/chat_protocol_sources.json')
    digest = hashlib.sha256(''.join(f'{p}:{protocol[p]}\n' for p in sorted(protocol)).encode()).hexdigest()
    h.save(PACKAGE/'candidate/diagnostic-identity.json', dict(binary_sha256=h.sha(PACKAGE/'candidate/q4t'),
        chat_protocol=dict(protocol_sha256=digest), candidate_identity_only=True,
        acceptance='PENDING; this is not a release attestation'))
    plan = dict(schema_version=1, root=str(ROOT), limits=LIMITS, planned=PLANNED,
        source_commit=commit, candidate=str(PACKAGE/'candidate/q4t'),
        candidate_sha256=h.sha(PACKAGE/'candidate/q4t'), binary=h.identity(PACKAGE/'candidate/q4t'),
        sidecar=h.identity(PACKAGE/'candidate/diagnostic-identity.json'),
        old_hashes={n:h.sha(PACKAGE/'previous'/n) for n in a.NAMES},
        previous_binary_sha256=DEFAULT_SHA, source_manifest_sha256=h.sha(PACKAGE/'sources.json'),
        protocol_sha256=digest, model_dir=inherited['model_dir'], model_inputs=inherited['model_inputs'],
        sdk_directory=sdk['sdk_directory'], port=18110, performance_acceptance=False,
        memory_probe=h.identity(PACKAGE/'candidate/memory_probe'),
        lifecycle_probe=h.identity(PACKAGE/'candidate/q4t_http_lifecycle_tests'))
    h.save(PACKAGE/'plan.json', plan)
    h.seal(PACKAGE)
    print('PREPARED', plan['candidate_sha256'], flush=True)


def check():
    h.verify_seal(PACKAGE)
    plan = h.read(PACKAGE/'plan.json')
    h.require(plan['root'] == str(ROOT) and plan['limits'] == LIMITS and plan['planned'] == PLANNED,
              'frozen scope/budget changed')
    h.require(plan['source_manifest_sha256'] == h.sha(PACKAGE/'sources.json'), 'source manifest changed')
    a.verify_hashes(ROOT, h.read(PACKAGE/'sources.json'))
    a.verify_hashes(ROOT, h.read(PACKAGE/'candidate/runtime-source.json'))
    a.verify_hashes(plan['model_dir'], plan['model_inputs'])
    for key in ('binary', 'lifecycle_probe', 'sidecar', 'memory_probe'):
        h.check_identity(plan[key])
    h.require(previous.previous.prior.sdk_identity() == h.read(PACKAGE/'sdk.json'), 'SDK changed')
    if (OUTPUT/'begin.json').exists():
        plan['started'] = plan['original_started'] = h.read(OUTPUT/'begin.json')['started']
    return plan


def verifier(plan, installed=False):
    if installed:
        from tools.serve_startup_memory.installed_health import run
        run(plan, OUTPUT)
        return
    folder = OUTPUT/'native'
    folder.mkdir()
    passed = cpu.command(folder, 'lifecycle', [plan['lifecycle_probe']['path']], timeout=90)
    logs = (folder/'lifecycle.stdout').read_text()+(folder/'lifecycle.stderr').read_text()
    passed = passed and '24 tests, 24 passed, 0 failed, 0 skipped' in logs
    h.save(folder/'result.json', dict(passed=passed, required_tests=24,
        binary=plan['lifecycle_probe'], raw_stdout_and_stderr_retained=True,
        model_loaded=False, parent_stage_lease=str(OUTPUT/'lease')))
    h.require(passed, 'native lifecycle failed; no model attempt')
    from tools.serve_startup_memory.verify import Run
    from tools.serve_lifecycle.observe import ObservationAbort
    try:
        Run(plan, OUTPUT/'business').run()
    except ObservationAbort as error:
        raise a.EnvironmentAbort(str(error)) from error


def release(plan):
    result = OUTPUT/'business/result.json'
    h.require(h.read(result)['status'] == 'PASS_BOUNDED_STARTUP_MEMORY', 'business gate')
    caps = json.loads(h.read(OUTPUT/'business/model/capabilities.json')['raw'])
    metadata = h.read(PACKAGE/'previous/q4t.release.json')
    inherited = copy.deepcopy({k:metadata[k] for k in ('acceptance', 'acceptance_decision',
        'acceptance_decision_sha256', 'chat_protocol', 'deployment_completion', 'scope') if k in metadata})
    inherited['release'] = h.identity(PACKAGE/'previous/q4t.release.json')
    metadata.pop('deployment_completion', None)
    metadata.update(version='serve-startup-memory-20261103', binary_sha256=plan['candidate_sha256'],
        source_commit=plan['source_commit'], tool_source_commit=plan['source_commit'],
        source_note='Fresh available-memory checks before metadata, allocations and readiness; estimate plus headroom before allocation. Zero/no-budget explicitly bypass. Fixed Thor accounting observations, no physical hard-limit/performance claim.',
        tested_source_manifest=str(PACKAGE/'candidate/runtime-source.json'),
        tested_source_manifest_sha256=h.sha(PACKAGE/'candidate/runtime-source.json'),
        build_cache=str(PACKAGE/'candidate/CMakeCache.txt'), build_cache_sha256=h.sha(PACKAGE/'candidate/CMakeCache.txt'),
        acceptance='ACCEPT_BOUNDED_STARTUP_MEMORY', acceptance_decision=str(result),
        acceptance_decision_sha256=h.sha(result), tool_acceptance=str(result), tool_acceptance_sha256=h.sha(result),
        previous_binary=str(PACKAGE/'previous/q4t'), previous_release=str(PACKAGE/'previous/q4t.release.json'),
        inherited_acceptance=inherited, implementation_capabilities=caps,
        operations_rules=h.identity(PACKAGE/'candidate/q4t.rules.yml'),
        rollback_operations_rules=h.identity(PACKAGE/'previous/q4t.rules.yml'),
        scope=dict(hardware='local Thor', heads=['full'], production='MTP k3, single8192/inflight8/queued2',
            validation='Native24, two real early refusals, three real startup snapshots, ordinary JSON/SSE, strict JSON and real SDK project-tool loop; four separate 256MiB committed allocation observations.',
            boundaries='No reservation against concurrent allocators, physical RAM hard bound, new long-soak or performance claim.',
            installation_health='CLI/worker and same-ELF candidate HTTP reuse; no installed model start',
            performance_acceptance=False, numerical_evidence=str(PACKAGE/'numerical-reuse.json')))
    metadata['chat_protocol'].update(protocol_sha256=plan['protocol_sha256'], template=caps['template'],
        observability=caps['observability'], http=caps['http'],
        sdk_interoperability_acceptance=dict(node='24.18.0', openai_javascript='7.47.0',
            scope='Strict real project read plus final answer over SSE; no automatic retry'))
    directory = OUTPUT/'release'
    directory.mkdir()
    h.save(directory/'q4t.release.json', metadata)
    h.save(directory/'binding.json', dict(old=plan['old_hashes'], new=dict(q4t=plan['candidate_sha256'],
        **{'q4t.release.json':h.sha(directory/'q4t.release.json')})))
    h.seal(directory)
    return directory


def bind():
    a.BASE, a.PACKAGE, a.OUTPUT = BASE, PACKAGE, OUTPUT
    a.MARKER = ROOT/'build/serve-startup-memory-recovery.json'
    a.check, a.verifier, a.release = check, verifier, release


def execute():
    h.require_guard(OUTPUT)
    events = [json.loads(line) for line in (OUTPUT/'lease/events.jsonl').read_text().splitlines()]
    started = datetime.datetime.fromisoformat(next(r['utc'] for r in events if r['event']=='lease_acquired')).timestamp()
    h.save(OUTPUT/'begin.json', dict(started=started, limits=LIMITS))
    def expired(signum, frame):
        raise RuntimeError('startup stage deadline; cleanup reserve only')
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, max(.1, started+LIMITS['seconds']-90-time.time()))
    try:
        bind(); a.execute()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


def finalize():
    check()
    h.require(time.time()-h.read(OUTPUT/'begin.json')['started'] <= LIMITS['seconds'], 'finalization deadline')
    bind()
    a.finalize()
    h.seal(OUTPUT)


def rollback_plan():
    h.verify_seal(PACKAGE)
    plan = h.read(PACKAGE/'plan.json')
    h.require(plan['root'] == str(ROOT) and plan['limits'] == LIMITS and
              plan['previous_binary_sha256'] == DEFAULT_SHA and
              plan['old_hashes']['q4t'] == DEFAULT_SHA, 'foreign rollback package')
    h.check_identity(plan['binary'])
    h.verify_pair(PACKAGE/'previous/q4t', PACKAGE/'previous/q4t.release.json',
                  DEFAULT_SHA, PACKAGE/'previous/CMakeCache.txt')
    h.require(h.sha(PACKAGE/'previous/q4t.rules.yml') == RULE_SHA, 'rollback rules changed')
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'launch', 'execute', 'finalize', 'rollback'))
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.action == 'launch':
        check()
        OUTPUT.mkdir(exist_ok=False)
        argv = ['/usr/bin/python3', '-B', str(ROOT/'tools/evalscope/gpu_exclusive.py'),
                '--output', str(OUTPUT/'lease'), '--', '/usr/bin/python3', '-B',
                str(Path(__file__).resolve()), 'execute']
        raise SystemExit(subprocess.call(argv, cwd=ROOT))
    if args.action == 'rollback':
        h.require(args.output is not None, 'fresh lease output required')
        bind()
        a.check = rollback_plan
        a.restore(args.output.resolve())
        return
    globals()[args.action]()


if __name__ == '__main__':
    main()
