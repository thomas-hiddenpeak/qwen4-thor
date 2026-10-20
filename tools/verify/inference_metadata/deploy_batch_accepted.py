"""Deploy accepted 753d9769, or restore c8f; always run under GPU lease."""
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess

from deploy_accepted import sha, save
from run_batch_replay import check_model

ROOT = Path(__file__).resolve().parents[3]
STAGE = ROOT / '.q4t-work/metadata-followup-20261020'
OUT = STAGE / 'batch-deploy-v1'
PREPARED = STAGE / 'final-review-v1'
PACKAGE = ROOT / '.q4t-work/releases/metadata-batch-20261020'
DECISION = ROOT / 'docs/METADATA_BATCH_DEPLOYMENT_2026-10-20.md'
OLD = 'c8f4b241f67b2818e92791f688082e338f6981a5db923cf09fb07e30b99e887d'
NEW = '753d9769f96e9848800eae88b4800bef65e20885fd2ec025b8f3bc91fc497542'
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
    if subprocess.check_output(['git', 'diff', 'b6870c0', '--', 'src', 'include'], cwd=ROOT):
        raise ValueError('runtime source changed')
    audit = json.loads((ROOT / 'docs/evidence/metadata-followup-20261020/'
                       'final-review-v1/audit.json').read_text())
    for name, digest in audit['prepared_files'].items():
        if sha(PREPARED / name) != digest:
            raise ValueError('prepared file changed: ' + name)
    if sha(SIDECAR) != audit['prepared_files']['previous/q4t.release.json']:
        raise ValueError('default sidecar changed since preparation')
    for stage in ['batch-http-v1', 'batch-replay-v1', 'deploy-v1', 'batch-performance-v1']:
        manifest = json.loads((ROOT / 'docs/evidence/metadata-followup-20261020'
                               / stage / 'sealed-files.json').read_text())
        for name, digest in manifest.items():
            if sha(STAGE / stage / name) != digest:
                raise ValueError('sealed evidence changed: ' + stage + '/' + name)
    if json.loads((STAGE / 'batch-performance-v1/run.json').read_text())['status'] != 'MEASUREMENTS_COMPLETE_ACCEPTANCE_PENDING':
        raise ValueError('performance stage incomplete')
    check_model(json.loads((STAGE / 'batch-replay-v1/model-files.json').read_text()))
    if '可以接受这次加固并部署' not in DECISION.read_text():
        raise ValueError('specific acceptance record missing')
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
            shutil.copy2(PREPARED / 'candidate' / name, PACKAGE / name)
        shutil.copy2(DECISION, OUT / 'acceptance-decision.md')
        shutil.copy2(Path(__file__), OUT / 'driver.py')
        metadata = dict(version='metadata-batch-hardening-20261020',
                        binary_sha256=NEW, source_commit='b6870c0',
                        build_cache=str(PACKAGE / 'CMakeCache.txt'),
                        build_cache_sha256=sha(PACKAGE / 'CMakeCache.txt'),
                        acceptance='ENGINEERING_ACCEPTED_WITH_TIMING_UNCERTAINTY',
                        acceptance_decision=str(DECISION),
                        acceptance_snapshot=str(OUT / 'acceptance-decision.md'),
                        acceptance_snapshot_sha256=sha(OUT / 'acceptance-decision.md'),
                        performance_report=str(ROOT / 'docs/METADATA_BATCH_PERFORMANCE_RESULT_2026-10-20.md'),
                        scope='default executable; no persistent service; statistical noninferiority unproven',
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
            source = STAGE / 'batch-http-v1' / ('quality-' + mode)
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
        check_model(json.loads((STAGE / 'batch-replay-v1/model-files.json').read_text()))
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
