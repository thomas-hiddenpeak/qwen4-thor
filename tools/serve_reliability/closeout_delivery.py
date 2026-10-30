"""Audit the closeout evidence, then install with an identity-bound rollback."""
import argparse
import copy
import json
import os
from pathlib import Path
import shutil
import signal
import time

import closeout as c

ROOT, BASE, h = c.ROOT, c.BASE, c.helpers
PACKAGE = BASE / 'delivery-package'
OUT = BASE / 'deployment'
MARKER = ROOT / 'build/serve-closeout-recovery.json'
health = c.module('closeout_installed_health', Path(__file__).with_name('health.py'))


def audit():
    plan = h.read(BASE / 'resume-plan.json')
    for path, digest in plan['files'].items():
        h.require(h.sha(path) == digest, 'frozen input changed: '+path)
    guard = h.read(BASE / 'repair-lease/exit.json')
    h.require(guard['status'] == 'COMPLETED' and guard['child_returncode'] == 0,
              'repaired measurement guard')
    result = h.read(BASE / 'http-repair/result.json')
    h.require(result['passed'] and result['http'] == 30 and result['starts'] == 9,
              'complete bounded matrix')
    h.require(result['completed'] == [s['name'] for s in plan['steps']], 'step coverage')
    ledger = [json.loads(line) for line in
              (BASE / 'http-repair/ledger.jsonl').read_text().splitlines()]
    for step in plan['steps']:
        folder = BASE / 'http-repair' / step['name']
        end = h.read(folder / 'exit.json')
        h.require(end['passed'] and end['server_exit'] == 0 and end['lifecycle'] and
                  end['requests'] == step['requests'], 'request/lifecycle coverage')
        if step['case'] != 'private':
            continue
        proof, check = h.read(folder / 'prefix-proof.json'), h.read(folder / 'cache-check.json')
        h.require(proof['reference'] == proof['failed'], 'different saved failure prefix')
        boundary = c.validate_probe(proof['failed'], proof['continuation'])
        c.validate_pair(boundary, check['positive']['usage']['prompt_tokens_details']['cached_tokens'],
                         check['negative']['usage']['prompt_tokens_details']['cached_tokens'])
        positive = h.read(folder / (step['name']+'-positive-request.json'))
        negative = h.read(folder / (step['name']+'-negative-request.json'))
        failed = h.read(folder / (step['name']+'-failed-request.json'))
        h.require(positive['prompt'] == negative['prompt'], 'different continuation controls')
        h.require(negative['session_id'] == failed['session_id'] == proof['failed_session'] and
                  positive['session_id'] == proof['reference_session'] and
                  positive['session_id'] != negative['session_id'], 'namespace controls')
        h.require('prefix_cache_scope' not in negative and
                  'prefix_cache_write_tokens' not in negative, 'private probe public interference')
        ordered = [r['label'] for r in ledger if r['kind'] == 'generation' and
                   r['label'].startswith(step['name']+'-')]
        h.require(ordered == [step['name']+'-'+suffix for suffix in
                  ['reference', 'positive', 'failed', 'negative', 'public-negative']],
                  'negative before replacement or unrelated seed')
    prior = h.read(BASE / 'retained-failure.json')
    h.require(prior['generation_http'] == prior['starts'] == 1 and
              h.read(BASE / 'lease/exit.json')['status'] == 'COMMAND_FAILED',
              'retained original failure')
    h.require(result['finished'] < plan['deadline_unix'], 'absolute measurement deadline')
    evidence = {}
    for name in ['http', 'http-repair', 'lease', 'repair-lease', 'host']:
        evidence.update({str(p): h.sha(p) for p in (BASE / name).rglob('*') if p.is_file()})
    for name in ['plan.json', 'resume-plan.json', 'retained-failure.json',
                 'header-contract.log', 'oracle-tests.log', 'oracle-repair-tests.log',
                 'build.log', 'build-fault.log', 'configure.log', 'configure-fault.log',
                 'driver.log', 'repair-driver.log', 'dependency-review.json',
                 'baseline/identity.json']:
        evidence[str(BASE / name)] = h.sha(BASE / name)
    old_seal = h.verify_seal(c.PRIOR / 'evidence-archive-v2')
    decision = dict(passed=True, engineering_decision='ACCEPT_BOUNDED_SERVE_CLOSEOUT',
        candidate_sha256=h.sha(plan['candidate']), fault_sha256=h.sha(plan['fault']),
        parent_sha256=h.sha(BASE / 'baseline/q4t'), source_commit=plan['source_commit'],
        measured_http=31, measured_starts=10, matrix_http=30, matrix_starts=9,
        health_http=6, health_starts=2, projected_http=37, projected_starts=12,
        performance_acceptance=False, retained_failure=prior,
        reused_prior_evidence_seal=old_seal,
        prior_private_negative_withdrawn=True, evidence=evidence)
    h.save(BASE / 'decision.json', decision)
    print(json.dumps({k: decision[k] for k in ['passed', 'candidate_sha256', 'measured_http', 'measured_starts']}))


def prepare():
    plan, decision = h.read(BASE / 'resume-plan.json'), h.read(BASE / 'decision.json')
    h.require(decision['passed'], 'accepted evidence')
    h.require(time.time() < plan['deadline_unix'] - 600, 'installed health time reserve')
    for path, digest in plan['files'].items():
        h.require(h.sha(path) == digest, 'frozen input changed: '+path)
    for path, digest in decision['evidence'].items():
        h.require(h.sha(path) == digest, 'accepted evidence changed: '+path)
    baseline = h.read(BASE / 'baseline/identity.json')
    old = h.verify_pair(ROOT / 'build/q4t', ROOT / 'build/q4t.release.json',
                        baseline['binary_sha256'])
    h.require(h.sha(ROOT / 'build/q4t.release.json') == baseline['sidecar_sha256'],
              'installed metadata changed')
    PACKAGE.mkdir()
    for name in ['previous', 'candidate', 'tested-source', 'evidence']:
        (PACKAGE / name).mkdir()
    for source, target in [('baseline/q4t', 'previous/q4t'),
        ('baseline/q4t.release.json', 'previous/q4t.release.json'),
        ('baseline/CMakeCache.txt', 'previous/CMakeCache.txt'),
        ('frozen/candidate', 'candidate/q4t'), ('frozen/CMakeCache.txt', 'candidate/CMakeCache.txt'),
        ('frozen/runtime-source.json', 'candidate/runtime-source.json'),
        ('decision.json', 'decision.json'), ('resume-plan.json', 'evidence/plan.json'),
        ('dependency-review.json', 'evidence/dependency-review.json')]:
        h.copy_checked(h.identity(BASE / source), PACKAGE / target)
    contract = ROOT / 'docs/SERVE_CLOSEOUT_2026-10-31.md'
    h.copy_checked(h.identity(contract), PACKAGE / 'acceptance-contract.md')
    runtime = h.read(PACKAGE / 'candidate/runtime-source.json')
    for relative, digest in runtime.items():
        h.require(h.sha(ROOT / relative) == digest, 'tested runtime source')
        target = PACKAGE / 'tested-source' / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        h.copy_checked(h.identity(ROOT / relative), target)
    shutil.copytree(plan['artifact'], PACKAGE / 'artifact')
    for name in ['head.bf16', 'token_ids.bin', 'manifest.json']:
        h.require(h.sha(PACKAGE / 'artifact' / name) == plan['files'][str(Path(plan['artifact']) / name)],
                  'head artifact identity')
    archive = BASE / 'evidence-archive'
    archive.mkdir()
    for path, digest in decision['evidence'].items():
        target = archive / Path(path).relative_to(BASE)
        target.parent.mkdir(parents=True, exist_ok=True)
        h.copy_checked(h.identity(path), target)
        h.require(h.sha(target) == digest, 'archive identity')
    receipt = dict(files=len(decision['evidence']), seal=h.seal(archive),
                   original=decision['evidence'])
    h.save(BASE / 'archive-receipt.json', receipt)
    h.copy_checked(h.identity(BASE / 'archive-receipt.json'), PACKAGE / 'evidence/archive-receipt.json')
    metadata = copy.deepcopy(old)
    metadata.update(version='serve-closeout-20261031',
        binary_sha256=h.sha(PACKAGE / 'candidate/q4t'), source_commit=plan['source_commit'],
        source_note='Header progress and private publication closeout. Fault hooks disabled.',
        tested_source_manifest=str(PACKAGE / 'candidate/runtime-source.json'),
        tested_source_manifest_sha256=h.sha(PACKAGE / 'candidate/runtime-source.json'),
        build_cache=str(PACKAGE / 'candidate/CMakeCache.txt'),
        build_cache_sha256=h.sha(PACKAGE / 'candidate/CMakeCache.txt'),
        acceptance=decision['engineering_decision'],
        acceptance_snapshot=str(PACKAGE / 'acceptance-contract.md'),
        acceptance_snapshot_sha256=h.sha(PACKAGE / 'acceptance-contract.md'),
        acceptance_decision=str(PACKAGE / 'decision.json'),
        acceptance_decision_sha256=h.sha(PACKAGE / 'decision.json'),
        acceptance_audit=str(PACKAGE / 'evidence/archive-receipt.json'),
        acceptance_audit_sha256=h.sha(PACKAGE / 'evidence/archive-receipt.json'),
        previous_binary=str(PACKAGE / 'previous/q4t'),
        previous_release=str(PACKAGE / 'previous/q4t.release.json'),
        subset_artifact=str(PACKAGE / 'artifact'),
        subset_manifest_sha256=h.sha(PACKAGE / 'artifact/manifest.json'))
    metadata['serving'].update(header_state='actual successful writes; partial headers close on exception',
        private_publication='actual saved token prefix; matching positive and negative continuation',
        fault_injection=False, performance_acceptance=False)
    h.save(PACKAGE / 'candidate/q4t.release.json', metadata)
    h.verify_pair(PACKAGE / 'candidate/q4t', PACKAGE / 'candidate/q4t.release.json',
                  decision['candidate_sha256'])
    h.verify_pair(PACKAGE / 'previous/q4t', PACKAGE / 'previous/q4t.release.json',
                  decision['parent_sha256'], PACKAGE / 'previous/CMakeCache.txt')
    tools = [Path(__file__), Path(c.__file__), Path(health.__file__), Path(h.__file__),
             Path(h.build.__file__), ROOT / 'tools/evalscope/serve_reliability.py',
             ROOT / 'tools/evalscope/gpu_exclusive.py', ROOT / 'tools/evalscope/nvml_process_query.py']
    h.save(PACKAGE / 'runtime-tool-inputs.json', {str(p): h.sha(p) for p in tools})
    for source in tools:
        target = PACKAGE / 'tools' / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        h.copy_checked(h.identity(source), target)
    health_plan = dict(candidate=str(ROOT / 'build/q4t'), artifact=str(PACKAGE / 'artifact'),
        output=str(OUT / 'run'), model=plan['model'], port=plan['port'],
        cache_prompt=plan['cache_prompt'], deadline_unix=plan['deadline_unix'])
    OUT.mkdir()
    h.save(OUT / 'health-plan.json', health_plan)
    h.save(PACKAGE / 'health-plan.json', health_plan)
    h.save(BASE / 'package-receipt.json', dict(seal=h.seal(PACKAGE), runtime_files=len(runtime)))


def restore():
    h.verify_seal(PACKAGE)
    if MARKER.exists():
        h.require(h.read(MARKER)['package'] == str(PACKAGE), 'foreign recovery marker')
    known = {}
    for name in ['q4t', 'q4t.release.json']:
        target = ROOT / 'build' / name
        old, new = h.sha(PACKAGE / 'previous' / name), h.sha(PACKAGE / 'candidate' / name)
        current = h.sha(target)
        h.require(current in [old, new], 'unknown installed identity; no overwrite')
        known[name] = (old, new, current)
    for name, (old, new, current) in known.items():
        if current != old:
            h.replace_checked(PACKAGE / 'previous' / name, ROOT / 'build' / name, new)
    h.verify_pair(ROOT / 'build/q4t', ROOT / 'build/q4t.release.json',
                  h.sha(PACKAGE / 'previous/q4t'))
    if MARKER.exists():
        MARKER.unlink(); h.fsync_directory(MARKER.parent)


def verify_tools():
    h.verify_seal(PACKAGE)
    for path, digest in h.read(PACKAGE / 'runtime-tool-inputs.json').items():
        h.require(h.sha(path) == digest, 'delivery tool identity')


def deploy():
    c.guarded(); verify_tools()
    plan = h.read(OUT / 'health-plan.json')
    h.require(plan == h.read(PACKAGE / 'health-plan.json') and not MARKER.exists(),
              'health plan and recovery marker')
    h.require(time.time() < plan['deadline_unix']-600, 'installed health reserve')
    for name in ['q4t', 'q4t.release.json']:
        h.require(h.sha(ROOT / 'build' / name) == h.sha(PACKAGE / 'previous' / name),
                  'installed baseline identity')
    h.save(MARKER, dict(package=str(PACKAGE), candidate=h.sha(PACKAGE / 'candidate/q4t')))
    h.fsync_directory(MARKER.parent)
    state = dict(status='INSTALLING', started=time.time())
    def interrupted(*_):
        raise KeyboardInterrupt('owned installation interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    try:
        for name in ['q4t', 'q4t.release.json']:
            h.replace_checked(PACKAGE / 'candidate' / name, ROOT / 'build' / name,
                              h.sha(PACKAGE / 'previous' / name))
        health.Health(plan).run()
        result = h.read(OUT / 'run/result.json')
        h.require(result['passed'] and result['http'] == 6 and result['starts'] == 2,
                  'six installed health requests')
        verify_tools()
        state['status'] = 'HEALTH_CHECKED_PENDING_GUARD'
    except BaseException as error:
        state.update(status='FAILED', error=repr(error))
        try:
            restore(); state['rollback'] = 'RESTORED_BASELINE'
        except BaseException as failure:
            state.update(status='RECOVERY_REQUIRED', rollback_error=repr(failure))
        raise
    finally:
        state['finished'] = time.time()
        h.save(OUT / 'deployment.json', state)


def finalize():
    guard, state = h.read(OUT / 'lease/exit.json'), h.read(OUT / 'deployment.json')
    h.require(guard['status'] == 'COMPLETED' and guard['child_returncode'] == 0,
              'deployment guard')
    h.require(state['status'] == 'HEALTH_CHECKED_PENDING_GUARD', 'health state')
    h.require(state['finished'] < h.read(OUT / 'health-plan.json')['deadline_unix'],
              'absolute deadline')
    verify_tools()
    h.verify_pair(ROOT / 'build/q4t', ROOT / 'build/q4t.release.json',
                  h.sha(PACKAGE / 'candidate/q4t'))
    h.require(h.sha(ROOT / 'build/q4t.release.json') ==
              h.sha(PACKAGE / 'candidate/q4t.release.json'), 'installed sidecar identity')
    h.require(h.read(MARKER)['package'] == str(PACKAGE), 'owned recovery marker')
    MARKER.unlink(); h.fsync_directory(MARKER.parent)
    h.save(OUT / 'finalized.json', dict(status='DELIVERED',
        binary_sha256=h.sha(ROOT / 'build/q4t'), sidecar_sha256=h.sha(ROOT / 'build/q4t.release.json'),
        total_http=37, total_starts=12, health_http=6, health_starts=2,
        persistent_service=False, finished=time.time()))
    archive = BASE / 'health-archive'
    shutil.copytree(OUT, archive)
    h.save(BASE / 'health-archive-receipt.json', dict(seal=h.seal(archive)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['audit', 'prepare', 'deploy', 'finalize', 'rollback'])
    action = parser.parse_args().action
    if action == 'rollback':
        c.guarded(); restore()
    else:
        globals()[action]()
