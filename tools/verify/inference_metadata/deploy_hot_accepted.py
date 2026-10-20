"""Deploy accepted d4232b63, or restore bc348458; always run under GPU lease."""
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess

from deploy_accepted import sha, save
from audit_hot_acceptance import audit

ROOT = Path(__file__).resolve().parents[3]
STAGE = ROOT / '.q4t-work/moe-hot-upload-20261020'
OUT = STAGE / 'deploy-v1'
PREPARED = STAGE / 'acceptance-v1/run/candidate-build'
PACKAGE = ROOT / '.q4t-work/releases/moe-hot-upload-20261020'
DECISION = ROOT / 'docs/MOE_HOT_UPLOAD_DEPLOYMENT_2026-10-20.md'
OLD = 'bc348458db08f7d8006ab9416fd83dd4810fc905af9bd6d2a766fecab937790a'
NEW = 'd4232b63e53bece8c82bd87d8dcac628fb089f4f7eddc163535030133107f7a6'
MODEL = '/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream'
BINARY = ROOT / 'build/q4t'
SIDECAR = ROOT / 'build/q4t.release.json'


def replace(source, destination):
    temporary = destination.with_name(destination.name + '.batch-new')
    shutil.copy2(source, temporary)
    os.replace(temporary, destination)


def verify_pair(binary, sidecar, expected):
    metadata = json.loads(sidecar.read_text())
    if sha(binary) != expected or metadata['binary_sha256'] != expected:
        raise ValueError('binary/sidecar identity mismatch')
    if sha(Path(metadata['build_cache'])) != metadata['build_cache_sha256']:
        raise ValueError('release cache changed')


def preflight():
    verify_pair(BINARY, SIDECAR, OLD)
    report = audit(STAGE)
    if report['candidate_sha256'] != NEW or sha(PREPARED / 'q4t') != NEW:
        raise ValueError('accepted candidate mismatch')
    source = STAGE / 'acceptance-v1/run/source'
    for folder in ('src', 'include'):
        for path in (ROOT / folder).rglob('*'):
            if not path.is_file():
                continue
            name = path.relative_to(ROOT)
            saved = source / name
            if not saved.is_file():
                raise ValueError('new runtime source: ' + str(name))
            if path == ROOT / 'include/q4t/io/pending_host_storage.h':
                # The committed change after validation is a line wrap only.
                if path.read_text().split() != saved.read_text().split():
                    raise ValueError('owner implementation changed')
            elif sha(path) != sha(saved):
                raise ValueError('runtime source changed: ' + str(name))
    if '收尾吧,以后的工作具备条件,你就进行收尾工作.' not in DECISION.read_text():
        raise ValueError('deployment authorization record missing')
    if PACKAGE.exists():
        raise ValueError('release package already exists')


def rollback():
    previous = OUT / 'previous'
    expected = json.loads((OUT / 'rollback-identity.json').read_text())
    for name, digest in expected.items():
        if sha(previous / name) != digest:
            raise ValueError('rollback backup changed: ' + name)
    verify_pair(previous / 'q4t', previous / 'q4t.release.json', OLD)
    new_manifest = json.loads((PACKAGE / 'manifest.json').read_text())
    # Allow a known partial replacement, but never overwrite an unknown pair.
    if sha(BINARY) not in [OLD, NEW]:
        raise ValueError('unknown current binary; refuse rollback')
    if sha(SIDECAR) not in [expected['q4t.release.json'], new_manifest['q4t.release.json']]:
        raise ValueError('unknown current sidecar; refuse rollback')
    replace(previous / 'q4t', BINARY)
    replace(previous / 'q4t.release.json', SIDECAR)
    verify_pair(BINARY, SIDECAR, OLD)
    if sha(SIDECAR) != expected['q4t.release.json']:
        raise ValueError('restored sidecar mismatch')
    return dict(status='ROLLED_BACK', binary_sha256=OLD)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--preflight-only', action='store_true')
    parser.add_argument('--rollback', action='store_true')
    args = parser.parse_args()
    if args.preflight_only and args.rollback:
        parser.error('choose preflight or rollback')
    if args.rollback:
        print(json.dumps(rollback()))
        return
    if args.preflight_only:
        preflight()
        print('PASS: read-only deployment preflight; no GPU or default mutation')
        return
    result = dict(status='PREPARING', health_completed=[], performance_samples=0,
                  performance_acceptance=False, persistent_service=False)
    changed = False

    def interrupted(signum, frame):
        raise RuntimeError('interrupted by signal ' + str(signum))

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        preflight()
        previous = OUT / 'previous'
        previous.mkdir()
        for name in ['q4t', 'q4t.release.json']:
            shutil.copy2(ROOT / 'build' / name, previous / name)
        old_meta = json.loads(SIDECAR.read_text())
        shutil.copy2(Path(old_meta['build_cache']), previous / 'CMakeCache.txt')
        save(OUT / 'rollback-identity.json', {p.name: sha(p) for p in previous.iterdir()})
        PACKAGE.mkdir()
        for name in ['q4t', 'CMakeCache.txt']:
            shutil.copy2(PREPARED / name, PACKAGE / name)
        shutil.copy2(DECISION, OUT / 'acceptance-decision.md')
        shutil.copy2(Path(__file__), OUT / 'driver.py')
        metadata = dict(version='moe-hot-upload-20261020',
                        binary_sha256=NEW, source_commit='7bb190f',
                        tested_source_manifest=str(STAGE / 'acceptance-v1/run/sources.json'),
                        tested_source_manifest_sha256=sha(STAGE / 'acceptance-v1/run/sources.json'),
                        source_note='7bb190f differs from tested sources only by recorded owner line wrap',
                        build_cache=str(PACKAGE / 'CMakeCache.txt'),
                        build_cache_sha256=sha(PACKAGE / 'CMakeCache.txt'),
                        acceptance='ENGINEERING_ACCEPTED_RELIABILITY',
                        acceptance_decision=str(DECISION),
                        acceptance_snapshot=str(OUT / 'acceptance-decision.md'),
                        acceptance_snapshot_sha256=sha(OUT / 'acceptance-decision.md'),
                        acceptance_report=str(ROOT / 'docs/MOE_HOT_UPLOAD_AUDIT_2026-10-20.md'),
                        scope='default executable; no persistent service; reliability change; no performance claim',
                        previous_binary=str(previous / 'q4t'),
                        previous_release=str(previous / 'q4t.release.json'))
        save(PACKAGE / 'q4t.release.json', metadata)
        save(PACKAGE / 'manifest.json', {p.name: sha(p) for p in PACKAGE.iterdir()})
        verify_pair(PACKAGE / 'q4t', PACKAGE / 'q4t.release.json', NEW)
        verify_pair(BINARY, SIDECAR, OLD)
        if sha(SIDECAR) != sha(previous / 'q4t.release.json'):
            raise ValueError('default sidecar changed during preparation')
        changed = True
        replace(PACKAGE / 'q4t', BINARY)
        replace(PACKAGE / 'q4t.release.json', SIDECAR)
        verify_pair(BINARY, SIDECAR, NEW)
        result['status'] = 'INSTALLED_HEALTH_PENDING'
        save(OUT / 'deployment.json', result)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}
        for mode in ['on', 'off']:
            source = STAGE / 'acceptance-v1/run' / ('quality-' + mode)
            fixtures = OUT / ('fixtures-' + mode)
            fixtures.mkdir()
            rows = json.loads((source / 'inputs/manifest.json').read_text())
            if rows[0]['length'] != 1024:
                raise ValueError('health fixture is not 1024 tokens')
            save(fixtures / 'manifest.json', rows[:1])
            (fixtures / 'requests.jsonl').write_text(
                (source / 'inputs/requests.jsonl').read_text().splitlines()[0] + '\n')
            command = ['python3', '-B', 'tools/evalscope/run_acceptance.py',
                       '--mode', 'quality', '--output', str(OUT / ('health-' + mode)),
                       '--binary', str(BINARY), '--model-dir', MODEL,
                       '--fixtures', str(fixtures), '--reference', str(source / 'results.json'),
                       '--port', '8740', '--startup-timeout', '300']
            if mode == 'on':
                command.append('--mtp-default')
            save(OUT / ('command-' + mode + '.json'), command)
            with (OUT / ('health-' + mode + '-driver.log')).open('w') as log:
                subprocess.run(command, cwd=ROOT, env=env, stdout=log,
                               stderr=subprocess.STDOUT, timeout=600, check=True)
            health = json.loads((OUT / ('health-' + mode) / 'exit.json').read_text())
            if health['completed'] != 1 or not health['http_output_checks_passed'] or health['server'] != 0:
                raise ValueError('health/shutdown failed')
            result['health_completed'].append(mode)
            save(OUT / 'deployment.json', result)
        verify_pair(BINARY, SIDECAR, NEW)
        if sha(SIDECAR) != sha(PACKAGE / 'q4t.release.json'):
            raise ValueError('installed sidecar changed')
        result.update(status='DEPLOYED_HEALTH_CHECKED', binary_sha256=NEW,
                      previous_sha256=OLD, requests=2, loads=2)
    except BaseException as error:
        result.update(status='FAILED', error=repr(error))
        if changed:
            try:
                result['rollback'] = rollback()
            except BaseException as rollback_error:
                result['rollback_error'] = repr(rollback_error)
        raise
    finally:
        save(OUT / 'deployment.json', result)


if __name__ == '__main__':
    main()
