"""Deploy the accepted immutable candidate and check two bounded HTTP cases.

Run the complete driver under gpu_exclusive. No persistent service is installed.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / '.q4t-work/metadata-followup-20261020/deploy-v1'
EVIDENCE = ROOT / '.q4t-work/inference-metadata-20261020/resume-acceptance-v1'
PACKAGE = ROOT / '.q4t-work/releases/metadata-20261020'
OLD = '44abd8b1a7edaca41620b87b4cf894427f8397c1e3529ef56512940ef1b44e03'
NEW = 'c8f4b241f67b2818e92791f688082e338f6981a5db923cf09fb07e30b99e887d'
MODEL = '/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream'


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def replace(source, destination):
    temporary = destination.with_name(destination.name + '.metadata-new')
    shutil.copy2(source, temporary)
    os.replace(temporary, destination)


def main():
    binary = ROOT / 'build/q4t'
    sidecar = ROOT / 'build/q4t.release.json'
    previous = OUT / 'previous'
    result = {'status': 'PREPARING', 'health_completed': [],
              'performance_acceptance': False, 'persistent_service': False}
    changed = False
    def interrupted(signum, frame):
        raise RuntimeError(f'interrupted by signal {signum}')
    signal.signal(signal.SIGTERM, interrupted)
    try:
        if sha(binary) != OLD:
            raise ValueError('default binary changed')
        old_meta = json.loads(sidecar.read_text())
        if old_meta['binary_sha256'] != OLD or sha(Path(old_meta['build_cache'])) != old_meta['build_cache_sha256']:
            raise ValueError('old deployment identity mismatch')
        manifest = json.loads((ROOT / 'docs/evidence/inference-metadata-20261020/resume-acceptance-v1/sealed-files.json').read_text())
        for name in ['candidate-build/q4t', 'candidate-build/CMakeCache.txt'] + [
                f'{prefix}-{mode}/{suffix}'
                for mode in ['on', 'off']
                for prefix, suffix in [('reference', 'inputs/manifest.json'),
                                       ('reference', 'inputs/requests.jsonl'),
                                       ('quality', 'results.json')]]:
            if sha(EVIDENCE / name) != manifest[name]:
                raise ValueError('sealed candidate mismatch: ' + name)
        if sha(EVIDENCE / 'candidate-build/q4t') != NEW:
            raise ValueError('candidate binary changed')
        if subprocess.check_output(['git', 'diff', '3d5564e', '--', 'src', 'include', 'CMakeLists.txt'], cwd=ROOT):
            raise ValueError('runtime source changed')
        previous.mkdir()
        for name in ['q4t', 'q4t.release.json']:
            shutil.copy2(ROOT / 'build' / name, previous / name)
        PACKAGE.mkdir()
        for name in ['q4t', 'CMakeCache.txt']:
            shutil.copy2(EVIDENCE / 'candidate-build' / name, PACKAGE / name)
        metadata = {'version': 'metadata-hardening-20261020',
                    'binary_sha256': NEW, 'source_commit': '3d5564e',
                    'build_cache': str(PACKAGE / 'CMakeCache.txt'),
                    'build_cache_sha256': sha(PACKAGE / 'CMakeCache.txt'),
                    'acceptance': 'ENGINEERING_ACCEPTED_WITH_TIMING_UNCERTAINTY',
                    'acceptance_decision': str(ROOT / 'docs/INFERENCE_METADATA_FINAL_REVIEW_2026-10-20.md'),
                    'scope': 'default executable; no persistent service; statistical noninferiority unproven',
                    'previous_binary': str(previous / 'q4t'),
                    'previous_release': str(previous / 'q4t.release.json')}
        save(PACKAGE / 'q4t.release.json', metadata)
        save(PACKAGE / 'manifest.json', {p.name: sha(p) for p in PACKAGE.iterdir() if p.is_file()})
        save(OUT / 'rollback-identity.json', {p.name: sha(p) for p in previous.iterdir()})
        changed = True
        replace(PACKAGE / 'q4t', binary)
        replace(PACKAGE / 'q4t.release.json', sidecar)
        for mode in ['on', 'off']:
            fixtures = OUT / ('fixtures-' + mode)
            fixtures.mkdir()
            source = EVIDENCE / ('reference-' + mode) / 'inputs'
            rows = json.loads((source / 'manifest.json').read_text())
            if rows[0]['length'] != 1024:
                raise ValueError('first fixture is not 1024 tokens')
            save(fixtures / 'manifest.json', rows[:1])
            (fixtures / 'requests.jsonl').write_text((source / 'requests.jsonl').read_text().splitlines()[0] + '\n')
            command = ['python3', '-B', 'tools/evalscope/run_acceptance.py',
                       '--mode', 'quality', '--output', str(OUT / ('health-' + mode)),
                       '--binary', str(binary), '--model-dir', MODEL,
                       '--fixtures', str(fixtures), '--reference',
                       str(EVIDENCE / ('quality-' + mode) / 'results.json'),
                       '--port', '8740', '--startup-timeout', '300']
            if mode == 'on':
                command.append('--mtp-default')
            save(OUT / ('command-' + mode + '.json'), command)
            with (OUT / ('health-' + mode + '-driver.log')).open('w') as log:
                subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                               check=True, timeout=600)
            health = json.loads((OUT / ('health-' + mode) / 'exit.json').read_text())
            if health['completed'] != 1 or not health['http_output_checks_passed'] or health['server'] != 0:
                raise ValueError('health/shutdown check failed')
            result['health_completed'].append(mode)
            save(OUT / 'deployment.json', result)
        if sha(binary) != NEW or sha(sidecar) != sha(PACKAGE / 'q4t.release.json'):
            raise ValueError('installed identity changed')
        result.update(status='DEPLOYED_HEALTH_CHECKED', binary_sha256=NEW,
                      previous_sha256=OLD, requests=2, loads=2)
    except BaseException as error:
        result.update(status='FAILED', error=repr(error))
        if changed:
            replace(previous / 'q4t', binary)
            replace(previous / 'q4t.release.json', sidecar)
            result['restored_previous'] = sha(binary) == OLD
        raise
    finally:
        save(OUT / 'deployment.json', result)


if __name__ == '__main__':
    main()
