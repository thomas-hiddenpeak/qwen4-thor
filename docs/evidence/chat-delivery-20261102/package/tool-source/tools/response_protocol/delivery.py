"""Deliver the accepted 2026-11-02 chat candidate; keep services stopped.

prepare/check/finalize are read-only with respect to the installed pair.
deploy and rollback require one complete GPU lease. Known partial old/new
pairs can be recovered; unknown files and foreign markers are never changed.
"""
import argparse
import copy
from pathlib import Path
import shutil
import subprocess
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'tools/verify/startup_delivery'),
               str(ROOT / 'tools/evalscope')]
import deploy as h
import nvml_process_query
from acceptance import install_interrupt_handlers

BASE = ROOT / '.q4t-work/chat-delivery-20261102'
PACKAGE = BASE / 'package'
DEPLOYMENT = BASE / 'deployment'
MARKER = ROOT / 'build/chat-delivery-recovery.json'
PREP = ROOT / '.q4t-work/chat-integration-prep-20261102'
CANDIDATE = '830694ce26621cdd852b9d0b064d42c217958e17c9444935dd9298ebcb645f51'
PREVIOUS = '0c5ab1de85ff68f9d757ac3bfad04187756a1b2c6634108c3112a5861c209483'
OLD_SIDECAR = 'e6fdd13b7d140926ed4a0d42bcda37b6ec6689305960e3deab03b3f73dc294d6'
CONTRACT = ROOT / 'docs/CHAT_DELIVERY_2026-11-02.md'


def copy_file(source, relative):
    target = PACKAGE / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    h.copy_checked(h.identity(source), target)


def verify_archive(directory):
    # Published archive READMEs are intentionally outside their raw-file seals.
    rows = h.read(directory / 'sealed-files.json')
    for name, digest in rows.items():
        path = (directory / name).resolve(strict=True)
        h.require(path.is_relative_to(directory) and h.sha(path) == digest,
                  'accepted evidence changed: ' + name)
    return dict(count=len(rows), manifest=h.identity(directory / 'sealed-files.json'))


def prepare():
    h.require(not BASE.exists(), 'stage already exists; no implicit retry')
    old = h.verify_pair(ROOT / 'build/q4t', ROOT / 'build/q4t.release.json', PREVIOUS)
    h.require(h.sha(ROOT / 'build/q4t.release.json') == OLD_SIDECAR,
              'default metadata changed')
    frozen = ROOT / 'docs/evidence/chat-business-runtime-20261102'
    accepted = h.read(frozen / 'utf8-correction/result.json')
    plan = h.read(frozen / 'corrected-plan.json')
    h.require(accepted['status'] == 'PASS_BOUNDED_CHAT_INTEGRATION' and
              accepted['candidate']['sha256'] == CANDIDATE and
              accepted['protocol_sha256'] == plan['protocol_sha256'],
              'accepted candidate binding')
    for name, digest in plan['sources'].items():
        h.require(h.sha(ROOT / name) == digest, 'accepted source changed: ' + name)
    for item in plan['artifacts'].values():
        h.require(h.sha(item['path']) == item['sha256'], 'accepted artifact changed')
    for name, digest in plan['model_inputs'].items():
        h.require(h.sha(Path(plan['model_dir']) / name) == digest,
                  'accepted model configuration changed')
    h.require(h.read(frozen / 'utf8-correction/lease/exit.json')['status'] ==
              'COMPLETED', 'accepted guard did not complete')
    archives = {name: verify_archive(ROOT / 'docs/evidence' / name) for name in
                ['chat-business-acceptance-20261102', 'chat-business-runtime-20261102']}
    build = h.read(PREP / 'development.json')
    for path, digest in build['artifacts_sha256'].items():
        h.require(h.sha(ROOT / path) == digest, 'build provenance changed')
    runtime = h.read(PREP / 'runtime-source.json')
    for name, digest in runtime.items():
        h.require(h.sha(ROOT / name) == digest, 'runtime source changed')
    BASE.mkdir(mode=0o700)
    PACKAGE.mkdir()
    DEPLOYMENT.mkdir()
    for name in ['q4t', 'q4t.release.json']:
        copy_file(ROOT / 'build' / name, 'previous/' + name)
    copy_file(old['build_cache'], 'previous/CMakeCache.txt')
    copy_file(old['tested_source_manifest'], 'previous/runtime-source.json')
    copy_file(PREP / 'q4t-candidate', 'candidate/q4t')
    copy_file(PREP / 'candidate-CMakeCache.txt', 'candidate/CMakeCache.txt')
    for name in ['runtime-source.json', 'chat_protocol_sources.json',
                 'chat_protocol_identity.h', 'protocol-identity.json',
                 'development.json', 'build-command.json',
                 'candidate-link-command.json']:
        copy_file(PREP / name, 'candidate/' + name)
    for name in runtime:
        copy_file(ROOT / name, 'tested-source/' + name)
    for name in ['corrected-plan.json', 'utf8-correction/result.json']:
        copy_file(frozen / name, 'evidence/' + name)
    copy_file(CONTRACT, 'acceptance-contract.md')
    copy_file(ROOT / 'docs/CHAT_BUSINESS_ACCEPTANCE_2026-11-02.md',
              'business-acceptance.md')
    for name in ['recovery-tests.log', 'recovery-tests.json']:
        copy_file(ROOT / '.q4t-work/chat-delivery-prep-20261102' / name,
                  'evidence/' + name)
    h.require(h.read(PACKAGE / 'evidence/recovery-tests.json')['returncode'] == 0,
              'delivery recovery contracts failed')
    h.save(PACKAGE / 'evidence/accepted-archives.json', archives)
    artifact = Path(old['subset_artifact'])
    h.verify_seal(artifact.parent)
    h.require(h.sha(artifact / 'manifest.json') == old['subset_manifest_sha256'],
              'previous subset manifest changed')
    shutil.copytree(artifact, PACKAGE / 'artifact')
    for name in ['head.bf16', 'token_ids.bin', 'manifest.json']:
        h.require(h.sha(artifact / name) == h.sha(PACKAGE / 'artifact' / name),
                  'subset copy changed')
    source_commit = subprocess.check_output(
        ['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    # Git captures every tracked dependency; the two delivery files are also
    # independently sealed, so rollback need not trust a later working tree.
    subprocess.run(['git', 'bundle', 'create', str(PACKAGE / 'source.bundle'),
                    'HEAD', '^55299a0a98a329f758a13db7bf2a51090f781818'],
                   cwd=ROOT, check=True)
    tools = {str(p.relative_to(ROOT)): h.sha(p) for p in
             (ROOT / 'tools/response_protocol').glob('delivery*.py')}
    for name in tools:
        copy_file(ROOT / name, 'tool-source/' + name)
    h.save(PACKAGE / 'tool-source.json', tools)
    h.save(PACKAGE / 'decision.json', dict(status='ACCEPT_BOUNDED_CHAT_DELIVERY',
        candidate_sha256=CANDIDATE, previous_sha256=PREVIOUS,
        accepted_result=h.identity(frozen / 'utf8-correction/result.json'),
        performance_acceptance=False, pending='installed health and clean guard'))
    metadata = copy.deepcopy(old)
    metadata.update(version='chat-integration-20261102', binary_sha256=CANDIDATE,
        source_commit=source_commit, tool_source_commit=source_commit,
        source_note='Prebuilt accepted chat candidate; this commit contains the '
            'exact tested runtime sources. Build-time parent and actual cache '
            'are retained in candidate/development.json; no rebuild.',
        acceptance='ACCEPT_BOUNDED_CHAT_INTEGRATION',
        previous_binary=str(PACKAGE / 'previous/q4t'),
        previous_release=str(PACKAGE / 'previous/q4t.release.json'),
        subset_artifact=str(PACKAGE / 'artifact'),
        subset_manifest_sha256=h.sha(PACKAGE / 'artifact/manifest.json'))
    for field, relative in [('tested_source_manifest', 'candidate/runtime-source.json'),
        ('build_cache', 'candidate/CMakeCache.txt'),
        ('acceptance_snapshot', 'business-acceptance.md'),
        ('acceptance_decision', 'evidence/utf8-correction/result.json'),
        ('acceptance_audit', 'evidence/accepted-archives.json'),
        ('tool_acceptance', 'evidence/utf8-correction/result.json')]:
        metadata[field] = str(PACKAGE / relative)
        metadata[field + '_sha256'] = h.sha(PACKAGE / relative)
    metadata['chat_protocol'] = dict(protocol_sha256=plan['protocol_sha256'],
        parse_response_default=False, opt_in='parse_response=true',
        capabilities='/v1/capabilities', tool_choice=['auto'],
        tool_calls='complete validated calls only', reasoning='reasoning_content',
        json_and_sse=True, tool_history_ids=True, constrained_decoding=False,
        media_acceptance=False, sdk_interoperability_acceptance=False)
    h.save(PACKAGE / 'candidate/q4t.release.json', metadata)
    health = dict(candidate=str(ROOT / 'build/q4t'), output=str(DEPLOYMENT / 'run'),
        artifact=str(PACKAGE / 'artifact'), model_dir=plan['model_dir'],
        model_inputs=plan['model_inputs'], protocol_sha256=plan['protocol_sha256'],
        port=18097, deadline_unix=time.time() + 1200,
        limits=dict(starts=2, generations=6, capability_gets=2, seconds=1200))
    h.save(PACKAGE / 'health-plan.json', health)
    for version, digest in [('previous', PREVIOUS), ('candidate', CANDIDATE)]:
        h.verify_pair(PACKAGE / version / 'q4t',
            PACKAGE / version / 'q4t.release.json', digest,
            PACKAGE / version / 'CMakeCache.txt')
    h.save(BASE / 'package-receipt.json', dict(seal=h.seal(PACKAGE)))
    print('PACKAGE_READY', flush=True)


def check():
    h.verify_seal(PACKAGE)
    states = []
    for name in ['q4t', 'q4t.release.json']:
        old, new = h.sha(PACKAGE / 'previous' / name), h.sha(PACKAGE / 'candidate' / name)
        path = ROOT / 'build' / name
        h.require_regular(path)
        current = h.sha(path)
        h.require(current in [old, new], 'unknown installed identity: ' + name)
        states.append((name, old, new, current))
    for version, digest in [('previous', PREVIOUS), ('candidate', CANDIDATE)]:
        h.verify_pair(PACKAGE / version / 'q4t',
            PACKAGE / version / 'q4t.release.json', digest,
            PACKAGE / version / 'CMakeCache.txt')
    h.require(not MARKER.exists() or h.read(MARKER)['package'] == str(PACKAGE),
              'foreign recovery marker')
    return states


def rollback(output):
    h.require_guard(output)
    states = check()  # Check the entire pair before the first write.
    # The old metadata points at its original cache. Fail closed if that
    # dependency vanished; the independent copy remains in previous/.
    previous = h.read(PACKAGE / 'previous/q4t.release.json')
    h.require(h.sha(previous['build_cache']) == previous['build_cache_sha256'],
              'previous build cache changed')
    if not MARKER.exists():
        h.save(MARKER, dict(package=str(PACKAGE), action='rollback'))
        h.fsync_directory(MARKER.parent)
    for name, old, new, current in states:
        if current != old:
            h.replace_checked(PACKAGE / 'previous' / name, ROOT / 'build' / name, new)
    h.verify_pair(ROOT / 'build/q4t', ROOT / 'build/q4t.release.json', PREVIOUS)
    MARKER.unlink()
    h.fsync_directory(MARKER.parent)
    print('RESTORED_PREVIOUS', flush=True)


def deploy():
    h.require_guard(DEPLOYMENT)
    states = check()
    h.require(not MARKER.exists() and all(old == current for _, old, _, current in states),
              'installation is not at the frozen previous pair')
    h.require(not (DEPLOYMENT / 'deployment.json').exists(), 'no implicit retry')
    plan = h.read(PACKAGE / 'health-plan.json')
    h.require(time.time() < plan['deadline_unix'] - 600, 'health time reserve')
    for name, digest in h.read(PACKAGE / 'tool-source.json').items():
        h.require(h.sha(ROOT / name) == digest, 'delivery tool changed')
    for name, digest in plan['model_inputs'].items():
        h.require(h.sha(Path(plan['model_dir']) / name) == digest, 'model input changed')
    install_interrupt_handlers()
    h.save(MARKER, dict(package=str(PACKAGE), action='install'))
    h.fsync_directory(MARKER.parent)
    state = dict(status='INSTALLING', started=time.time())
    try:
        for name, old, _, _ in states:
            h.replace_checked(PACKAGE / 'candidate' / name, ROOT / 'build' / name, old)
        h.verify_pair(ROOT / 'build/q4t', ROOT / 'build/q4t.release.json', CANDIDATE)
        from delivery_health import Health
        Health(plan).run()
        final = nvml_process_query.query(timeout=5)
        h.save(DEPLOYMENT / 'final-gpu.json', final)
        h.require(final['status'] == 'OK' and not final['pids'], 'GPU not released')
        check()
        state['status'] = 'HEALTH_CHECKED_PENDING_GUARD'
    except BaseException as error:
        state.update(status='FAILED', error=repr(error))
        try:
            rollback(DEPLOYMENT)
            state['rollback'] = 'RESTORED'
        except BaseException as recovery_error:
            state['rollback_error'] = repr(recovery_error)
        raise
    finally:
        state['finished'] = time.time()
        h.save(DEPLOYMENT / 'deployment.json', state)


def finalize():
    guard = h.read(DEPLOYMENT / 'lease/exit.json')
    h.require(guard['status'] == 'COMPLETED' and guard['child_returncode'] == 0,
              'deployment guard not clean')
    state = h.read(DEPLOYMENT / 'deployment.json')
    h.require(state['status'] == 'HEALTH_CHECKED_PENDING_GUARD' and
              state['finished'] < h.read(PACKAGE / 'health-plan.json')['deadline_unix'],
              'installed health not accepted')
    states = check()
    h.require(all(new == current for _, _, new, current in states), 'installed pair changed')
    h.require(h.read(MARKER)['package'] == str(PACKAGE), 'owned marker missing')
    MARKER.unlink()
    h.fsync_directory(MARKER.parent)
    h.save(DEPLOYMENT / 'finalized.json', dict(status='DELIVERED',
        binary_sha256=h.sha(ROOT / 'build/q4t'),
        sidecar_sha256=h.sha(ROOT / 'build/q4t.release.json'),
        health=h.read(DEPLOYMENT / 'run/result.json'),
        persistent_service=False, performance_acceptance=False, finished=time.time()))
    print('DELIVERED', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'check', 'deploy', 'finalize', 'rollback'])
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.action == 'rollback':
        h.require(args.output is not None, 'rollback needs its own output/lease')
        output = args.output.resolve()
        h.require(output.is_relative_to(ROOT / '.q4t-work'), 'rollback output location')
        rollback(output)
    else:
        globals()[args.action]()
