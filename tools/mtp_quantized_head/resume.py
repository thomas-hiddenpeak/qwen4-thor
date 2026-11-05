"""Continue only the three unstarted arms, keeping the original total deadline."""
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
spec = importlib.util.spec_from_file_location('quantized_resume_stage',
    Path(__file__).with_name('stage.py'))
stage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stage)
sha, save = stage.sha, stage.save


def resume():
    if subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT):
        raise RuntimeError('commit the repaired candidate and driver first')
    old_path = ROOT / '.q4t-work/feasibility/plan.json'
    old = json.loads(old_path.read_text())
    old_out = Path(old['output'])
    state = json.loads((old_out / 'state.json').read_text())
    if (state['status'], state['current'], state['loads_started']) != (
            'STOPPED', '00-plain', 1):
        raise RuntimeError('unexpected prior stage or additional loads')
    seal_path = old_out / 'files.json'
    for name, digest in json.loads(seal_path.read_text()).items():
        if sha(old_out / name) != digest:
            raise RuntimeError('original HTTP evidence changed: ' + name)
    plain = old_out / '00-plain'
    audit = stage.audit_server(plain, 14)
    cases = json.loads((plain / 'results.json').read_text())
    if [len(c['rows']) for c in cases] != [3, 11]:
        raise RuntimeError('partial plain evidence')
    base = ROOT / '.q4t-work/feasibility-resume'
    base.mkdir()
    frozen = base / 'frozen'
    frozen.mkdir()
    build = ROOT.parent / 'engine'
    if json.loads((ROOT.parent / 'build-03-exit.json').read_text())['returncode']:
        raise RuntimeError('rebuild did not pass')
    if re.search(r'\bwarning:', (ROOT.parent / 'build-03.log').read_text(), re.I):
        raise RuntimeError('rebuild warnings')
    shutil.copy2(build / 'q4t', frozen / 'candidate')
    if sha(build / 'q4t') != sha(frozen / 'candidate'):
        raise RuntimeError('candidate copy differs')
    plan = copy.deepcopy(old)
    plan['source_commit'] = subprocess.check_output(['git', 'rev-parse', 'HEAD'],
        cwd=ROOT, text=True).strip()
    plan['output'] = str(base / 'run')
    plan['candidate'] = str(frozen / 'candidate')
    plan['reused_plain'] = str(plain)
    plan['original_plan'] = str(old_path)
    plan['reused_requests'] = 14
    plan['http_requests'], plan['http_loads'] = 42, 3
    plan['stage_timeout_seconds'] = int(old['task_deadline_unix'] - time.time())
    plan['steps'] = plan['steps'][1:]
    plan_path = base / 'plan.json'
    for step in plan['steps']:
        step['command'] = ['/usr/bin/python3', '-B',
            str(ROOT / 'tools/mtp_quantized_head/stage.py'), '--plan',
            str(plan_path), '--step', step['name']]
        if step['head'] == 'nvfp4':
            step['server_command'][0] = plan['candidate']
    # Verify unchanged input/parent/dependency identities, then replace only
    # the source hashes changed by the explicitly committed repair.
    plan['files'] = {}
    tracked = subprocess.check_output(['git', 'ls-files'], cwd=ROOT,
                                     text=True).splitlines()
    for name, digest in old['files'].items():
        path = Path(name)
        if path.is_relative_to(ROOT) and str(path.relative_to(ROOT)) in tracked:
            continue
        if sha(path) != digest:
            raise RuntimeError('unrelated frozen dependency changed: ' + name)
        plan['files'][name] = digest
    inputs = [ROOT / p for p in tracked if p.startswith(('src/', 'include/',
        'tools/', 'cmake/')) or p in ('CMakeLists.txt',
        'docs/MTP_QUANTIZED_HEAD_PLAN_2026-11-05.md')]
    inputs += [p for p in old_out.rglob('*') if p.is_file()]
    inputs += [old_path, frozen / 'candidate', ROOT.parent / 'build-03.log',
               ROOT.parent / 'build-03-exit.json']
    plan['files'].update({str(p): sha(p) for p in inputs})
    save(base / 'reused-plain-audit.json', audit)
    stage.validate(plan)
    stage.legacy.verify_files(plan)
    save(plan_path, plan)
    print(plan_path)


if __name__ == '__main__':
    resume()
