"""Freeze, verify and deliver the combined media batch under one GPU lease."""
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
from tools.serve_media_composite import cpu
from tools.serve_media_failure import stage as old

h, a = old.h, old.a
BASE, PACKAGE, OUTPUT = cpu.BASE, cpu.BASE/'package', cpu.BASE/'execution'
DEFAULT_SHA, SIDECAR_SHA, RULE_SHA = old.DEFAULT_SHA, old.SIDECAR_SHA, old.RULE_SHA
LIMITS = dict(generations=35, negatives=24, starts=12, observations=384, seconds=2400)
PLANNED = dict(generations=32, negatives=21, starts=11)


def cpu_gate():
    cpu.check()
    h.require(h.read(cpu.CPU/'tests/result.json')['passed'], 'new fixture/driver CPU gate')
    log = (cpu.CPU/'tests/fixture.stdout').read_text()
    h.require('PASS cases=11 decoder_peak=' in log and
              'logical_quota=268435456 recovery=1 gpu_started=false' in log, 'fixture coverage')
    h.verify_seal(cpu.frozen.PACKAGE)
    h.verify_seal(old.PACKAGE)


def prepare():
    h.require(not PACKAGE.exists(), 'preserve frozen package')
    h.require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT),
              'commit reviewed stage before freezing')
    previous = h.verify_pair(ROOT/'build/q4t', ROOT/'build/q4t.release.json', DEFAULT_SHA)
    h.require(h.sha(ROOT/'build/q4t.release.json') == SIDECAR_SHA, 'default sidecar changed')
    h.require(not list((ROOT/'build').glob('*recovery*.json')), 'recovery pending')
    cpu_gate()
    inherited = h.read(old.PACKAGE/'plan.json')
    a.verify_hashes(inherited['model_dir'], inherited['model_inputs'])
    sdk = old.previous.previous.prior.sdk_identity()
    PACKAGE.mkdir()
    shutil.copytree(cpu.CPU, PACKAGE/'cpu')
    shutil.copytree(BASE/'fixtures', PACKAGE/'fixtures')
    for name in ('candidate', 'previous', 'validation', 'reviews'):
        (PACKAGE/name).mkdir()
    for name in a.NAMES:
        h.copy_checked(h.identity(ROOT/'build'/name), PACKAGE/'previous'/name)
    h.copy_checked(h.identity(previous['build_cache']), PACKAGE/'previous/CMakeCache.txt')
    for name in ('candidate', 'previous'):
        h.copy_checked(h.identity(ROOT/'deploy/serve-ops/q4t.rules.yml'), PACKAGE/name/'q4t.rules.yml')
    for name in ('q4t', 'CMakeCache.txt', 'compile_commands.json',
                 'q4t_http_lifecycle_tests', 'q4t_vision_contract', 'q4t_vision_upload'):
        h.copy_checked(h.identity(cpu.ENGINE/name), PACKAGE/'candidate'/name)
    shutil.copytree(cpu.ENGINE/'generated', PACKAGE/'candidate/generated')
    validation = cpu.frozen.BASE/'validation'
    for name in ('q4t', 'CMakeCache.txt', 'compile_commands.json'):
        h.copy_checked(h.identity(validation/name), PACKAGE/'validation'/name)
    shutil.copytree(validation/'generated', PACKAGE/'validation/generated')
    for name in ('numerical-review.json', 'inventory-review.json'):
        h.copy_checked(h.identity(cpu.frozen.BASE/name), PACKAGE/'reviews'/name)
    h.copy_checked(h.identity(cpu.frozen.PACKAGE/'result.json'), PACKAGE/'reviews/integration-cpu.json')
    current = cpu.frozen.c.sources()
    h.save(PACKAGE/'candidate/runtime-source.json', current)
    h.save(PACKAGE/'numerical-reuse.json', dict(
        unchanged_since_cpu=True, sources=h.identity(PACKAGE/'candidate/runtime-source.json'),
        reviewed_functions=h.identity(PACKAGE/'reviews/numerical-review.json'),
        geometry_and_pixel_evidence=h.identity(cpu.frozen.PACKAGE/'result.json'),
        scope='Unchanged 39cb0ba3 production source and exact ELF. Geometry/admission '
              'changes have CPU evidence; native GPU, actual media and text/tool quality '
              'must pass this stage. No new numerical or performance-equivalence claim.',
        performance_acceptance=False))
    paths = subprocess.check_output(['git', 'ls-files', 'tools', 'cmake', 'deploy/serve-ops'],
                                    cwd=ROOT, text=True).splitlines()
    paths += ['docs/SERVE_MEDIA_COMPOSITE_2026-11-03.md', 'docs/SERVE_OPERATIONS.md', 'docs/MODEL.md']
    paths += [str(p.relative_to(ROOT)) for p in (ROOT/'docs').glob('serve-*.json')]
    h.save(PACKAGE/'sources.json', {p: h.sha(ROOT/p) for p in sorted(set(paths)) if (ROOT/p).is_file()})
    h.save(PACKAGE/'sdk.json', sdk)
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    subprocess.run(['git', 'bundle', 'create', str(PACKAGE/'source.bundle'), '0af90eb8..HEAD'],
                   cwd=ROOT, timeout=60, check=True)
    protocol = h.read(PACKAGE/'candidate/generated/q4t/server/chat_protocol_sources.json')
    digest = hashlib.sha256(''.join(f'{p}:{protocol[p]}\n' for p in sorted(protocol)).encode()).hexdigest()
    h.save(PACKAGE/'candidate/diagnostic-identity.json', dict(binary_sha256=h.sha(PACKAGE/'candidate/q4t'),
        chat_protocol=dict(protocol_sha256=digest), candidate_identity_only=True,
        acceptance='PENDING GPU/business; not a release attestation'))
    plan = dict(schema_version=1, root=str(ROOT), limits=LIMITS, planned=PLANNED,
        source_commit=commit, candidate=str(PACKAGE/'candidate/q4t'),
        candidate_sha256=h.sha(PACKAGE/'candidate/q4t'), binary=h.identity(PACKAGE/'candidate/q4t'),
        sidecar=h.identity(PACKAGE/'candidate/diagnostic-identity.json'),
        old_hashes={n: h.sha(PACKAGE/'previous'/n) for n in a.NAMES},
        previous_binary_sha256=DEFAULT_SHA, source_manifest_sha256=h.sha(PACKAGE/'sources.json'),
        protocol_sha256=digest, model_dir=inherited['model_dir'], model_inputs=inherited['model_inputs'],
        sdk_directory=sdk['sdk_directory'], port=18110, performance_acceptance=False,
        validation_binary=h.identity(PACKAGE/'validation/q4t'),
        lifecycle_probe=h.identity(PACKAGE/'candidate/q4t_http_lifecycle_tests'),
        vision_probe=h.identity(PACKAGE/'candidate/q4t_vision_contract'),
        upload_probe=h.identity(PACKAGE/'candidate/q4t_vision_upload'),
        device_probe=h.identity(PACKAGE/'cpu/device_contract'), fixtures=str(PACKAGE/'fixtures'))
    h.save(PACKAGE/'plan.json', plan)
    h.seal(PACKAGE)
    print('PREPARED', plan['candidate_sha256'], flush=True)


def check():
    h.verify_seal(PACKAGE)
    plan = h.read(PACKAGE/'plan.json')
    h.require(plan['root'] == str(ROOT) and plan['limits'] == LIMITS and plan['planned'] == PLANNED,
              'frozen scope/budget changed')
    h.require(plan['source_manifest_sha256'] == h.sha(PACKAGE/'sources.json'), 'manifest changed')
    a.verify_hashes(ROOT, h.read(PACKAGE/'sources.json'))
    a.verify_hashes(ROOT, h.read(PACKAGE/'candidate/runtime-source.json'))
    a.verify_hashes(plan['model_dir'], plan['model_inputs'])
    for key in ('binary', 'lifecycle_probe', 'sidecar', 'validation_binary',
                'vision_probe', 'upload_probe', 'device_probe'):
        h.check_identity(plan[key])
    h.require(old.previous.previous.prior.sdk_identity() == h.read(PACKAGE/'sdk.json'), 'SDK changed')
    if (OUTPUT/'begin.json').exists():
        plan['started'] = plan['original_started'] = h.read(OUTPUT/'begin.json')['started']
    return plan


def verifier(plan, installed=False):
    if installed:
        from tools.serve_media_composite.installed_health import run
        run(plan, OUTPUT)
        return
    folder = OUTPUT/'native'; folder.mkdir()
    passed = cpu.command(folder, 'lifecycle', [plan['lifecycle_probe']['path']], timeout=90)
    passed = passed and '25 tests, 25 passed, 0 failed, 0 skipped' in (folder/'lifecycle.stdout').read_text()
    h.save(folder/'result.json', dict(passed=passed, required_tests=25,
        binary=plan['lifecycle_probe'], parent_stage_lease=str(OUTPUT/'lease'), model_loaded=False))
    h.require(passed, 'native lifecycle failure; no model attempt')
    device = OUTPUT/'native-device'; device.mkdir()
    a.require_idle_gpu(device/'gpu-before.json')
    try:
        passed = cpu.command(device, 'device', [plan['device_probe']['path']], timeout=60)
        h.require(passed, 'actual attention query contract')
        result = json.loads((device/'device.stdout').read_text())
        h.require(result['passed'] and result['rejections'] == 8 and not result['kernel_executed'],
                  'device contract coverage')
        h.save(device/'result.json', result)
    finally:
        a.require_idle_gpu(device/'gpu-after.json')
    from tools.serve_media_failure.verify import native
    native(plan, OUTPUT/'native-vision')
    from tools.serve_media_composite.verify import Run
    from tools.serve_lifecycle.observe import ObservationAbort
    try:
        Run(dict(plan, actual_attention_shared_bytes=result['attention_shared_bytes']),
            OUTPUT/'business').run()
    except ObservationAbort as error:
        raise a.EnvironmentAbort(str(error)) from error


def release(plan):
    result = OUTPUT/'business/result.json'
    h.require(h.read(result)['status'] == 'PASS_BOUNDED_MEDIA_COMPOSITE', 'business gate')
    caps = json.loads(h.read(OUTPUT/'business/model/capabilities.json')['raw'])
    metadata = h.read(PACKAGE/'previous/q4t.release.json')
    inherited = copy.deepcopy({k: metadata[k] for k in ('acceptance', 'acceptance_decision',
        'acceptance_decision_sha256', 'chat_protocol', 'deployment_completion', 'scope') if k in metadata})
    inherited['release'] = h.identity(PACKAGE/'previous/q4t.release.json')
    metadata.pop('deployment_completion', None)
    metadata.update(version='serve-media-composite-20261103', binary_sha256=plan['candidate_sha256'],
        source_commit=plan['source_commit'], tool_source_commit=plan['source_commit'],
        source_note='Shared media admission, decoder-owned quota, expanded context before slot selection; '
          'explicit media load failure closes startup; vision resource ownership and classified device '
          'errors; fixed host-memory responses; cooperative cancellation. Physical RAM is not reserved.',
        tested_source_manifest=str(PACKAGE/'candidate/runtime-source.json'),
        tested_source_manifest_sha256=h.sha(PACKAGE/'candidate/runtime-source.json'),
        build_cache=str(PACKAGE/'candidate/CMakeCache.txt'), build_cache_sha256=h.sha(PACKAGE/'candidate/CMakeCache.txt'),
        acceptance='ACCEPT_BOUNDED_MEDIA_COMPOSITE', acceptance_decision=str(result),
        acceptance_decision_sha256=h.sha(result), tool_acceptance=str(result), tool_acceptance_sha256=h.sha(result),
        previous_binary=str(PACKAGE/'previous/q4t'), previous_release=str(PACKAGE/'previous/q4t.release.json'),
        inherited_acceptance=inherited, implementation_capabilities=caps,
        operations_rules=h.identity(PACKAGE/'candidate/q4t.rules.yml'),
        rollback_operations_rules=h.identity(PACKAGE/'previous/q4t.rules.yml'),
        scope=dict(hardware='local Thor', heads=['full'], production='MTP k3, single8192/inflight8/queued2',
            media='Explicit experimental no-MTP, single8192/max-prefill2048; no explicit sequence-capacity combination.',
            validation='Native25, actual attention query/failure adapters, tiny CUDA ownership sweep/333 uploads; '
              'production text/strict JSON/JS real-project tool; red image/two identical frames, early admission '
              'and actual decoder quota errors, same-PID OOM/cancel recovery, classified fatal and startup errors.',
            boundaries='No complete multimodal quality, physical OOM/device damage/RAM hard bound, '
              'preemptive cancellation, new long-soak or performance claim.',
            installation_health='CLI/worker and same-ELF candidate HTTP reuse; no installed model start',
            performance_acceptance=False, numerical_evidence=str(PACKAGE/'numerical-reuse.json')))
    metadata['chat_protocol'].update(protocol_sha256=plan['protocol_sha256'], template=caps['template'],
        observability=caps['observability'], http=caps['http'],
        sdk_interoperability_acceptance=dict(node='24.18.0', openai_javascript='7.47.0',
            scope='Strict real project read plus SSE answer; no automatic retry'))
    directory = OUTPUT/'release'; directory.mkdir()
    h.save(directory/'q4t.release.json', metadata)
    h.save(directory/'binding.json', dict(old=plan['old_hashes'], new=dict(q4t=plan['candidate_sha256'],
        **{'q4t.release.json': h.sha(directory/'q4t.release.json')})))
    h.seal(directory)
    return directory


def bind():
    a.BASE, a.PACKAGE, a.OUTPUT = BASE, PACKAGE, OUTPUT
    a.MARKER = ROOT/'build/serve-media-composite-recovery.json'
    a.check, a.verifier, a.release = check, verifier, release


def execute_locked():
    # Preserve the established pair installation/rollback transaction. Its
    # older implementation has literal 1500/1800 deadlines; this batch binds
    # both checkpoints to its own predeclared 2400-second limit.
    h.require_guard(OUTPUT)
    plan = check()
    a.verify_hashes(ROOT/'build', plan['old_hashes'])
    h.require(not list((ROOT/'build').glob('*recovery*.json')), 'recovery pending')
    h.require(not (OUTPUT/'execution.json').exists(), 'no automatic retry')
    a.acceptance.install_interrupt_handlers()
    start = plan['original_started']
    state = dict(status='RUNNING', started=time.time(), original_started=start)
    installed = False
    try:
        a.require_idle_gpu(OUTPUT/'initial-gpu.json')
        verifier(plan)
        check()
        h.require(time.time() < start+LIMITS['seconds']-300, 'installation time reserve')
        directory = release(plan)
        pair = OUTPUT/'pair'; pair.mkdir()
        h.copy_checked(h.identity(plan['candidate']), pair/'q4t')
        h.copy_checked(h.identity(directory/'q4t.release.json'), pair/'q4t.release.json')
        binding = h.read(directory/'binding.json')
        states = a.state_pair(ROOT/'build', PACKAGE/'previous', pair,
                              binding['old'], binding['new'])
        h.require(states == binding['old'], 'default changed')
        h.save(a.MARKER, dict(stage=str(PACKAGE), action='install'))
        h.fsync_directory(a.MARKER.parent)
        installed = True
        for name in a.NAMES:
            h.replace_checked(pair/name, ROOT/'build'/name, states[name])
        h.verify_pair(ROOT/'build/q4t', ROOT/'build/q4t.release.json', plan['candidate_sha256'])
        verifier(plan, installed=True)
        a.require_idle_gpu(OUTPUT/'final-gpu.json')
        h.require(time.time() < start+LIMITS['seconds'], 'overall deadline')
        state['status'] = 'HEALTH_CHECKED_PENDING_GUARD'
    except BaseException as error:
        state.update(status='ENV_ABORT' if isinstance(error, a.EnvironmentAbort)
                     else 'FAILED', error=repr(error))
        if installed:
            try:
                a.restore(OUTPUT); state['rollback'] = 'RESTORED'
            except BaseException as recovery_error:
                state['rollback_error'] = repr(recovery_error)
        raise
    finally:
        state['finished'] = time.time()
        h.save(OUTPUT/'execution.json', state)


def execute():
    h.require_guard(OUTPUT)
    events = [json.loads(line) for line in (OUTPUT/'lease/events.jsonl').read_text().splitlines()]
    started = datetime.datetime.fromisoformat(next(r['utc'] for r in events if r['event'] == 'lease_acquired')).timestamp()
    h.save(OUTPUT/'begin.json', dict(started=started, limits=LIMITS))
    def expired(signum, frame):
        raise RuntimeError('composite stage deadline; cleanup reserve only')
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, max(.1, started+LIMITS['seconds']-90-time.time()))
    try:
        bind(); execute_locked()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


def finalize():
    check()
    h.require(time.time()-h.read(OUTPUT/'begin.json')['started'] <= LIMITS['seconds'], 'finalization deadline')
    bind(); a.finalize(); h.seal(OUTPUT)


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
        bind(); a.check = rollback_plan; a.restore(args.output.resolve())
        return
    globals()[args.action]()


if __name__ == '__main__': main()
