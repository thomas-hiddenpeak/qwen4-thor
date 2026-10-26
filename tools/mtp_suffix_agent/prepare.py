"""Freeze agent workload/order/identities on CPU before the first model test."""
import hashlib
import json
from pathlib import Path
import random
import shutil
import subprocess
import sys
import time
from run_stage import ROOT, reuse, validate

BASE = ROOT / '.q4t-work/suffix-agent-20261027'
OLD = ROOT / '.q4t-work/suffix-causal-20261026/acceptance-b/frozen-plan.json'


def sha(p):
    with p.open('rb') as f: return hashlib.file_digest(f, 'sha256').hexdigest()


def save(p, value):
    with p.open('x') as f:
        json.dump(value, f, ensure_ascii=False, indent=2); f.write('\n')


def main():
    from transformers import AutoTokenizer
    old = json.loads(OLD.read_text())
    build = json.loads((BASE / 'build-3-result.json').read_text())
    source = json.loads((BASE / 'build-3-source.json').read_text())
    assert build['passed'] and json.loads((BASE / 'cpu-final-result.json').read_text())['passed']
    for p, h in source['files'].items():
        if sha(Path(p)) != h: raise RuntimeError('source changed: ' + p)
    stage = BASE / 'acceptance'; stage.mkdir()
    fixtures = stage / 'fixtures'; fixtures.mkdir()
    tokenizer = AutoTokenizer.from_pretrained(old['model'], local_files_only=True)
    orig = {c['id']: c for c in old['performance']}
    text = lambda key: json.loads(Path(orig[key]['path']).read_text().splitlines()[0])['prompt']
    backgrounds = text('context-204800').splitlines()[1:-2]
    rng = random.Random(202610270431)
    rng.shuffle(backgrounds)
    def padded(kind, target, slot):
        body = text(kind).replace('at least twenty times', 'at least one hundred times')
        marker = '<|im_start|>user\n'
        if marker in body:
            prefix, suffix = body.split(marker, 1)
            prefix += marker
        else:
            prefix, suffix = '', body
        prefix += f'Agent workspace {slot}: archived background notes follow. Only perform the final instruction.\n<archive>\n'
        suffix = '\n</archive>\nFinal instruction:\n' + suffix
        lo, hi = 0, len(backgrounds)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            count = len(tokenizer.encode(prefix + '\n'.join(backgrounds[:mid]) + suffix, add_special_tokens=False))
            if count <= target: lo = mid
            else: hi = mid - 1
        prompt = prefix + '\n'.join(backgrounds[:lo]) + suffix
        tokens = len(tokenizer.encode(prompt, add_special_tokens=False))
        if not target - 128 <= tokens <= target: raise RuntimeError('complete-record length')
        return prompt, dict(sha256=hashlib.sha256(prompt.encode()).hexdigest(), tokens=tokens,
                           target=target, role='main' if slot == 0 else 'subagent')
    groups = []
    for name, kinds, output in [
        ('repeat-code', ['repeat-code'] * 4, 2048),
        ('repeat-text', ['repeat-text'] * 4, 2048),
        ('mixed', ['repeat-code','context-8192','repeat-text','context-8192'],256),
        ('nonrepeat', ['context-8192'] * 4, 256)]:
        prompts, manifest = [], []
        for slot, (kind, target) in enumerate(zip(kinds, [131072,32768,49152,65536-output], strict=True)):
            prompt, row = padded(kind, target, slot); prompts.append(prompt); manifest.append(row)
        path = fixtures / (name + '.jsonl')
        path.write_text(''.join(json.dumps({'prompt':p},ensure_ascii=False)+'\n' for p in prompts))
        groups.append(dict(id=name, path=str(path), count=4, parallel=4, tokens=output,
            min=min(r['tokens'] for r in manifest), max=max(r['tokens'] for r in manifest), prompts=manifest))
    qdir = Path(old['quality_fixtures']); quality = {r['prompt_sha256']:r for r in json.loads((qdir/'manifest.json').read_text())}
    shutil.copy2(qdir/'requests.jsonl', fixtures/'quality.jsonl')
    qcase = dict(id='quality', path=str(fixtures/'quality.jsonl'), count=11, parallel=1,
                 tokens=32, min=1, max=204800)
    queues = []
    for n in [16,32]:
        p = fixtures / f'queue-{n}.jsonl'
        p.write_text((json.dumps({'prompt':text('repeat-code')})+'\n') * n)
        queues.append(dict(id=f'queue-{n}',path=str(p),count=n,parallel=n,tokens=32,
                           min=orig['repeat-code']['input_tokens'],max=orig['repeat-code']['input_tokens']))
    tail = dict(id='output-tail',path=orig['repeat-code']['path'],count=1,parallel=1,
                tokens=3,min=orig['repeat-code']['input_tokens'],max=orig['repeat-code']['input_tokens'])
    singles = [dict(id=c['id'],path=c['path'],count=3,parallel=1,tokens=256,
                    min=c['input_min'],max=c['input_max']) for c in old['performance']]
    saved = stage/'programs'; saved.mkdir()
    for n in ['q4t','q4t_tests','q4t_host_tests']:
        shutil.copy2(build[n]['path'], saved/n)
    binary, tests = str(saved/'q4t'), str(saved/'q4t_tests')
    plan = dict(stage='AGENT',output=str(stage/'run'),lease=str(stage/'lease'),
        candidate=binary,candidate_sha256=sha(saved/'q4t'),parent=str(ROOT/'build/q4t'),
        parent_sha256=sha(ROOT/'build/q4t'),model=old['model'],artifact=old['artifact'],
        model_file_metadata=old['model_file_metadata'],source_commit=source['commit'],
        maximum_http_requests=323,maximum_starts=18,maximum_gpu_contracts=4,
        http_requests=320,http_loads=15,stage_timeout_seconds=28800,
        task_deadline_unix=time.time()+28800,direct_contract_timeout_seconds=1200,
        quality=quality,quality_fixtures=str(qdir),port=8762,development=old['development'],
        telemetry_sources=reuse.telemetry.discover_sources(),resource_sample_interval_seconds=1,
        resource_sample_max_age_seconds=5,warmups=0,retries=0,
        cache='natural inherited; no flush/power/affinity changes',
        design=str(ROOT/'docs/MTP_SUFFIX_AGENT_PLAN_2026-10-27.md'),
        material='new long controlled variants of known acceptance material; not novel task-quality holdout; development lifecycle is separate',
        source_evidence_reuse='unchanged suffix index/numeric/global ID/subset format and repaired monitor contracts; old timings are anchors only',
        order='fixed JSONL order + evalscope seed20260920; actual launch order retained in database',
        steps=[],files={})
    if plan['parent_sha256'] != '90f24f49ea93e7ddbb359e817f05a9d39f87a54f66433c01a8d46f198f489bd4':
        raise RuntimeError('default changed')
    def service(name, head, on, cases, layout='agent', program=binary, block=0, lifecycle=False, gate=False):
        plan['steps'].append(dict(name=name,kind='block',head=head,suffix=on,cases=cases,
            layout=layout,binary=program,block=block,lifecycle=lifecycle,conditional_gate=gate,
            http_loads=1,http_requests=sum(c['count'] for c in cases)+3*lifecycle,timeout=3000,
            command=[sys.executable,'-B',str(ROOT/'tools/mtp_suffix_agent/run_stage.py'),
                     '--plan',str(stage/'frozen-plan.json'),'--custom-step',name]))
    service('00-first-http','full',True,[qcase])
    for name, filter_, env in [('01-attention-layout','agent_attention_layout',{}),
        ('02-full-states','mtp_suffix_agent_states',{}),
        ('03-subset-states','mtp_suffix_agent_states',{'Q4T_TEST_SUBSET_HEAD':plan['artifact']})]:
        plan['steps'].append(dict(name=name,kind='contract',http_loads=0,http_requests=0,
            timeout=400,command=[tests,filter_],env=env))
    service('04-full-off','full',False,groups,block=1)
    service('05-full-on','full',True,groups+[queues[0],tail],block=1,lifecycle=True)
    service('06-subset-on','subset',True,[qcase]+groups+[queues[1],tail],block=1,lifecycle=True)
    service('07-subset-off','subset',False,groups,block=1)
    for n,(head,on) in enumerate([('full',True),('full',False),('subset',False),('subset',True)],8):
        service(f'{n:02d}-{head}-'+('on' if on else 'off'),head,on,groups,block=2,gate=n==8)
    for n,(head,new) in enumerate([('full',False),('full',True),('subset',True),('subset',False),('plain',False),('plain',True)],12):
        service(f'{n:02d}-{head}-'+('candidate' if new else 'parent'),head,head!='plain',
                singles if head!='plain' else singles[:5],layout='single',
                program=binary if new else plan['parent'])
    validate(plan)
    assert sum(s['http_loads'] for s in plan['steps']) == 15
    paths = {Path(p) for p in source['files']}
    paths.update([OLD,ROOT/'AGENTS.md',ROOT/'docs/EVALUATION.md',ROOT/'docs/PERFORMANCE_DECISION_PROTOCOL.md',
        Path(plan['design']),ROOT/'build/q4t',ROOT/'build/q4t.release.json',*saved.iterdir(),*fixtures.iterdir(),
        qdir/'manifest.json',qdir/'requests.jsonl'])
    paths.update(BASE.glob('build-*-*.json'));paths.update(BASE.glob('build-*.log'));paths.update(BASE.glob('cpu-*.json'));paths.update(BASE.glob('cpu-*.log'))
    for p in ROOT.joinpath('tools').rglob('*.py'):
        if '.venv' not in p.parts and any(x.startswith('mtp_suffix') or x=='evalscope' for x in p.parts):paths.add(p)
    for c in old['performance']+old['development']:paths.add(Path(c['path']))
    paths.update(Path(plan['artifact']).iterdir())
    for p in paths:
        if p.is_file():plan['files'][str(p.resolve())]=sha(p)
    save(stage/'frozen-plan.json',plan)
    print(json.dumps(dict(plan=str(stage/'frozen-plan.json'),sha256=sha(stage/'frozen-plan.json'),
          binary_sha256=plan['candidate_sha256'],requests=320,starts=15,deadline=plan['task_deadline_unix'])))

if __name__=='__main__':main()
