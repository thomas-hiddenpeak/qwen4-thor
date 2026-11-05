"""Bind the accepted default and two existing HTTP fixtures for observation."""
import argparse
import copy
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    'default_profile_stage', Path(__file__).with_name('stage.py'))
stage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stage)
sha, save = stage.sha, stage.save
ELF_SHA = 'c1bdb0a7599ba3f61d9cb48597069a66f6e27e02f38cf32e495fc80059008636'
RELEASE_SHA = '0efc2c0dc309e42a7b7761860edce7888aa8139f5ea305f54eae054a81fdbcf3'


def prepare(original):
    if subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT):
        raise RuntimeError('commit the frozen driver before preparation')
    release_path = original / 'build/q4t.release.json'
    release = json.loads(release_path.read_text())
    if sha(original / 'build/q4t') != ELF_SHA or sha(release_path) != RELEASE_SHA:
        raise RuntimeError('accepted default identity changed')
    source_manifest = Path(release['tested_source_manifest'])
    if sha(source_manifest) != release['tested_source_manifest_sha256']:
        raise RuntimeError('accepted source manifest changed')
    source = json.loads(source_manifest.read_text())
    for name, digest in source.items():
        if sha(original / name) != digest:
            raise RuntimeError('main differs from accepted runtime: ' + name)
    base = ROOT / '.q4t-work/default-verify-profile-20261106'
    frozen = base / 'frozen'
    frozen.mkdir(parents=True, exist_ok=False)
    for src, name in [(original / 'build/q4t', 'q4t'),
                      (release_path, 'q4t.release.json'),
                      (source_manifest, 'runtime-source.json')]:
        shutil.copy2(src, frozen / name)
        if sha(src) != sha(frozen / name):
            raise RuntimeError('frozen copy mismatch')
    old_path = original / '.q4t-work/mtp-prefill-release-20261029/phase-a/plan.json'
    old = json.loads(old_path.read_text())
    cases = [copy.deepcopy(old['protection'][i]) for i in (0, 3)]
    for case in cases:
        src = Path(case['path'])
        if sha(src) != old['files'][str(src)]:
            raise RuntimeError('existing fixture changed')
        target = frozen / (case['id'] + '.jsonl')
        # Exactly one existing request per context, no synthetic warmup.
        target.write_text(src.read_text().splitlines()[0] + '\n')
        case.update(path=str(target), count=1)
    path = base / 'plan.json'
    plan = dict(stage='DEFAULT_VERIFY_PROFILE', output=str(base / 'run'),
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'],
            cwd=ROOT, text=True).strip(),
        runtime_source_commit=release['source_commit'],
        baseline_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'],
            cwd=original, text=True).strip(),
        execution_tools_root=str(original), candidate=str(frozen / 'q4t'),
        model=old['model'], model_file_metadata=old['model_file_metadata'],
        artifact=release['subset_artifact'], port=8762,
        http_requests=12, http_loads=6, stage_timeout_seconds=1800,
        task_deadline_unix=time.time() + 1800, direct_contract_timeout_seconds=0,
        resource_sample_max_age_seconds=10,
        telemetry_sources=stage.telemetry.discover_sources(), steps=[], files={})
    for mode in stage.MODES:
        for head in ('full', 'subset'):
            name = f'{len(plan["steps"]):02d}-{mode}-{head}'
            command = [plan['candidate'], 'serve', '--model-dir', plan['model'],
                '--host', '127.0.0.1', '--port', str(plan['port']),
                '--max-seq', '1', '--max-prefill', '8192', '--max-len', '208896',
                '--max-tokens', '256', '--startup-plan', 'off', '--mtp', '--mtp-k', '3']
            if head == 'subset':
                command += ['--mtp-subset-head', plan['artifact']]
            plan['steps'].append(dict(name=name, mode=mode, head=head,
                kind='observation', http_requests=2, http_loads=1, timeout=360,
                cases=copy.deepcopy(cases), server_command=command,
                command=[sys.executable, '-B', str(Path(__file__).with_name('stage.py')),
                         '--plan', str(path), '--step', name]))
    tracked = subprocess.check_output(['git', 'ls-files'], cwd=ROOT,
                                      text=True).splitlines()
    files = {ROOT / p for p in tracked if p.startswith('tools/')}
    files.update(original / p for p in source)
    files.update(p for p in (original / 'tools').rglob('*.py')
                 if not any(x.startswith('.') for x in p.relative_to(original).parts))
    files.update(p for p in frozen.rglob('*') if p.is_file())
    files.update(p for p in Path(plan['artifact']).rglob('*') if p.is_file())
    files.update([old_path, source_manifest, release_path, original / 'build/q4t',
        Path(sys.executable).resolve(),
        ROOT / 'docs/DEFAULT_VERIFY_PROFILE_PLAN_2026-11-06.md',
        original / 'tools/evalscope/.venv/bin/evalscope'])
    for folder in (original / 'tools/evalscope/.venv/lib').glob('python*/site-packages/evalscope'):
        files.update(folder.rglob('*.py'))
    plan['files'] = {str(p): sha(p) for p in sorted(files) if p.is_file()}
    save(path, plan)
    stage.validate(plan)
    stage.legacy.verify_files(plan)
    print(json.dumps(dict(plan=str(path), files=len(plan['files']),
                         accepted_runtime_sources=len(source))))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--original-root', type=Path, required=True)
    args = parser.parse_args()
    prepare(args.original_root.resolve())
