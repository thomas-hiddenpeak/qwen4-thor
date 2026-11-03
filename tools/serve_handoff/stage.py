"""Frozen handoff regression and guarded default-pair transaction."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_handoff import cpu
from tools.serve_media_composite import stage as transaction

h, a = transaction.h, transaction.a
BASE, PACKAGE, OUTPUT = cpu.BASE, cpu.BASE/'package', cpu.BASE/'execution'
LIMITS = dict(generations=10, negatives=3, starts=1, controls=40, seconds=600)
OLD = '280b95dceabe5a3b1fd794019102de0dc9f0b62e8a3aa9fcc30eb5c57fdfd793'
SIDECAR = '18c9d2c50c847c90c6b033b4350fa0cf10c59777bf3a9dacd01391250a341f19'
CHANGED = {'include/q4t/server/structured_admission.h', 'include/q4t/server/chat_server.h',
    'include/q4t/server/http_response.h', 'src/server/chat_generation.cpp',
    'src/server/http_response.cpp', 'src/server/chat_capabilities.cpp'}


def prepare():
    h.require(not PACKAGE.exists() and not OUTPUT.exists(), 'preserve prior run')
    h.require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT),
              'commit reviewed source first')
    cpu.check()
    h.require(h.read(cpu.CPU/'tests/result.json')['passed'], 'CPU acceptance missing')
    driver = h.read(BASE/'driver-corrected/result.json')
    h.require(driver['passed'] and driver['effective_tests'] == 4, 'driver gate')
    a.verify_hashes(ROOT, driver['sources'])
    previous = h.verify_pair(ROOT/'build/q4t', ROOT/'build/q4t.release.json', OLD)
    h.require(h.sha(ROOT/'build/q4t.release.json') == SIDECAR, 'old sidecar changed')
    original = h.read(previous['tested_source_manifest'])
    current = cpu.sources()
    h.require(original.keys() == current.keys() and
              {p for p in original if original[p] != current[p]} == CHANGED,
              'unreviewed runtime changes')
    PACKAGE.mkdir()
    for name in ('candidate', 'previous'): (PACKAGE/name).mkdir()
    for name in a.NAMES:
        h.copy_checked(h.identity(ROOT/'build'/name), PACKAGE/'previous'/name)
    h.copy_checked(h.identity(previous['build_cache']), PACKAGE/'previous/CMakeCache.txt')
    for name in ('q4t', 'q4t_http_lifecycle_tests', 'CMakeCache.txt', 'compile_commands.json'):
        h.copy_checked(h.identity(cpu.ENGINE/name), PACKAGE/'candidate'/name)
    shutil.copytree(cpu.ENGINE/'generated', PACKAGE/'candidate/generated')
    shutil.copytree(cpu.CPU, PACKAGE/'cpu')
    h.save(PACKAGE/'candidate/runtime-source.json', current)
    for name in ('candidate', 'previous'):
        h.copy_checked(h.identity(ROOT/'deploy/serve-ops/q4t.rules.yml'),
                       PACKAGE/name/'q4t.rules.yml')
    files = subprocess.check_output(['git', 'ls-files', 'tools', 'cmake', 'deploy/serve-ops'],
                                    cwd=ROOT, text=True).splitlines()
    files += ['docs/SERVE_STRUCTURED_HANDOFF_2026-11-03.md', 'docs/MODEL.md']
    files += [str(p.relative_to(ROOT)) for p in (ROOT/'docs').glob('serve-*.json')]
    h.save(PACKAGE/'sources.json', {p:h.sha(ROOT/p) for p in sorted(set(files))})
    model = h.read(ROOT/'.q4t-work/serve-media-composite-20261103/package/plan.json')
    a.verify_hashes(model['model_dir'], model['model_inputs'])
    protocol = h.read(PACKAGE/'candidate/generated/q4t/server/chat_protocol_sources.json')
    digest = hashlib.sha256(''.join(f'{p}:{protocol[p]}\n' for p in sorted(protocol)).encode()).hexdigest()
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    h.save(PACKAGE/'numerical-reuse.json', dict(changed=sorted(CHANGED),
        runtime_manifest=h.identity(PACKAGE/'candidate/runtime-source.json'),
        previous_manifest=h.identity(previous['tested_source_manifest']),
        model_math_tokenizer_template_unchanged=True, performance_acceptance=False,
        new_http_quality_required=True))
    h.save(PACKAGE/'plan.json', dict(root=str(ROOT), limits=LIMITS,
        planned=dict(starts=1, generations=9, negatives=1), source_commit=commit,
        candidate=str(PACKAGE/'candidate/q4t'), binary=h.identity(PACKAGE/'candidate/q4t'),
        candidate_sha256=h.sha(PACKAGE/'candidate/q4t'), protocol_sha256=digest,
        previous_binary_sha256=OLD, old_hashes={n:h.sha(PACKAGE/'previous'/n) for n in a.NAMES},
        lifecycle_probe=h.identity(PACKAGE/'candidate/q4t_http_lifecycle_tests'),
        model_dir=model['model_dir'], model_inputs=model['model_inputs'], port=18121,
        performance_acceptance=False))
    h.seal(PACKAGE)


def check():
    h.verify_seal(PACKAGE)
    plan = h.read(PACKAGE/'plan.json')
    h.require(plan['root'] == str(ROOT) and plan['limits'] == LIMITS, 'foreign plan')
    a.verify_hashes(ROOT, h.read(PACKAGE/'sources.json'))
    a.verify_hashes(ROOT, h.read(PACKAGE/'candidate/runtime-source.json'))
    a.verify_hashes(plan['model_dir'], plan['model_inputs'])
    for key in ('binary', 'lifecycle_probe'): h.check_identity(plan[key])
    if (OUTPUT/'begin.json').exists():
        plan['started'] = plan['original_started'] = h.read(OUTPUT/'begin.json')['started']
    return plan


def verifier(plan, installed=False):
    if installed:
        h.require(h.sha(ROOT/'build/q4t') == plan['candidate_sha256'] and
            h.read(OUTPUT/'business/result.json')['status'] == 'PASS_BOUNDED_STRUCTURED_HANDOFF',
            'installed identity or HTTP evidence differs')
        h.require(h.read(OUTPUT/'business/command.json')['binary'] == plan['binary'],
                  'HTTP used another ELF')
        folder = OUTPUT/'health'; folder.mkdir()
        passed = cpu.command(folder, 'version', [str(ROOT/'build/q4t'), 'version'], timeout=10)
        h.require(passed and 'Qwen4-Thor' in (folder/'version.stdout').read_text(), 'installed CLI')
        h.save(folder/'result.json', dict(status='PASS_SAME_ELF_HTTP_AND_WORKER_REUSE',
            business=h.identity(OUTPUT/'business/result.json'),
            workers=h.identity(OUTPUT/'business/final-metrics.json'),
            installed=h.identity(ROOT/'build/q4t'), new_model_starts=0, new_model_requests=0))
        return
    folder = OUTPUT/'native'; folder.mkdir()
    passed = cpu.command(folder, 'lifecycle', [plan['lifecycle_probe']['path']], timeout=90)
    h.require(passed and '25 tests, 25 passed, 0 failed, 0 skipped' in
        (folder/'lifecycle.stdout').read_text(), 'native HTTP regression')
    h.save(folder/'result.json', dict(passed=True, tests=25, binary=plan['lifecycle_probe']))
    from tools.serve_handoff.verify import Run
    Run(plan, OUTPUT/'business').run()


def release(plan):
    result = OUTPUT/'business/result.json'
    h.require(h.read(result)['status'] == 'PASS_BOUNDED_STRUCTURED_HANDOFF', 'business gate')
    caps = json.loads(h.read(OUTPUT/'business/capabilities.json')['body'])
    metadata = h.read(PACKAGE/'previous/q4t.release.json')
    inherited = copy.deepcopy({k:metadata[k] for k in ('acceptance', 'acceptance_decision',
        'acceptance_decision_sha256', 'chat_protocol', 'deployment_completion', 'scope') if k in metadata})
    inherited['release'] = h.identity(PACKAGE/'previous/q4t.release.json')
    metadata.pop('deployment_completion', None)
    metadata.update(version='serve-structured-handoff-20261103',
        binary_sha256=plan['candidate_sha256'], source_commit=plan['source_commit'],
        tool_source_commit=plan['source_commit'], source_note='Terminal handoff waits for full '
        'structured-resource destruction. Running work still rejects; waiting is cancellable and '
        'bounded by existing inflight/request limits. No early ownership release.',
        tested_source_manifest=str(PACKAGE/'candidate/runtime-source.json'),
        tested_source_manifest_sha256=h.sha(PACKAGE/'candidate/runtime-source.json'),
        build_cache=str(PACKAGE/'candidate/CMakeCache.txt'),
        build_cache_sha256=h.sha(PACKAGE/'candidate/CMakeCache.txt'),
        acceptance='ACCEPT_BOUNDED_STRUCTURED_HANDOFF', acceptance_decision=str(result),
        acceptance_decision_sha256=h.sha(result), tool_acceptance=str(result),
        tool_acceptance_sha256=h.sha(result), previous_binary=str(PACKAGE/'previous/q4t'),
        previous_release=str(PACKAGE/'previous/q4t.release.json'), inherited_acceptance=inherited,
        implementation_capabilities=caps,
        operations_rules=h.identity(PACKAGE/'candidate/q4t.rules.yml'),
        rollback_operations_rules=h.identity(PACKAGE/'previous/q4t.rules.yml'),
        scope=dict(hardware='local Thor', heads=['full'], production='MTP k3, seq1/8192/inflight8/queued2',
            validation='CPU deterministic terminal/ownership/cancellation; native HTTP25; ordinary JSON/SSE, '
            'strict JSON immediately followed by actual project tools, incomplete-error and SSE recovery.',
            inherited='Unchanged model math, templates and media implementation; earlier scoped evidence retained.',
            boundaries='No new media/SDK/long-soak, restart, physical RAM or performance acceptance.',
            installation_health='CLI plus exact-ELF HTTP/grammar-worker evidence reuse',
            performance_acceptance=False, numerical_evidence=str(PACKAGE/'numerical-reuse.json')))
    metadata['chat_protocol'].update(protocol_sha256=plan['protocol_sha256'], template=caps['template'],
        observability=caps['observability'], http=caps['http'],
        sdk_interoperability_acceptance=dict(current='Python http.client with native client parser',
            previous_client_evidence=inherited['release'], sdk_protocol_implementation_unchanged=True))
    folder = OUTPUT/'release'; folder.mkdir()
    h.save(folder/'q4t.release.json', metadata)
    h.save(folder/'binding.json', dict(old=plan['old_hashes'], new=dict(q4t=plan['candidate_sha256'],
        **{'q4t.release.json': h.sha(folder/'q4t.release.json')})))
    h.seal(folder)
    return folder


def bind():
    a.BASE, a.PACKAGE, a.OUTPUT = BASE, PACKAGE, OUTPUT
    a.MARKER = ROOT/'build/serve-structured-handoff-recovery.json'
    a.check, a.verifier, a.release = check, verifier, release
    # Reuse the reviewed pair transaction with this phase's exact deadline.
    transaction.BASE, transaction.PACKAGE, transaction.OUTPUT = BASE, PACKAGE, OUTPUT
    transaction.LIMITS = LIMITS
    transaction.check, transaction.verifier, transaction.release, transaction.bind = check, verifier, release, bind


def execute():
    bind()
    transaction.execute()


def finalize():
    check()
    h.require(time.time()-h.read(OUTPUT/'begin.json')['started'] <= LIMITS['seconds'],
              'phase finalization deadline')
    bind(); a.finalize(); h.seal(OUTPUT)


def rollback(output):
    h.verify_seal(PACKAGE)
    plan = h.read(PACKAGE/'plan.json')
    h.require(plan['old_hashes']['q4t'] == OLD and plan['root'] == str(ROOT), 'foreign rollback')
    h.verify_pair(PACKAGE/'previous/q4t', PACKAGE/'previous/q4t.release.json', OLD,
                  PACKAGE/'previous/CMakeCache.txt')
    bind(); a.check = lambda: plan
    a.restore(output)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'launch', 'execute', 'finalize', 'rollback'))
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.action == 'launch':
        check(); OUTPUT.mkdir()
        raise SystemExit(subprocess.call(['/usr/bin/python3', '-B',
            str(ROOT/'tools/evalscope/gpu_exclusive.py'), '--output', str(OUTPUT/'lease'), '--',
            '/usr/bin/python3', '-B', str(Path(__file__).resolve()), 'execute'], cwd=ROOT))
    if args.action == 'rollback':
        h.require(args.output is not None, 'fresh lease directory required')
        rollback(args.output)
    else:
        globals()[args.action]()
