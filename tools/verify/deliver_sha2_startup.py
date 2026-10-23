"""Seal or deploy the accepted SHA2 candidate; deployment requires its GPU lease.

Each destination is replaced atomically. The pair is not a power-loss transaction;
a durable marker and identity-checked rollback preserve the previous release.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    'delivery_helpers', ROOT / 'tools/verify/startup_delivery/deploy.py')
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)


def prepare(stage, install):
    audit = h.read(stage / 'audit.json')
    decision = h.read(stage / 'decision.json')
    plan = h.read(stage / 'frozen-plan.json')
    assert decision['engineering_decision'] == 'ACCEPT_STARTUP_BOUNDED'
    assert audit['status'] == 'PASS_BOUNDED_EVIDENCE' and audit['startup_target_met']
    assert audit['candidate_sha256'] == decision['candidate_sha256'] == plan['candidate_sha256']
    assert decision['audit_sha256'] == h.sha(stage / 'audit.json')
    assert decision['frozen_plan_sha256'] == h.sha(stage / 'frozen-plan.json')
    for path, digest in plan['files'].items():
        assert h.sha(Path(path)) == digest, path
    h.verify_pair(install / 'build/q4t', install / 'build/q4t.release.json', plan['parent_sha256'])
    package = stage / 'delivery-package'
    package.mkdir()
    for folder in ['previous', 'candidate']:
        (package / folder).mkdir()
    for source, target in [
        (install / 'build/q4t', package / 'previous/q4t'),
        (install / 'build/q4t.release.json', package / 'previous/q4t.release.json'),
        (stage / 'build/q4t', package / 'candidate/q4t'),
        (stage / 'build/CMakeCache.txt', package / 'candidate/CMakeCache.txt'),
        (ROOT / 'docs/SHA2_STARTUP_2026-10-23.md', package / 'decision.md'),
        (Path(__file__), package / 'deliver_sha2_startup.py')]:
        shutil.copy2(source, target)
    for name in ['audit.json', 'decision.json', 'frozen-plan.json']:
        shutil.copy2(stage / name, package / name)
    old = h.read(package / 'previous/q4t.release.json')
    shutil.copy2(old['build_cache'], package / 'previous/CMakeCache.txt')
    previous = install / '.q4t-work/mtp-subset-head-20261023/delivery-package-v2'
    for folder in ['artifact', 'health-fixture']:
        shutil.copytree(previous / folder, package / folder)
    shutil.copytree(stage / 'source-snapshot', package / 'tested-source')
    output = ROOT / '.q4t-work/sha2-deployment-v1'
    metadata = dict(version='sha2-startup-20261023', binary_sha256=plan['candidate_sha256'],
                    source_commit=plan['source_commit'],
                    source_note='exact runtime inputs frozen before HTTP; tested-source retained',
                    build_cache=str(package / 'candidate/CMakeCache.txt'),
                    build_cache_sha256=h.sha(package / 'candidate/CMakeCache.txt'),
                    acceptance='ACCEPT_STARTUP_BOUNDED',
                    acceptance_snapshot=str(package / 'decision.md'),
                    acceptance_snapshot_sha256=h.sha(package / 'decision.md'),
                    acceptance_audit=str(package / 'audit.json'),
                    acceptance_audit_sha256=h.sha(package / 'audit.json'),
                    previous_binary=str(package / 'previous/q4t'),
                    previous_release=str(package / 'previous/q4t.release.json'),
                    subset_artifact=str(package / 'artifact'),
                    subset_manifest_sha256=h.sha(package / 'artifact/manifest.json'),
                    scope='full-head default; same explicit 65536 BF16 subset; no persistent service',
                    atomicity='individual atomic replaces; recovery marker; identity-checked rollback')
    h.save(package / 'candidate/q4t.release.json', metadata)
    steps = []
    for name, flags in [('subset', ['--mtp', '--mtp-subset-head', str(package / 'artifact')]),
                        ('full', ['--mtp']), ('plain', [])]:
        steps.append(dict(name=name, command=[
            'python3', '-B', str(ROOT / 'tools/evalscope/run_acceptance.py'),
            '--mode', 'quality', '--output', str(output / name),
            '--binary', str(install / 'build/q4t'), '--model-dir',
            '/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream',
            '--fixtures', str(package / 'health-fixture'), '--startup-timeout', '300',
            '--port', '8753', '--max-len', '208896', *flags]))
    h.save(package / 'health-plan.json', dict(output=str(output), starts=3, requests=3,
                                             timeout_seconds=1200, steps=steps))
    h.seal(package)
    print(package)


def deploy(stage, install, rollback_output):
    package = stage / 'delivery-package'
    plan = h.read(package / 'health-plan.json')
    output = rollback_output.resolve() if rollback_output else Path(plan['output'])
    assert output.is_relative_to(ROOT / '.q4t-work')
    if rollback_output:
        assert output != Path(plan['output']), 'rollback needs fresh evidence'
    h.require_guard(output)
    h.verify_seal(package)
    old = h.read(package / 'previous/q4t.release.json')['binary_sha256']
    new = h.read(package / 'candidate/q4t.release.json')['binary_sha256']
    audit = h.read(package / 'audit.json')
    assert old == audit['parent_sha256'] and new == audit['candidate_sha256']
    marker = install / 'build/sha2-startup-recovery.json'
    def restore():
        for name in ['q4t', 'q4t.release.json']:
            dest = install / 'build' / name
            oldsha, newsha = h.sha(package / 'previous' / name), h.sha(package / 'candidate' / name)
            assert h.sha(dest) in [oldsha, newsha], 'unknown destination; preserve marker'
            if h.sha(dest) != oldsha:
                h.replace_checked(package / 'previous' / name, dest, newsha)
        h.verify_pair(install / 'build/q4t', install / 'build/q4t.release.json', old)
        if marker.exists():
            marker.unlink()
            h.fsync_directory(marker.parent)
    if rollback_output:
        restore()
        h.save(output / 'rollback.json', dict(status='ROLLED_BACK', binary_sha256=old,
                                              package=str(package), finished=time.time()))
        return
    assert not marker.exists() and not (output / 'deployment.json').exists()
    for name in ['q4t', 'q4t.release.json']:
        assert h.sha(install / 'build' / name) == h.sha(package / 'previous' / name)
    h.save(marker, dict(package=str(package), output=str(output), previous=old, candidate=new,
                        rollback='invoke deliver_sha2_startup.py deploy with --rollback-output under a fresh output/lease'))
    h.fsync_directory(marker.parent)
    state = dict(status='INSTALLING', health=[], started=time.time())
    def interrupted(signum, _frame):
        raise KeyboardInterrupt('signal ' + str(signum))
    signal.signal(signal.SIGTERM, interrupted)
    try:
        for name in ['q4t', 'q4t.release.json']:
            h.replace_checked(package / 'candidate' / name, install / 'build' / name,
                              h.sha(package / 'previous' / name))
        h.verify_pair(install / 'build/q4t', install / 'build/q4t.release.json', new)
        deadline = time.monotonic() + plan['timeout_seconds']
        env = {k: v for k, v in os.environ.items() if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}
        for step in plan['steps']:
            with (output / (step['name'] + '-driver.log')).open('x') as log:
                subprocess.run(step['command'], cwd=ROOT, env=env, stdout=log,
                               stderr=subprocess.STDOUT, check=True,
                               timeout=max(1, deadline-time.monotonic()))
            directory = output / step['name']
            exit_state, rows = h.read(directory / 'exit.json'), h.read(directory / 'results.json')
            assert exit_state['server'] == 0 and exit_state['http_output_checks_passed']
            assert len(rows) == 1 and rows[0]['exact_match']
            log = (directory / 'server.log').read_text()
            final = [json.loads(line.split('] ', 1)[1]) for line in log.splitlines()
                     if line.startswith('[startup-reservation] ') and '"event":"finalized"' in line]
            assert len(final) == 1 and final[0]['status'] == 'PASS' and final[0]['release_unknown'] == 0
            assert 'MTP init failed' not in log and 'retained after failed drain' not in log
            state['health'].append(dict(name=step['name'], exact_match=True, finalized=final[0]))
        h.verify_seal(package)
        h.verify_pair(install / 'build/q4t', install / 'build/q4t.release.json', new)
        state['status'] = 'DEPLOYED_HEALTH_CHECKED'
        marker.unlink()
        h.fsync_directory(marker.parent)
    except BaseException as error:
        state.update(status='FAILED', error=repr(error))
        try:
            restore()
            state['rollback'] = 'RESTORED_PREVIOUS'
        except BaseException as failure:
            state.update(status='RECOVERY_REQUIRED', rollback_error=repr(failure))
        raise
    finally:
        state['finished'] = time.time()
        h.save(output / 'deployment.json', state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'deploy'])
    parser.add_argument('--stage', type=Path, required=True)
    parser.add_argument('--install-root', type=Path, required=True)
    parser.add_argument('--rollback-output', type=Path)
    args = parser.parse_args()
    stage, install = args.stage.resolve(), args.install_root.resolve()
    assert stage.is_relative_to(install / '.q4t-work')
    if args.action == 'prepare':
        assert args.rollback_output is None
        prepare(stage, install)
    else:
        deploy(stage, install, args.rollback_output)


if __name__ == '__main__':
    main()
