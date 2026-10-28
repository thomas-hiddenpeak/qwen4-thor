"""Bind one candidate and existing acceptance inputs before the first request."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / '.q4t-work/mtp-prefill-scoring-20261029'
spec = importlib.util.spec_from_file_location('score_driver', ROOT / 'tools/mtp_prefill_scoring/run_stage.py')
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)
sha, save = driver.legacy.sha, driver.save


def main():
    old = json.loads((ROOT / '.q4t-work/prefix-seed-20261028/phase-d/plan.json').read_text())
    frozen = BASE / 'frozen'
    frozen.mkdir()
    for source, name in [(ROOT / 'build/q4t', 'parent'), (ROOT / 'build/q4t.release.json', 'parent.release.json'),
                         (BASE / 'build/q4t', 'q4t'), (BASE / 'build/q4t_prefill_scoring_contract', 'state-contract'),
                         (BASE / 'build/CMakeCache.txt', 'CMakeCache.txt')]:
        shutil.copy2(source, frozen / name)
    if sha(frozen / 'parent') != '6f3722b26e5b9d1b08d405e873d1f90d72f4b5c1aec2dd14589d1e27390b7965':
        raise ValueError('delivered parent changed')
    refs = json.loads((ROOT / 'docs/evidence/prefix-seed-20261028/protection-review.json').read_text())['pairs']
    cases = []
    for old_case in old['protection']:
        if old_case['id'] not in ['context-8192', 'context-45056', 'context-204800']: continue
        c = copy.deepcopy(old_case)
        dest = frozen / Path(c['path']).name
        shutil.copy2(c['path'], dest)
        c.update(path=str(dest), count=1, parallel=1)
        r = next(r for r in refs if r['head'] == 'full' and r['tier'] == c['id'])
        body = json.loads(dest.read_text().splitlines()[0])
        c.update(input_tokens=r['input_tokens'], prompt_sha256=hashlib.sha256(body['prompt'].encode()).hexdigest())
        full_result = json.loads((ROOT / '.q4t-work/prefix-seed-20261028/phase-d/run/01-full-on/results.json').read_text())
        c['output_sha256'] = next(r for r in full_result if r['id'] == c['id'])['rows'][0]['output_sha256']
        cases.append(c)
    plan = dict(stage='PREFILL_SCORE', candidate=str(frozen / 'q4t'), parent=str(frozen / 'parent'),
        candidate_sha256=sha(frozen / 'q4t'), parent_sha256=sha(frozen / 'parent'),
        state_contract=str(frozen / 'state-contract'), state_contract_sha256=sha(frozen / 'state-contract'),
        model=old['model'], artifact=old['artifact'], model_file_metadata=old['model_file_metadata'],
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
        port=8762, output=str(BASE / 'run'), steps=[], http_requests=16, http_loads=8,
        maximum_gpu_contracts=2, stage_timeout_seconds=5400, task_deadline_unix=time.time()+5400,
        direct_contract_timeout_seconds=1200, resource_sample_max_age_seconds=10,
        telemetry_sources=driver.reuse.telemetry.discover_sources(), files={})
    path = BASE / 'plan.json'
    def http_step(head, arm, mode):
        index = len(plan['steps']); name=f'{index:02d}-{head}-{arm}-{mode}'
        selected = cases if mode == 'normal' else cases[-1:]
        binary = plan['parent'] if mode == 'normal' and arm == 'parent' else plan['candidate']
        env = {} if mode == 'normal' else {'Q4T_PREFILL_SCORING_TIMING': '1'}
        if mode == 'diagnostic' and arm == 'parent': env['Q4T_MTP_PREFILL_LEGACY_SCORING'] = '1'
        plan['steps'].append(dict(name=name, head=head, arm=arm, mode=mode, kind='block',
            cases=selected, binary=binary, server_env=env, http_requests=len(selected), http_loads=1,
            timeout=650, command=[sys.executable, '-B', str(ROOT / 'tools/mtp_prefill_scoring/run_stage.py'),
                '--plan', str(path), '--custom-step', name]))
    for head, arm in [('full','candidate'),('full','parent'),('subset','parent'),('subset','candidate')]:
        http_step(head,arm,'normal')
    name='04-benefit-gate'
    plan['steps'].append(dict(name=name,kind='analysis',http_requests=0,http_loads=0,timeout=30,
        command=[sys.executable,'-B',str(ROOT/'tools/mtp_prefill_scoring/run_stage.py'),'--plan',str(path),'--custom-step',name]))
    for head in ['full','subset']:
        name=f'{len(plan["steps"]):02d}-{head}-state'
        command=[plan['state_contract'],plan['model'],str(BASE/('cache-'+head))]
        if head=='subset':command.append(plan['artifact'])
        plan['steps'].append(dict(name=name,head=head,kind='contract',http_requests=0,http_loads=0,
            timeout=600,command=command))
    for head, arm in [('full','parent'),('full','candidate'),('subset','candidate'),('subset','parent')]:
        http_step(head,arm,'diagnostic')
    files=[p for group in ['src','include','tools'] for p in (ROOT/group).rglob('*')
           if p.is_file() and '.venv' not in p.parts and p.suffix in ['.cpp','.cu','.h','.py']]
    files += [ROOT/'CMakeLists.txt',ROOT/'docs/MTP_PREFILL_SCORING_PLAN_2026-10-29.md']
    files += [p for p in frozen.rglob('*') if p.is_file()]
    files += [Path(plan['artifact'])/n for n in ['head.bf16','token_ids.bin','manifest.json']]
    plan['files']={str(p):sha(p) for p in files}
    save(path,plan)
    (frozen/'source.diff').write_bytes(subprocess.check_output(['git','diff','HEAD']))
    print(json.dumps({k:plan[k] for k in ['candidate_sha256','parent_sha256','state_contract_sha256','task_deadline_unix','http_requests','http_loads']},indent=2))


if __name__=='__main__': main()
