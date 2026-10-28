"""Material/identity freeze only; no inference or validation execution."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

from run_stage import ROOT, reuse, save
BASE=ROOT/'.q4t-work/prefix-cache-20261028'
def sha(path):
    with Path(path).open('rb') as f: return hashlib.file_digest(f,'sha256').hexdigest()


def main():
    from transformers import AutoTokenizer
    frozen=BASE/'frozen';frozen.mkdir()
    programs=frozen/'programs';programs.mkdir()
    for name in ['q4t','q4t_prefix_storage_contract','q4t_prefix_state_contract']:
        shutil.copy2(BASE/'build'/name,programs/name)
    shutil.copy2(ROOT/'build/q4t',programs/'parent')
    shutil.copy2(ROOT/'build/q4t.release.json',programs/'parent.release.json')
    old=json.loads((ROOT/'.q4t-work/session-cache-20261027/phase-d/plan.json').read_text())
    tokenizer=AutoTokenizer.from_pretrained(old['model'],local_files_only=True)
    count=lambda x:len(tokenizer.encode(x,add_special_tokens=False))
    quality=frozen/'quality.jsonl';shutil.copy2(old['quality_case']['path'],quality)
    qcase=dict(old['quality_case'],path=str(quality))
    short=min([json.loads(x)['prompt'] for x in quality.read_text().splitlines()],key=len)
    records=''.join(f'Archived record {i}: the agent waits for tools before continuing its task.\n' for i in range(1000))
    front='<|im_start|>user\n<archive>\nAuthoritative shared record: audit_code=831907.\n'
    end='\n</archive>\nOutput def greet(name): return "Hello, " + name repeatedly until asked to stop.<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n'
    lines=records.splitlines(True)
    lo,hi=0,len(lines)
    while lo<hi:
        mid=(lo+hi)//2
        if count(front+''.join(lines[:mid])+end)<9216: lo=mid+1
        else: hi=mid
    development=frozen/'development.txt';development.write_text(front+''.join(lines[:lo])+end)
    fixtures=frozen/'fixtures';fixtures.mkdir()
    scenarios=[]
    for prefix in [8192,32768]:
        for kind in ['repeat-code','repeat-text']:
            source=ROOT/'.q4t-work/suffix-agent-20261027/acceptance/fixtures'/(kind+'.jsonl')
            template=json.loads(source.read_text().splitlines()[0])['prompt']
            instruction=template.split('</archive>\n',1)[1]
            common='<|im_start|>user\n<archive>\nShared system policy: preserve the supplied code and records.\n'
            common+='Tool schema: read_file(path), write_file(path, content), run_command(command).\n'
            for i in range(4000):
                common+=f'Common history {i}: inspect the repository, retain exact text, and wait for tool output.\n'
                if count(common)>prefix+64: break
            scenario=dict(id=f'{kind}-{prefix}',prefix=prefix,cases=[])
            for slot,target in enumerate([131072,32768+512,49152,60000]):
                prefix_text=common+f'Agent branch {slot}: private history begins here.\n'
                private=[f'Private record {j} for agent {slot}: completed the review and recorded its result.\n' for j in range(9000)]
                suffix='\n</archive>\n'+instruction
                lo,hi=0,len(private)
                while lo<hi:
                    mid=(lo+hi+1)//2
                    if count(prefix_text+''.join(private[:mid])+suffix)<=target:lo=mid
                    else:hi=mid-1
                prompt=prefix_text+''.join(private[:lo])+suffix
                if count(prompt)+256>(208896 if slot==0 else 65536):raise ValueError('slot budget')
                path=fixtures/(scenario['id']+f'-{slot}.jsonl')
                path.write_text(json.dumps({'prompt':prompt},ensure_ascii=False)+'\n')
                scenario['cases'].append(dict(path=str(path),input_tokens=count(prompt),tokens=256,
                    prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest()))
            # Freeze actual token LCP; a common string alone is insufficient at
            # the concatenation boundary because tokenization can change it.
            token_rows=[tokenizer.encode(json.loads(Path(c['path']).read_text())['prompt'],add_special_tokens=False) for c in scenario['cases']]
            length=0
            for group in zip(*token_rows):
                if len(set(group))!=1:break
                length+=1
            if not prefix<=length<(32768 if prefix==8192 else 65536):raise ValueError('LCP scope')
            scenario['actual_common_tokens']=length
            scenarios.append(scenario)
    protection=[c for c in old['protection'] if 'path' in c]
    for c in protection:
        destination=frozen/(c['id']+'.jsonl');shutil.copy2(c['path'],destination);c['path']=str(destination)
    now=time.time()
    common=dict(candidate=str(programs/'q4t'),candidate_sha256=sha(programs/'q4t'),
        parent=str(programs/'parent'),parent_sha256=sha(programs/'parent'),
        storage_contract=str(programs/'q4t_prefix_storage_contract'),
        state_contract=str(programs/'q4t_prefix_state_contract'),model=old['model'],
        artifact=old['artifact'],model_file_metadata=old['model_file_metadata'],quality=old['quality'],
        quality_case=qcase,short_prompt=short,development_prompt=str(development),
        scenarios=scenarios,protection=protection,port=8762,
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        task_deadline_unix=now+21600,maximum_http_requests=200,maximum_http_starts=14,
        maximum_gpu_contracts=4,telemetry_sources=reuse.telemetry.discover_sources(),
        resource_sample_interval_seconds=1,resource_sample_max_age_seconds=10,
        direct_contract_timeout_seconds=1800,required_gates=[],files={})
    files=[p for group in ['src','include'] for p in (ROOT/group).rglob('*') if p.is_file()]
    files += [p for group in ['prefix_cache','session_cache','mtp_suffix_multi','mtp_suffix_lazy',
        'mtp_suffix_zero_hit','mtp_suffix_admission','mtp_suffix','evalscope'] for p in (ROOT/'tools'/group).glob('*.py')]
    files += [ROOT/'CMakeLists.txt',ROOT/'docs/PREFIX_CACHE_PLAN_2026-10-28.md']
    files += [p for p in frozen.rglob('*') if p.is_file()]
    files += [Path(common['artifact'])/n for n in ['head.bf16','token_ids.bin','manifest.json']]
    files += list((ROOT/'tools/prefix_cache').glob('*.cpp'))
    common['files']={str(p):sha(p) for p in files}
    save(frozen/'common.json',common)
    (frozen/'source.diff').write_bytes(subprocess.check_output(['git','diff','HEAD'],cwd=ROOT))
    phases=[('A',3600,[('full',True),('subset',True)],20),
            ('B',1800,[('full',True),('subset',True)],0),
            ('C',9000,[('full',False),('full',True),('subset',True),('subset',False)],16),
            ('D',9000,[('full',False),('full',True),('subset',True),('subset',False),('plain',False),('plain',True)],15)]
    for phase,timeout,arms,requests in phases:
        directory=BASE/('phase-'+phase.lower());directory.mkdir()
        plan=copy.deepcopy(common);path=directory/'plan.json'
        plan.update(stage='PREFIX_'+phase,output=str(directory/'run'),steps=[],
            stage_timeout_seconds=timeout,http_requests=len(arms)*requests,http_loads=0 if phase=='B' else len(arms))
        if phase!='A':plan['required_gates']=[str(BASE/('gate-'+{'B':'a','C':'b','D':'c'}[phase]+'.json'))]
        for i,(head,on) in enumerate(arms):
            name=f'{i:02d}-{head}-'+('on' if on else 'off')
            candidate=phase!='D' or on
            step=dict(name=name,head=head,shared=on and head!='plain',session=head!='plain',
                binary=common['candidate'] if candidate else common['parent'],
                kind='contract' if phase=='B' else 'block',http_requests=requests,http_loads=0 if phase=='B' else 1,
                timeout=900 if phase=='B' else (1800 if phase in ['A','D'] else 2250))
            if phase=='B':
                step['command']=[common['state_contract'],common['model'],str(directory/('cache-'+head))]
                if head=='subset':step['command'].append(common['artifact'])
            else:step['command']=[sys.executable,'-B',str(ROOT/'tools/prefix_cache/run_stage.py'),'--plan',str(path),'--custom-step',name]
            plan['steps'].append(step)
        save(path,plan)
    save(BASE/'identity.json',dict(candidate=common['candidate_sha256'],parent=common['parent_sha256'],
        deadline=common['task_deadline_unix'],runtime_commit=common['source_commit'],files=len(files),
        formal_requests=194,formal_starts=12,direct_contracts=2,remaining_http=6,remaining_starts=2,
        scenarios=[dict(id=s['id'],lcp=s['actual_common_tokens'],inputs=[c['input_tokens'] for c in s['cases']]) for s in scenarios]))
    print((BASE/'identity.json').read_text())


if __name__=='__main__':main()
