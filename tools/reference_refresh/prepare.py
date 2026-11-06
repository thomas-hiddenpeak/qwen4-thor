"""Freeze the approved six-start, twelve-request stage before GPU access."""
import copy
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

spec = importlib.util.spec_from_file_location('refresh_stage',
    Path(__file__).with_name('stage.py'))
stage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stage)
ROOT, sha, save = stage.ROOT, stage.sha, stage.save
ORIGINAL = Path('/home/rm01/models/dev/qwen4-thor')
BASE = ROOT / '.q4t-work/reference-refresh-acceptance-20261106'


def prepare():
    stage.require(not subprocess.check_output(
        ['git', 'status', '--porcelain'], cwd=ROOT), 'commit stage first')
    build = json.loads((BASE / 'build-cpu.json').read_text())
    stage.require([r['name'] for r in build] == ['build-config-contract',
        'config-contract', 'build-qsa-contract', 'qsa-contract',
        'serve-inventory', 'configure', 'build'] and all(
            r['returncode'] == 0 and sha(BASE / (r['name'] + '.log')) ==
            r['log_sha256'] for r in build), 'build/CPU receipts')
    for name in ('config-contract', 'qsa-contract'):
        stage.require('7 tests, 7 passed, 0 failed, 0 skipped' in
            (BASE / (name + '.log')).read_text(), 'CPU completion')
    identity = json.loads((BASE / 'build-identity.json').read_text())
    stage.require(all(sha(ROOT / n) == h for n, h in identity['sources'].items())
        and sha(BASE / 'engine/q4t') == identity['candidate_sha256'],
        'built/tested identity')
    release_path = ORIGINAL / 'build/q4t.release.json'
    stage.require(sha(ORIGINAL / 'build/q4t') ==
        'c1bdb0a7599ba3f61d9cb48597069a66f6e27e02f38cf32e495fc80059008636'
        and sha(release_path) ==
        '0efc2c0dc309e42a7b7761860edce7888aa8139f5ea305f54eae054a81fdbcf3',
        'accepted default changed')
    release = json.loads(release_path.read_text())
    manifest = Path(release['tested_source_manifest'])
    stage.require(sha(manifest) == release['tested_source_manifest_sha256'],
                  'accepted source manifest changed')
    sources = json.loads(manifest.read_text())
    stage.require(all(sha(ORIGINAL / n) == h for n, h in sources.items()),
                  'main differs from accepted runtime')
    changed = {n for n, h in sources.items() if sha(ROOT / n) != h}
    stage.require(changed == {'src/io/model_config.cpp',
        'include/q4t/io/model_config.h', 'include/q4t/model/layer_subtiming.h',
        'src/model/full_attention.cu', 'src/model/model.cu'}, 'runtime scope')
    frozen = BASE / 'frozen'
    frozen.mkdir(exist_ok=False)
    for src, name in [(ORIGINAL / 'build/q4t', 'baseline'),
        (BASE / 'engine/q4t', 'candidate'), (release_path, 'baseline.release.json'),
        (manifest, 'baseline-runtime-source.json')]:
        shutil.copy2(src, frozen / name)
        stage.require(sha(src) == sha(frozen / name), 'frozen copy')
    save(frozen / 'candidate-runtime-source.json',
         {n: sha(ROOT / n) for n in sorted(sources)})
    old_path = ORIGINAL / '.q4t-work/mtp-prefill-release-20261029/phase-a/plan.json'
    old = json.loads(old_path.read_text())
    cases = [copy.deepcopy(old['protection'][i]) for i in (0, 3)]
    for case in cases:
        src = Path(case['path'])
        stage.require(sha(src) == old['files'][str(src)], 'fixture identity')
        target = frozen / (case['id'] + '.jsonl')
        target.write_text(src.read_text().splitlines()[0] + '\n')
        case.update(path=str(target), count=1)
    path = BASE / 'plan.json'
    plan = dict(stage='REFERENCE_REFRESH_R1_R2', output=str(BASE / 'run'),
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'],
            cwd=ROOT, text=True).strip(), execution_tools_root=str(ORIGINAL),
        baseline=str(frozen / 'baseline'), candidate=str(frozen / 'candidate'),
        model=old['model'], model_file_metadata=old['model_file_metadata'],
        artifact=release['subset_artifact'], port=8762, http_loads=6,
        http_requests=12, stage_timeout_seconds=1140,
        approved_total_wall_seconds=1200, task_deadline_unix=time.time() + 1140,
        direct_contract_timeout_seconds=0, resource_sample_max_age_seconds=10,
        telemetry_sources=stage.telemetry.discover_sources(), steps=[], files={})
    for mode in stage.MODES:
        for head in ('full', 'subset'):
            name = f'{len(plan["steps"]):02d}-{mode}-{head}'
            tool = mode == 'off' and head == 'full'
            selected = copy.deepcopy(cases[1:] if mode == 'on' else cases)
            binary = plan['baseline'] if mode == 'baseline' else plan['candidate']
            command = [binary, 'serve', '--model-dir', plan['model'], '--host',
                '127.0.0.1', '--port', str(plan['port']), '--max-seq', '1',
                '--max-prefill', '8192', '--max-len', '208896', '--max-tokens',
                '256', '--startup-plan', 'off', '--mtp', '--mtp-k', '3']
            if head == 'subset':
                command += ['--mtp-subset-head', plan['artifact']]
            plan['steps'].append(dict(name=name, mode=mode, head=head,
                tool_loop=tool, kind='acceptance', http_loads=1,
                http_requests=len(selected) + 2 * tool, timeout=300,
                cases=selected, server_command=command, command=[
                    sys.executable, '-B', str(Path(__file__).with_name('stage.py')),
                    '--plan', str(path), '--step', name]))
    tracked = subprocess.check_output(['git', 'ls-files'], cwd=ROOT,
                                      text=True).splitlines()
    files = {ROOT / n for n in tracked if n.startswith(('tools/', 'src/',
              'include/', 'cmake/')) or n == 'CMakeLists.txt'}
    files.update(ORIGINAL / n for n in sources)
    files.update(p for p in (ORIGINAL / 'tools').rglob('*.py')
                 if not any(x.startswith('.') for x in p.relative_to(ORIGINAL).parts))
    files.update(p for p in frozen.rglob('*') if p.is_file())
    files.update(p for p in Path(plan['artifact']).rglob('*') if p.is_file())
    files.update([old_path, manifest, release_path, ORIGINAL / 'build/q4t',
        ROOT / 'docs/MODEL.md', ROOT / 'docs/REFERENCE_REFRESH_ACCEPTANCE_2026-11-06.md',
        BASE / 'build-cpu.json', BASE / 'build-identity.json', BASE / 'engine/q4t',
        Path(sys.executable).resolve(),
        ORIGINAL / 'tools/evalscope/.venv/bin/evalscope'])
    files.update((BASE / 'engine/generated').rglob('*'))
    files.update(BASE / (r['name'] + '.log') for r in build)
    for folder in (ORIGINAL / 'tools/evalscope/.venv/lib').glob('python*/site-packages/evalscope'):
        files.update(folder.rglob('*.py'))
    plan['files'] = {str(p): sha(p) for p in sorted(files) if p.is_file()}
    stage.validate(plan)
    stage.legacy.verify_files(plan)
    save(path, plan)
    print(json.dumps(dict(plan=str(path), inputs=len(plan['files']),
                         runtime_changed=sorted(changed))))


if __name__ == '__main__':
    prepare()
