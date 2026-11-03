"""One corrected unhanded-plan probe; reuse 16 passes and finish pending business."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_backend_cleanup import stage as original, verify as v

h, a = original.h, original.a
ORIGINAL_OUTPUT = original.OUTPUT
BASE = ROOT/'.q4t-work/serve-backend-cleanup-correction-20261103'
PACKAGE, OUTPUT = BASE/'package', BASE/'execution'
PROBE_SOURCE = 'tools/serve_backend_cleanup/cleanup_probe.cpp'
LIMITS = dict(generations=10, negatives=6, starts=7, probes=1, observations=256, seconds=1800)
CUMULATIVE_PROBES = 18


def audit_original():
    h.verify_seal(original.PACKAGE); h.verify_seal(ORIGINAL_OUTPUT)
    h.require(h.read(ORIGINAL_OUTPUT/'execution.json')['status'] == 'FAILED' and
              h.read(ORIGINAL_OUTPUT/'execution.json')['error'] == "RuntimeError('probe exit code')",
              'unexpected original failure')
    h.require(not (ORIGINAL_OUTPUT/'business').exists(), 'original HTTP must be unstarted')
    guard = h.read(ORIGINAL_OUTPUT/'lease/exit.json')
    h.require(guard['status'] == 'COMMAND_FAILED' and guard['child_returncode'] == 1 and
              'ENV_ABORT' not in (ORIGINAL_OUTPUT/'lease/events.jsonl').read_text(), 'original guard')
    native = h.read(ORIGINAL_OUTPUT/'native/result.json')
    h.require(native['passed'] and native['required_tests'] == 25, 'native proof absent')
    folder = ORIGINAL_OUTPUT/'native-cleanup'
    h.require(h.read(folder/'mtp-lifecycle-exit.json')['returncode'] == 0 and
              'MTP lifecycle: PASS' in (folder/'mtp-lifecycle.stdout').read_text(), 'MTP proof')
    rows = []
    for name, mode, api, phase, expected, production in v.probe_cases():
        case = folder/name
        rc = h.read(case/'exit.json')['returncode']
        out, err = (case/'stdout').read_text(), (case/'stderr').read_text()
        if name != 'unhanded-success':
            v.probe_outcome(rc, out, err, expected=expected, api=api, phase=phase, production=production)
        else:
            h.require(rc == 2 and '"error":"plan_incomplete"' in err and
                      '"freed":1' in err and '"release_unknown":0' in err,
                      'original failure differs from frozen fixture mismatch')
        rows.append(dict(name=name, accepted=name != 'unhanded-success',
            command=h.identity(case/'command.json'), stdout=h.identity(case/'stdout'),
            stderr=h.identity(case/'stderr'), exit=h.identity(case/'exit.json')))
    return dict(passed=True, native_tests=25, mtp_lifecycle_processes=1,
        actual_mtp_load_attempts_with_weights=3, probes=rows, reused_passed_probes=16,
        original_failed_probe='unhanded-success', old_probes=17,
        old_full_server_starts=0, old_generation_attempts=0,
        original_stage_remains_failed=True)


def prepare():
    h.require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT),
              'commit correction before freezing')
    h.require(not PACKAGE.exists(), 'preserve correction package')
    audit = audit_original()
    built = h.read(BASE/'cpu/build-result.json')
    h.require(built['passed'] and built['zero_warnings'], 'corrected probe build')
    h.check_identity(built['binary']); h.check_identity(built['source'])
    h.require(h.read(BASE/'cpu/test-result.json')['passed'], 'correction CPU checks')
    original_sources = h.read(original.PACKAGE/'sources.json')
    changed = [p for p, digest in original_sources.items() if h.sha(ROOT/p) != digest]
    h.require(changed == [PROBE_SOURCE], 'unexpected original source change')
    a.verify_hashes(ROOT, h.read(original.PACKAGE/'candidate/runtime-source.json'))
    started = h.read(ORIGINAL_OUTPUT/'begin.json')['started']
    h.require(time.time() < started+1800-700, 'original time reserve insufficient')
    PACKAGE.mkdir(); shutil.copytree(BASE/'cpu', PACKAGE/'cpu')
    h.copy_checked(built['binary'], PACKAGE/'q4t_cleanup_probe')
    h.save(PACKAGE/'reused-evidence.json', audit)
    paths = [PROBE_SOURCE, 'tools/serve_backend_cleanup/correction.py',
             'tools/serve_backend_cleanup/test_correction.py',
             'docs/SERVE_BACKEND_CLEANUP_CORRECTION_2026-11-03.md']
    h.save(PACKAGE/'sources.json', {p:h.sha(ROOT/p) for p in paths})
    diff = subprocess.check_output(['git', 'diff', '601fa1b3', '--', PROBE_SOURCE], cwd=ROOT, text=True)
    h.save(PACKAGE/'probe-source-review.json', dict(diff=diff,
        only_failed_probe_assertion_changed=True, runtime_elf_unchanged=True,
        reason='Successful unhanded free leaves plan_incomplete; assert freed/unknown/live counters and finalized inactive state, not overall startup success. Reuse independent cases in the original probe ELF.'))
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    h.save(PACKAGE/'plan.json', dict(source_commit=commit, started=started,
        limits=LIMITS, cumulative_probes=CUMULATIVE_PROBES,
        original_package=h.identity(original.PACKAGE/'sealed-files.json'),
        original_execution=h.identity(ORIGINAL_OUTPUT/'sealed-files.json'),
        original_limits_unchanged=original.LIMITS, original_plan_remains_failed=True,
        additional_probe_attempts=1, new_server_starts=7, new_generations=10,
        probe=h.identity(PACKAGE/'q4t_cleanup_probe')))
    h.seal(PACKAGE)


def check():
    h.verify_seal(PACKAGE); saved = h.read(PACKAGE/'plan.json')
    h.check_identity(saved['original_package']); h.check_identity(saved['original_execution'])
    h.require(saved['limits'] == LIMITS and saved['cumulative_probes'] == 18,
              'correction budget changed')
    a.verify_hashes(ROOT, h.read(PACKAGE/'sources.json'))
    old_sources = h.read(original.PACKAGE/'sources.json')
    a.verify_hashes(ROOT, {p:d for p,d in old_sources.items() if p != PROBE_SOURCE})
    a.verify_hashes(ROOT, h.read(original.PACKAGE/'candidate/runtime-source.json'))
    plan = h.read(original.PACKAGE/'plan.json')
    a.verify_hashes(plan['model_dir'], plan['model_inputs'])
    for key in ('binary', 'sidecar', 'validation_binary', 'lifecycle_probe',
                'cleanup_probe', 'cleanup_validation_probe', 'mtp_lifecycle_probe'):
        h.check_identity(plan[key])
    h.check_identity(saved['probe'])
    h.require(original.previous.previous.prior.sdk_identity() == h.read(original.PACKAGE/'sdk.json'), 'SDK changed')
    h.require(audit_original() == h.read(PACKAGE/'reused-evidence.json'), 'old evidence changed')
    plan.update(started=saved['started'], original_started=saved['started'],
                tool_source_commit=saved['source_commit'], corrected_probe=saved['probe'])
    return plan


def verifier(plan, installed=False):
    if installed:
        from tools.serve_backend_cleanup.installed_health import run
        run(plan, OUTPUT); return
    case = OUTPUT/'corrected-probe'; case.mkdir()
    argv = [plan['corrected_probe']['path'], 'reservation-release']
    h.save(case/'command.json', dict(argv=argv, binary=plan['corrected_probe'],
        expected_exit=0, repeat_reason='only the failed assertion corrected'))
    completed = subprocess.run(argv, cwd=ROOT, env=v.environment(),
        capture_output=True, text=True, timeout=45)
    (case/'stdout').write_text(completed.stdout); (case/'stderr').write_text(completed.stderr)
    h.save(case/'exit.json', dict(returncode=completed.returncode))
    v.probe_outcome(completed.returncode, completed.stdout, completed.stderr,
        expected=0, api=None, phase=None, production=False)
    h.save(case/'result.json', dict(passed=True, original_plan_remains_failed=True,
        new_probe_attempts=1, cumulative_probe_attempts=18, other_16_not_repeated=True))
    a.require_idle_gpu(case/'gpu-after.json')
    try:
        v.Run(plan, OUTPUT/'business').run()
    except v.observe.ObservationAbort as error:
        raise a.EnvironmentAbort(str(error)) from error


def release(plan):
    result = OUTPUT/'business/result.json'
    h.require(h.read(result)['status'] == 'PASS_BOUNDED_BACKEND_CLEANUP', 'business gate')
    caps = json.loads(h.read(OUTPUT/'business/model/capabilities.json')['raw'])
    metadata = h.read(original.PACKAGE/'previous/q4t.release.json')
    inherited = copy.deepcopy({k:metadata[k] for k in ('acceptance', 'acceptance_decision',
        'acceptance_decision_sha256', 'chat_protocol', 'deployment_completion', 'scope') if k in metadata})
    inherited['release'] = h.identity(original.PACKAGE/'previous/q4t.release.json')
    metadata.pop('deployment_completion', None)
    metadata.update(version='serve-backend-cleanup-20261103', binary_sha256=plan['candidate_sha256'],
        source_commit=plan['source_commit'], tool_source_commit=plan['tool_source_commit'],
        source_note='Requested MTP load failure stops both startup modes. Default q4t direct CUDA cleanup/completion failure terminates without unsafe unwind; explicitly handled serving free retains its stop/drain/error protocol. No external shared-library or complete media-allocation coverage; faults simulate API status only.',
        tested_source_manifest=str(original.PACKAGE/'candidate/runtime-source.json'),
        tested_source_manifest_sha256=h.sha(original.PACKAGE/'candidate/runtime-source.json'),
        build_cache=str(original.PACKAGE/'candidate/CMakeCache.txt'), build_cache_sha256=h.sha(original.PACKAGE/'candidate/CMakeCache.txt'),
        acceptance='ACCEPT_BOUNDED_BACKEND_CLEANUP', acceptance_decision=str(result),
        acceptance_decision_sha256=h.sha(result), tool_acceptance=str(result), tool_acceptance_sha256=h.sha(result),
        previous_binary=str(original.PACKAGE/'previous/q4t'), previous_release=str(original.PACKAGE/'previous/q4t.release.json'),
        inherited_acceptance=inherited, implementation_capabilities=caps,
        operations_rules=h.identity(original.PACKAGE/'candidate/q4t.rules.yml'),
        rollback_operations_rules=h.identity(original.PACKAGE/'previous/q4t.rules.yml'),
        scope=dict(hardware='local Thor', heads=['full'], production='MTP k3, single8192/inflight8/queued2',
            validation='Original native25, real MTP lifecycle and 16 probes reused; last probe expectation corrected and replayed (18 total probe attempts, original plan FAILED retained);  production JSON/SSE/strict JSON/JS project-tool loop; legacy MTP rejection, four internal shutdown release failures, handled free SSE error.',
            boundaries='Default q4t direct CUDA calls only, excluding external shared-library internals and research/standalone binaries. No complete allocation/media guarantee, physical OOM/device corruption/RAM hard bound, new long-soak or performance claim.',
            installation_health='CLI/worker and same-ELF candidate HTTP reuse; no installed model start',
            performance_acceptance=False, numerical_evidence=str(original.PACKAGE/'numerical-reuse.json')))
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
    a.BASE, a.PACKAGE, a.OUTPUT = BASE, original.PACKAGE, OUTPUT
    a.MARKER = ROOT/'build/serve-backend-cleanup-correction-recovery.json'
    a.check, a.verifier, a.release = check, verifier, release


def execute():
    h.require_guard(OUTPUT); plan = check()
    h.save(OUTPUT/'begin.json', dict(started=plan['started'], limits=LIMITS,
                                   original_wall_deadline_preserved=True))
    remaining = plan['started']+1800-90-time.time()
    h.require(remaining > 0, 'original deadline')
    def expired(signum, frame): raise RuntimeError('original deadline; cleanup reserve only')
    signal.signal(signal.SIGALRM, expired); signal.setitimer(signal.ITIMER_REAL, remaining)
    try: bind(); a.execute()
    finally: signal.setitimer(signal.ITIMER_REAL, 0)


def finalize():
    plan = check()
    h.require(time.time()-plan['started'] <= 1800, 'original wall deadline')
    bind(); a.finalize(); h.seal(OUTPUT)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'launch', 'execute', 'finalize'))
    args = parser.parse_args()
    if args.action == 'launch':
        check(); OUTPUT.mkdir(mode=0o700)
        raise SystemExit(subprocess.call(['/usr/bin/python3', '-B', str(ROOT/'tools/evalscope/gpu_exclusive.py'),
            '--output', str(OUTPUT/'lease'), '--', '/usr/bin/python3', '-B', str(Path(__file__)), 'execute'], cwd=ROOT))
    globals()[args.action]()


if __name__ == '__main__': main()
