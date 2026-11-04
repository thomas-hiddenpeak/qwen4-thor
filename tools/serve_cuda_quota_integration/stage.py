"""Frozen short acceptance and the established guarded default-pair transaction."""
import argparse
import copy
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_cuda_quota_integration import common as c, cpu
from tools.serve_media_composite import stage as transaction

h, a = transaction.h, transaction.a
BASE = c.BASE
PACKAGE, OUTPUT = BASE / 'package', BASE / 'execution'
LIMITS = dict(generations=7, starts=3, controls=120, native=6, seconds=600)
DAY = ROOT / '.q4t-work/serve-soak-20261103/day'


def prepare(cpu_folder):
    c.require(not PACKAGE.exists() and not OUTPUT.exists(), 'preserve prior run')
    c.require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT),
              'commit reviewed implementation and driver first')
    built = cpu.check(cpu_folder)
    c.require(c.read(cpu_folder / 'tests/result.json')['passed'], 'CPU gates missing')
    driver = c.read(BASE / 'driver-cpu/result.json')
    c.require(driver['passed'] and driver['count'] == 5, 'driver gates missing')
    a.verify_hashes(ROOT, driver['sources'])
    previous = h.verify_pair(ROOT / 'build/q4t', ROOT / 'build/q4t.release.json', c.OLD)
    c.require(c.sha(ROOT / 'build/q4t.release.json') == c.SIDECAR, 'old release drift')
    old_sources = c.read(previous['tested_source_manifest'])
    current = c.sources()
    changed = sorted(name for name in old_sources if old_sources[name] != current[name])
    added = sorted(current.keys() - old_sources.keys())
    # Scope review prohibits changes to model arithmetic, tokenization, media,
    # kernel implementations or the previously accepted quota algorithm.
    allowed = {'CMakeLists.txt', 'cmake/ChatProtocolIdentity.cmake',
        'cmake/CudaCleanupBoundary.cmake', 'cmake/HostTestSources.cmake',
        'include/q4t/runtime/device_memory.h', 'src/runtime/startup_reservation.cpp',
        'src/main.cpp'}
    c.require(all(name in allowed or name.startswith(('include/q4t/server/',
        'src/server/')) for name in changed), 'runtime change exceeds reviewed scope')
    c.require(set(added) <= set(c.NEW_RUNTIME), 'unreviewed runtime files')
    core = c.read(ROOT / '.q4t-work/serve-cuda-quota-core-20261104/plan.json')
    for name in ('include/q4t/runtime/allocation_quota.h',
                 'include/q4t/runtime/reservation_owner.h'):
        c.require(c.sha(ROOT / name) == core['sources'][name], 'accepted core changed')
    PACKAGE.mkdir()
    for name in ('candidate', 'previous'):
        (PACKAGE / name).mkdir()
    for name in a.NAMES:
        h.copy_checked(h.identity(ROOT / 'build' / name), PACKAGE / 'previous' / name)
    h.copy_checked(h.identity(previous['build_cache']), PACKAGE / 'previous/CMakeCache.txt')
    for source, name in [('q4t', 'q4t'), ('lifecycle', 'q4t_http_lifecycle_tests'),
                         ('native', 'quota_wrapper_probe')]:
        h.copy_checked(built['binaries'][source], PACKAGE / 'candidate' / name)
    for name in ('CMakeCache.txt', 'compile_commands.json'):
        h.copy_checked(h.identity(cpu.ENGINE / name), PACKAGE / 'candidate' / name)
    shutil.copytree(cpu.ENGINE / 'generated', PACKAGE / 'candidate/generated')
    shutil.copytree(cpu_folder, PACKAGE / 'cpu')
    c.save(PACKAGE / 'candidate/runtime-source.json', current)
    for name in ('candidate', 'previous'):
        h.copy_checked(h.identity(ROOT / 'deploy/serve-ops/q4t.rules.yml'),
                       PACKAGE / name / 'q4t.rules.yml')
    files = subprocess.check_output(['git', 'ls-files', 'tools', 'cmake',
                                     'deploy/serve-ops'], cwd=ROOT, text=True).splitlines()
    files += ['docs/SERVE_CUDA_QUOTA_INTEGRATION_2026-11-04.md', 'docs/MODEL.md']
    files += [str(path.relative_to(ROOT)) for path in (ROOT / 'docs').glob('serve-*.json')]
    c.save(PACKAGE / 'sources.json', {name: c.sha(ROOT / name) for name in sorted(set(files))})
    model = c.read(ROOT / '.q4t-work/serve-media-composite-20261103/package/plan.json')
    a.verify_hashes(model['model_dir'], model['model_inputs'])
    protocol = c.read(PACKAGE / 'candidate/generated/q4t/server/chat_protocol_sources.json')
    digest = hashlib.sha256(''.join(f'{name}:{protocol[name]}\n'
        for name in sorted(protocol)).encode()).hexdigest()
    c.save(PACKAGE / 'numerical-reuse.json', dict(changed=changed, added=added,
        previous_manifest=c.identity(previous['tested_source_manifest']),
        current_manifest=c.identity(PACKAGE / 'candidate/runtime-source.json'),
        accepted_quota_core_unchanged=True, model_math_tokenizer_template_unchanged=True,
        performance_acceptance=False, new_http_quality_required=True))
    c.require(not (DAY / 'execution').exists(), 'original day unexpectedly executed')
    c.save(PACKAGE / 'plan.json', dict(root=str(ROOT), limits=LIMITS,
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT,
                                             text=True).strip(),
        candidate=str(PACKAGE / 'candidate/q4t'),
        binary=c.identity(PACKAGE / 'candidate/q4t'),
        candidate_sha256=c.sha(PACKAGE / 'candidate/q4t'), protocol_sha256=digest,
        previous_binary_sha256=c.OLD,
        old_hashes={name: c.sha(PACKAGE / 'previous' / name) for name in a.NAMES},
        lifecycle_probe=c.identity(PACKAGE / 'candidate/q4t_http_lifecycle_tests'),
        native_probe=c.identity(PACKAGE / 'candidate/quota_wrapper_probe'),
        model_dir=model['model_dir'], model_inputs=model['model_inputs'], port=18125,
        explicit_limit_mib=100352, original_day=c.identity(DAY / 'plan.json'),
        performance_acceptance=False, physical_ram_hard_limit=False))
    h.seal(PACKAGE)


def check():
    h.verify_seal(PACKAGE)
    plan = c.read(PACKAGE / 'plan.json')
    c.require(plan['root'] == str(ROOT) and plan['limits'] == LIMITS, 'foreign plan')
    a.verify_hashes(ROOT, c.read(PACKAGE / 'sources.json'))
    a.verify_hashes(ROOT, c.read(PACKAGE / 'candidate/runtime-source.json'))
    a.verify_hashes(plan['model_dir'], plan['model_inputs'])
    for key in ('binary', 'lifecycle_probe', 'native_probe', 'original_day'):
        c.require(c.identity(plan[key]['path']) == plan[key], 'identity changed: ' + key)
    c.require(not (DAY / 'execution').exists(), 'day execution outside short contract')
    if (OUTPUT / 'begin.json').exists():
        plan['started'] = plan['original_started'] = c.read(OUTPUT / 'begin.json')['started']
    return plan


def verifier(plan, installed=False):
    if not installed:
        from tools.serve_cuda_quota_integration.verify import Run
        Run(plan, OUTPUT / 'business').run()
        return
    c.require(c.sha(ROOT / 'build/q4t') == plan['candidate_sha256'] and
        c.read(OUTPUT / 'business/result.json')['status'] == 'PASS_BOUNDED_CUDA_QUOTA',
        'installed binary lacks accepted HTTP evidence')
    folder = OUTPUT / 'health'
    folder.mkdir()
    c.require(c.command(folder, 'version', [str(ROOT / 'build/q4t'), 'version'], 10),
              'installed CLI')
    c.require('Qwen4-Thor' in (folder / 'version.stdout').read_text(), 'version output')
    c.save(folder / 'result.json', dict(status='PASS_SAME_ELF_HTTP_AND_WORKER_REUSE',
        installed=c.identity(ROOT / 'build/q4t'),
        business=c.identity(OUTPUT / 'business/result.json'),
        new_model_starts=0, new_model_requests=0))


def release(plan):
    result = OUTPUT / 'business/result.json'
    c.require(c.read(result)['status'] == 'PASS_BOUNDED_CUDA_QUOTA', 'business gate')
    caps = __import__('json').loads(c.read(OUTPUT / 'business/auto-capabilities.json')['body'])
    metadata = c.read(PACKAGE / 'previous/q4t.release.json')
    inherited = copy.deepcopy({key: metadata[key] for key in ('acceptance',
        'acceptance_decision', 'acceptance_decision_sha256', 'chat_protocol',
        'deployment_completion', 'scope') if key in metadata})
    inherited['release'] = c.identity(PACKAGE / 'previous/q4t.release.json')
    metadata.pop('deployment_completion', None)
    metadata.update(version='serve-cuda-quota-integration-20261104',
        binary_sha256=plan['candidate_sha256'], source_commit=plan['source_commit'],
        tool_source_commit=plan['source_commit'], source_note='Process-lived requested CUDA '
        'byte quota across startup reservation and runtime allocation APIs. '
        'Async frees retain charge until proven synchronization; stream generations '
        'prevent stale credit. Explicit observability and recoverable refusal reason.',
        tested_source_manifest=str(PACKAGE / 'candidate/runtime-source.json'),
        tested_source_manifest_sha256=c.sha(PACKAGE / 'candidate/runtime-source.json'),
        build_cache=str(PACKAGE / 'candidate/CMakeCache.txt'),
        build_cache_sha256=c.sha(PACKAGE / 'candidate/CMakeCache.txt'),
        acceptance='ACCEPT_BOUNDED_CUDA_QUOTA_INTEGRATION',
        acceptance_decision=str(result), acceptance_decision_sha256=c.sha(result),
        tool_acceptance=str(result), tool_acceptance_sha256=c.sha(result),
        previous_binary=str(PACKAGE / 'previous/q4t'),
        previous_release=str(PACKAGE / 'previous/q4t.release.json'),
        inherited_acceptance=inherited, implementation_capabilities=caps,
        operations_rules=c.identity(PACKAGE / 'candidate/q4t.rules.yml'),
        rollback_operations_rules=c.identity(PACKAGE / 'previous/q4t.rules.yml'),
        scope=dict(hardware='local Thor', heads=['full'],
            production='auto/MTP k3/seq1; explicit/startup-plan off/no-budget/no-MTP/seq2',
            validation='CPU wrapper and host contracts; six native wrapper modes; '
                '25 native HTTP lifecycle checks; one quota startup refusal; '
                'seven real successful generations including project tool and citation.',
            boundaries='Project-linked requested bytes only. No external DSO, host heap, '
                'CUDA pool retention or physical RAM cap; no new media/SDK/long test.',
            performance_acceptance=False,
            installation_health='CLI plus exact-ELF HTTP/worker evidence reuse',
            numerical_evidence=str(PACKAGE / 'numerical-reuse.json')))
    metadata['chat_protocol'].update(protocol_sha256=plan['protocol_sha256'],
        template=caps['template'], observability=caps['observability'], http=caps['http'],
        sdk_interoperability_acceptance=dict(current='Python http.client/native parser',
            previous_client_evidence=inherited['release'],
            sdk_protocol_implementation_unchanged=True))
    folder = OUTPUT / 'release'
    folder.mkdir()
    c.save(folder / 'q4t.release.json', metadata)
    c.save(folder / 'binding.json', dict(old=plan['old_hashes'],
        new=dict(q4t=plan['candidate_sha256'],
                 **{'q4t.release.json': c.sha(folder / 'q4t.release.json')})))
    h.seal(folder)
    return folder


def bind():
    a.BASE, a.PACKAGE, a.OUTPUT = BASE, PACKAGE, OUTPUT
    a.MARKER = ROOT / 'build/serve-cuda-quota-integration-recovery.json'
    a.check, a.verifier, a.release = check, verifier, release
    transaction.BASE, transaction.PACKAGE, transaction.OUTPUT = BASE, PACKAGE, OUTPUT
    transaction.LIMITS = LIMITS
    transaction.check, transaction.verifier = check, verifier
    transaction.release, transaction.bind = release, bind


def execute():
    bind()
    transaction.execute()


def finalize():
    check()
    c.require(time.time() - c.read(OUTPUT / 'begin.json')['started'] <= LIMITS['seconds'],
              'stage finalization deadline')
    bind()
    a.finalize()
    h.seal(OUTPUT)


def rollback(output):
    h.verify_seal(PACKAGE)
    plan = c.read(PACKAGE / 'plan.json')
    c.require(plan['old_hashes']['q4t'] == c.OLD and plan['root'] == str(ROOT),
              'foreign rollback')
    h.verify_pair(PACKAGE / 'previous/q4t', PACKAGE / 'previous/q4t.release.json',
                  c.OLD, PACKAGE / 'previous/CMakeCache.txt')
    bind()
    a.check = lambda: plan
    a.restore(output)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'launch', 'execute', 'finalize',
                                           'check', 'rollback'])
    parser.add_argument('--cpu', type=Path, default=BASE / 'cpu')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.action == 'prepare':
        prepare(args.cpu)
    elif args.action == 'launch':
        check()
        OUTPUT.mkdir()
        raise SystemExit(subprocess.call(['/usr/bin/python3', '-B',
            str(ROOT / 'tools/evalscope/gpu_exclusive.py'), '--output', str(OUTPUT / 'lease'),
            '--', '/usr/bin/python3', '-B', str(Path(__file__).resolve()), 'execute'], cwd=ROOT))
    elif args.action == 'rollback':
        c.require(args.output is not None, 'fresh rollback lease directory required')
        rollback(args.output)
    else:
        globals()[args.action]()
