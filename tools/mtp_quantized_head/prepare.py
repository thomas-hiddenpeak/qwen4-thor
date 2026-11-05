"""Freeze existing inputs, current identities and a single HTTP gate."""
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
spec = importlib.util.spec_from_file_location('quantized_stage',
    Path(__file__).with_name('stage.py'))
stage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stage)
sha, save = stage.sha, stage.save


def prepare(original, build):
    if subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT):
        raise RuntimeError('commit candidate before freezing')
    base = ROOT / '.q4t-work/feasibility'
    base.mkdir(parents=True)
    frozen = base / 'frozen'
    frozen.mkdir()
    for name in ('configure.log', 'build.log', 'build-02.log'):
        if re.search(r'\bwarning:', (build.parent / name).read_text(), re.I):
            raise RuntimeError('build warning')
    if json.loads((build.parent / 'build-02-exit.json').read_text())['returncode']:
        raise RuntimeError('candidate build failed')
    release_path = original / 'build/q4t.release.json'
    release = json.loads(release_path.read_text())
    if sha(original / 'build/q4t') != (
            'c1bdb0a7599ba3f61d9cb48597069a66f6e27e02f38cf32e495fc80059008636'):
        raise RuntimeError('accepted parent changed')
    if sha(release_path) != '0efc2c0dc309e42a7b7761860edce7888aa8139f5ea305f54eae054a81fdbcf3':
        raise RuntimeError('parent metadata changed')
    for source, target in [(original / 'build/q4t', 'parent'),
            (release_path, 'parent.release.json'), (build / 'q4t', 'candidate'),
            (build / 'CMakeCache.txt', 'CMakeCache.txt')]:
        shutil.copy2(source, frozen / target)
        if sha(source) != sha(frozen / target):
            raise RuntimeError('frozen copy mismatch')
    old_path = original / '.q4t-work/mtp-prefill-release-20261029/phase-a/plan.json'
    old = json.loads(old_path.read_text())
    cases = [copy.deepcopy(old['protection'][0]), copy.deepcopy(old['quality_case'])]
    for case in cases:
        source = Path(case['path'])
        if sha(source) != old['files'][str(source)]:
            raise RuntimeError('accepted input changed')
        target = frozen / (case['id'] + '.jsonl')
        shutil.copy2(source, target)
        case['path'] = str(target)
    artifact = Path(release['subset_artifact'])
    plan_path = base / 'plan.json'
    plan = dict(stage='QUANTIZED_HEAD_FEASIBILITY', output=str(base / 'run'),
        candidate=str(frozen / 'candidate'), parent=str(frozen / 'parent'),
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'],
            cwd=ROOT, text=True).strip(),
        baseline_commit='d8880693d050385cf155655b62196aee1ff50b7a',
        model=old['model'], model_file_metadata=old['model_file_metadata'],
        artifact=str(artifact), quality=old['quality'], port=8762,
        execution_tools_root=str(original), http_requests=56, http_loads=4,
        stage_timeout_seconds=1800, task_deadline_unix=time.time() + 1800,
        direct_contract_timeout_seconds=0, resource_sample_max_age_seconds=10,
        telemetry_sources=stage.telemetry.discover_sources(), steps=[], files={})
    for index, head in enumerate(('plain', 'full', 'nvfp4', 'subset')):
        binary = plan['candidate' if head == 'nvfp4' else 'parent']
        command = [binary, 'serve', '--model-dir', plan['model'], '--host',
            '127.0.0.1', '--port', str(plan['port']), '--max-seq', '1',
            '--max-prefill', '8192', '--max-len', '208896', '--max-tokens',
            '256', '--startup-plan', 'off']
        command += ['--no-mtp'] if head == 'plain' else ['--mtp', '--mtp-k', '3']
        if head == 'nvfp4':
            command += ['--mtp-head-quantization', 'nvfp4']
        if head == 'subset':
            command += ['--mtp-subset-head', str(artifact)]
        name = f'{index:02d}-{head}'
        plan['steps'].append(dict(name=name, kind='matrix', head=head,
            http_requests=14, http_loads=1, timeout=600, cases=copy.deepcopy(cases),
            server_command=command, command=['/usr/bin/python3', '-B',
                str(ROOT / 'tools/mtp_quantized_head/stage.py'), '--plan',
                str(plan_path), '--step', name]))
    tracked = subprocess.check_output(['git', 'ls-files'], cwd=ROOT,
                                     text=True).splitlines()
    inputs = [ROOT / p for p in tracked if p.startswith(('src/', 'include/',
        'tools/', 'cmake/')) or p in ('CMakeLists.txt',
        'docs/MTP_QUANTIZED_HEAD_PLAN_2026-11-05.md')]
    inputs += [p for p in frozen.iterdir() if p.is_file()]
    inputs += [artifact / name for name in ('manifest.json', 'head.bf16',
                                           'token_ids.bin')]
    inputs += [original / 'build/q4t', release_path]
    inputs += [build.parent / n for n in ('configure.log', 'build.log',
                                         'build-02.log', 'build-02-exit.json')]
    plan['files'] = {str(p): sha(p) for p in inputs}
    stage.validate(plan)
    stage.legacy.verify_files(plan)
    save(plan_path, plan)
    print(plan_path)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--original', type=Path, required=True)
    parser.add_argument('--build', type=Path, required=True)
    args = parser.parse_args()
    prepare(args.original.resolve(), args.build.resolve())
