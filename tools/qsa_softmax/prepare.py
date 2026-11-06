"""Freeze one runtime-file delta, existing fixtures, and the HTTP A budget."""
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

spec = importlib.util.spec_from_file_location('qsa_stage',
                                              Path(__file__).with_name('stage.py'))
stage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stage)
ROOT, sha, save, require = stage.ROOT, stage.sha, stage.save, stage.require
PARENT_SHA = 'aae93a734be67d1695f4c6f3d06aae35de55ed9126994d65ad4bb79946b074b4'


def prepare(original):
    require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT),
            'commit before freezing')
    base = ROOT / '.q4t-work/qsa-softmax-20261106'
    build = base / 'engine'
    for name in ('configure.log', 'build.log'):
        require(not re.search(r'\bwarning:', (base / name).read_text(), re.I),
                'build warnings')
    built = json.loads((base / 'build-exit.json').read_text())
    require(built['returncode'] == 0 and built['source_sha256'] ==
            sha(ROOT / 'src/model/full_attention.cu'), 'build identity/failure')
    release_path = original / 'build/q4t.release.json'
    require(sha(original / 'build/q4t') ==
            'c1bdb0a7599ba3f61d9cb48597069a66f6e27e02f38cf32e495fc80059008636' and
            sha(release_path) ==
            '0efc2c0dc309e42a7b7761860edce7888aa8139f5ea305f54eae054a81fdbcf3',
            'accepted default identity changed')
    release = json.loads(release_path.read_text())
    default_manifest = Path(release['tested_source_manifest'])
    require(sha(default_manifest) == release['tested_source_manifest_sha256'],
            'accepted source manifest changed')
    default_sources = json.loads(default_manifest.read_text())
    require(all(sha(original / n) == h for n, h in default_sources.items()),
            'main runtime differs from accepted baseline')
    previous = ROOT / '.q4t-work/reference-refresh-acceptance-20261106'
    parent = previous / 'frozen/candidate'
    parent_manifest = previous / 'frozen/candidate-runtime-source.json'
    old_plan = json.loads((previous / 'plan.json').read_text())
    require(sha(parent) == PARENT_SHA and sha(parent_manifest) ==
            old_plan['files'][str(parent_manifest)], 'short-accepted parent identity')
    sources = json.loads(parent_manifest.read_text())
    require(sources.keys() == default_sources.keys(), 'runtime source coverage')
    changed = {n for n, h in sources.items() if sha(ROOT / n) != h}
    require(changed == {'src/model/full_attention.cu'}, 'single-file runtime scope')
    protocol = build / 'generated/q4t/server/chat_protocol_identity.h'
    require(sha(protocol) == sha(previous /
            'engine/generated/q4t/server/chat_protocol_identity.h'),
            'protocol changed beyond R1/R2 parent')
    frozen = base / 'frozen'
    frozen.mkdir(exist_ok=False)
    for src, name in [(parent, 'parent'), (parent_manifest, 'parent-runtime-source.json'),
                      (build / 'q4t', 'candidate'),
                      (release_path, 'installed-default.release.json'),
                      (default_manifest, 'installed-default-runtime-source.json'),
                      (build / 'CMakeCache.txt', 'CMakeCache.txt')]:
        shutil.copy2(src, frozen / name)
        require(sha(src) == sha(frozen / name), 'frozen copy mismatch')
    save(frozen / 'candidate-runtime-source.json', {n: sha(ROOT / n) for n in sources})
    old_path = original / '.q4t-work/mtp-prefill-release-20261029/phase-a/plan.json'
    old = json.loads(old_path.read_text())
    cases = [copy.deepcopy(old['protection'][i]) for i in (0, 3)]
    cases.append(copy.deepcopy(old['quality_case']))
    for case, (_, count, tokens, parallel) in zip(cases, stage.CASES):
        source = Path(case['path'])
        require(sha(source) == old['files'][str(source)], 'existing fixture changed')
        target = frozen / (case['id'] + '.jsonl')
        shutil.copy2(source, target)
        case.update(path=str(target), tokens=tokens, count=count, parallel=parallel)
    plan_path = base / 'plan.json'
    plan = dict(stage='QSA_SOFTMAX_FEASIBILITY', output=str(base / 'run'),
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'],
            cwd=ROOT, text=True).strip(), parent_runtime_commit=
        '5d641ea4cb0d18c3f74ba3ea8515d4824e583640',
        runtime_delta=sorted(changed), execution_tools_root=str(original),
        candidate=str(frozen / 'candidate'), parent=str(frozen / 'parent'),
        model=old['model'], model_file_metadata=old['model_file_metadata'],
        quality=old['quality'], port=8762, http_requests=34, http_loads=2,
        minimum_ttft_gain=.03, stage_timeout_seconds=1140,
        approved_total_wall_seconds=1200, task_deadline_unix=time.time() + 1140,
        direct_contract_timeout_seconds=0, resource_sample_max_age_seconds=10,
        telemetry_sources=stage.telemetry.discover_sources(), steps=[], files={})
    for index, (head, arm) in enumerate(stage.ORDER):
        name = f'{index:02d}-{head}-{arm}'
        plan['steps'].append(dict(name=name, head=head, arm=arm,
            kind='matrix', http_requests=17, http_loads=1, timeout=570,
            cases=copy.deepcopy(cases), server_command=stage.server_command(plan, arm),
            command=[sys.executable, '-B', str(Path(__file__).with_name('stage.py')),
                     '--plan', str(plan_path), '--step', name]))
    tracked = subprocess.check_output(['git', 'ls-files'], cwd=ROOT,
                                      text=True).splitlines()
    files = {ROOT / n for n in tracked if n.startswith(
        ('src/', 'include/', 'tools/', 'cmake/')) or n in (
        'CMakeLists.txt', 'docs/QSA_SOFTMAX_PLAN_2026-11-06.md')}
    files.update(original / n for n in default_sources)
    files.update(p for p in (original / 'tools').rglob('*.py')
                 if not any(x.startswith('.') for x in p.relative_to(original).parts))
    files.update(p for p in frozen.rglob('*') if p.is_file())
    files.update([old_path, default_manifest, release_path, original / 'build/q4t',
        parent, parent_manifest, previous / 'plan.json',
        base / 'build.log', base / 'configure.log', base / 'build-exit.json',
        base / 'build-command.json', build / 'compile_commands.json', build / 'q4t',
        Path(sys.executable).resolve(), original / 'tools/evalscope/.venv/bin/evalscope'])
    files.update((build / 'generated').rglob('*'))
    for folder in (original / 'tools/evalscope/.venv/lib').glob('python*/site-packages/evalscope'):
        files.update(folder.rglob('*.py'))
    plan['files'] = {str(p): sha(p) for p in sorted(files) if p.is_file()}
    stage.validate(plan)
    stage.legacy.verify_files(plan)
    save(plan_path, plan)
    print(json.dumps(dict(plan=str(plan_path), inputs=len(plan['files']),
        parent_sha256=sha(frozen / 'parent'), candidate_sha256=sha(frozen / 'candidate'),
        runtime_delta=sorted(changed))))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--original-root', type=Path, required=True)
    args = parser.parse_args()
    prepare(args.original_root.resolve())
