"""Freeze a clean candidate and existing inputs for the first HTTP gate."""
import argparse
import copy
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('selected_stage',
                                              Path(__file__).with_name('stage.py'))
stage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stage)
sha, save = stage.sha, stage.save


def prepare(original, build):
    if subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT):
        raise RuntimeError('commit before freezing')
    for name in ('configure.log', 'build.log'):
        if re.search(r'\bwarning:', (build.parent / name).read_text(), re.I):
            raise RuntimeError('build warnings')
    if json.loads((build.parent / 'build-exit.json').read_text())['returncode'] != 0:
        raise RuntimeError('build did not succeed')
    release_path = original / 'build/q4t.release.json'
    release = json.loads(release_path.read_text())
    if (sha(original / 'build/q4t') !=
            'c1bdb0a7599ba3f61d9cb48597069a66f6e27e02f38cf32e495fc80059008636' or
            sha(release_path) !=
            '0efc2c0dc309e42a7b7761860edce7888aa8139f5ea305f54eae054a81fdbcf3'):
        raise RuntimeError('accepted default identity changed')
    base = ROOT / '.q4t-work/feasibility'
    base.mkdir(parents=True, exist_ok=False)
    frozen = base / 'frozen'
    frozen.mkdir()
    for src, name in [(original / 'build/q4t', 'parent'),
                      (release_path, 'parent.release.json'),
                      (build / 'q4t', 'candidate'),
                      (build / 'CMakeCache.txt', 'CMakeCache.txt')]:
        shutil.copy2(src, frozen / name)
        if sha(src) != sha(frozen / name):
            raise RuntimeError('frozen copy mismatch')
    old_path = original / '.q4t-work/mtp-prefill-release-20261029/phase-a/plan.json'
    old = json.loads(old_path.read_text())
    cases = [copy.deepcopy(old['protection'][0]), copy.deepcopy(old['quality_case'])]
    for c, tokens, count in zip(cases, (256, 32), (3, 11)):
        source = Path(c['path'])
        if sha(source) != old['files'][str(source)]:
            raise RuntimeError('existing input identity changed')
        target = frozen / (c['id'] + '.jsonl')
        shutil.copy2(source, target)
        c.update(path=str(target), tokens=tokens, count=count, parallel=1)
    plan_path = base / 'plan.json'
    plan = dict(stage='SELECTED_HEAD_FEASIBILITY', output=str(base / 'run'),
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'],
            cwd=ROOT, text=True).strip(), baseline_commit='def8cc96e7f94b50fbc917303c8570ec23e841ea',
        execution_tools_root=str(original), candidate=str(frozen / 'candidate'),
        parent=str(frozen / 'parent'), model=old['model'],
        model_file_metadata=old['model_file_metadata'], artifact=release['subset_artifact'],
        quality=old['quality'], port=8762, http_requests=56, http_loads=4,
        minimum_decode_gain=.03, stage_timeout_seconds=1800,
        task_deadline_unix=time.time() + 1800, direct_contract_timeout_seconds=0,
        resource_sample_max_age_seconds=10,
        telemetry_sources=stage.telemetry.discover_sources(), steps=[], files={})
    for index, (head, arm) in enumerate([
            ('full', 'parent'), ('full', 'candidate'),
            ('subset', 'candidate'), ('subset', 'parent')]):
        name = f'{index:02d}-{head}-{arm}'
        command = [plan[arm], 'serve', '--model-dir', plan['model'],
            '--host', '127.0.0.1', '--port', str(plan['port']), '--max-seq', '1',
            '--max-prefill', '8192', '--max-len', '208896', '--max-tokens', '256',
            '--startup-plan', 'off', '--mtp', '--mtp-k', '3']
        if head == 'subset':
            command += ['--mtp-subset-head', plan['artifact']]
        plan['steps'].append(dict(name=name, head=head, arm=arm, kind='matrix',
            http_requests=14, http_loads=1, timeout=600, cases=copy.deepcopy(cases),
            server_command=command, command=[sys.executable, '-B',
                str(ROOT / 'tools/mtp_selected_head/stage.py'), '--plan', str(plan_path),
                '--step', name]))
    tracked = subprocess.check_output(['git', 'ls-files'], cwd=ROOT, text=True).splitlines()
    files = {ROOT / name for name in tracked if name.startswith(
        ('src/', 'include/', 'tools/', 'cmake/')) or name in (
        'CMakeLists.txt', 'docs/MTP_SELECTED_HEAD_PLAN_2026-11-06.md')}
    files.update(p for p in frozen.rglob('*') if p.is_file())
    files.update(p for p in Path(plan['artifact']).rglob('*') if p.is_file())
    files.update((old_path, build.parent / 'build.log', build.parent / 'configure.log',
                  build.parent / 'build-exit.json', Path(sys.executable).resolve()))
    files.add(original / 'tools/evalscope/.venv/bin/evalscope')
    for folder in (original / 'tools/evalscope/.venv/lib').glob('python*/site-packages/evalscope'):
        files.update(folder.rglob('*.py'))
    plan['files'] = {str(p): sha(p) for p in sorted(files) if p.is_file()}
    save(plan_path, plan)
    stage.validate(plan)
    print(json.dumps(dict(plan=str(plan_path), files=len(plan['files']),
                         candidate_sha256=sha(frozen / 'candidate'))))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--original-root', type=Path, required=True)
    parser.add_argument('--build', type=Path, required=True)
    args = parser.parse_args()
    prepare(args.original_root.resolve(), args.build.resolve())
