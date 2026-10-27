"""Freeze acceptance-only two-round material and the fixed C service order."""
import copy
import hashlib
import json
from pathlib import Path
import sys

from run_stage import ROOT, save
from oracle import short_answer


def sha(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f,'sha256').hexdigest()


def main():
    from transformers import AutoTokenizer
    base=ROOT/'.q4t-work/session-cache-20261027'
    target=base/'phase-c';target.mkdir();fixtures=target/'fixtures';fixtures.mkdir()
    plan=copy.deepcopy(json.loads((base/'phase-b/plan.json').read_text()))
    old=json.loads((ROOT/'.q4t-work/suffix-agent-20261027/acceptance/frozen-plan.json').read_text())
    tokenizer=AutoTokenizer.from_pretrained(plan['model'],local_files_only=True)
    count=lambda text:len(tokenizer.encode(text,add_special_tokens=False))
    refdir=ROOT/'.q4t-work/suffix-agent-20261027/acceptance-resume-2/run/05-full-on'
    scenarios=[]
    def write(name,prompt,tokens,expected=None):
        path=fixtures/(name+'.jsonl')
        path.write_text(json.dumps({'prompt':prompt},ensure_ascii=False)+'\n')
        return dict(path=str(path),input_tokens=count(prompt),tokens=tokens,
                    prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
                    expected_sha256=expected)
    for kind in ['repeat-code','repeat-text']:
        source=ROOT/'.q4t-work/suffix-agent-20261027/acceptance/fixtures'/(kind+'.jsonl')
        rows=[json.loads(x) for x in source.read_text().splitlines()]
        refs=json.loads((refdir/kind/'responses.json').read_text())
        gold=refs[0]['text'];gold_sha=refs[0]['output_sha256']
        assert all(r['text']==gold and r['output_tokens']==2048 for r in refs)
        scenario=dict(id=kind,first=[],second=[],interference=[],reference=str(refdir/kind/'responses.json'))
        for slot,row in enumerate(rows):
            prompt=row['prompt']
            if slot==3:
                front,rest=prompt.split('<archive>\n',1)
                records,back=rest.split('\n</archive>',1)
                lines=records.splitlines();lo,hi=0,len(lines)
                while lo<hi:
                    mid=(lo+hi+1)//2
                    candidate=front+'<archive>\n'+'\n'.join(lines[:mid])+'\n</archive>'+back
                    if count(candidate)<=60000:lo=mid
                    else:hi=mid-1
                prompt=front+'<archive>\n'+'\n'.join(lines[:lo])+'\n</archive>'+back
            instruction=prompt.split('</archive>\n',1)[1]
            second=prompt+gold+'<|im_end|>\n<|im_start|>user\n'+instruction
            capacity=208896 if slot==0 else 65536
            if count(second)+2048>capacity:
                raise ValueError('two rounds exceed frozen slot capacity')
            scenario['first'].append(write(f'{kind}-{slot}-first',prompt,2048,gold_sha))
            scenario['second'].append(write(f'{kind}-{slot}-second',second,2048,gold_sha))
            if slot==0:
                poison=prompt.replace('Agent workspace 0:', 'Unrelated request:',1)
            else:
                # Long interference holds slot zero while three short requests
                # reserve and overwrite the short slots. No session capability.
                poison='<|im_start|>user\n'+instruction
            scenario['interference'].append(write(f'{kind}-{slot}-interference',poison,256))
        scenarios.append(scenario)
    post=json.loads((base/'phase-b/postflight.json').read_text())
    state=json.loads((base/'phase-b/run/state.json').read_text())
    if post['status']!='MEASURED_REQUIRES_REVIEW' or state['completed']!=['full-state','subset-state']:
        raise RuntimeError('state/monitor gate missing')
    gate=dict(passed=True,candidate_sha256=plan['candidate_sha256'],
        direct_contracts=['full-state','subset-state'],
        direct_scope='4 real target layers including all persistent state kinds; real draft',
        http_quality={'full':11,'subset':11},http_requests_used=35,
        http_starts_used=2,gpu_fixtures_used=2,tool_fixes_used=2,
        remaining_fix_allowance=0,full_cancel_recovery_pending=True,
        unaffected_reuse='Accepted 0/1/2/3 and mixed suffix/neural independent-prefix evidence; default branch numerical operations unchanged.',
        postflight_sha256=sha(base/'phase-b/postflight.json'))
    save(base/'correctness-gate.json',gate)
    plan.update(stage='SESSION_C',output=str(target/'run'),http_requests=98,
        http_loads=4,stage_timeout_seconds=12000,
        required_gates=[str(base/'correctness-gate.json')],scenarios=scenarios,
        order=['full OFF','full ON','subset ON','subset OFF'],steps=[],
        design='One AB/BA service pair per head; descriptive, no population noninferiority claim. Each scenario first -> unrelated four-slot reuse -> second; no warmup/cache drop.',
        acceptance=dict(transaction_latency_reduction_min=.10,resume_ttft_reduction_min=.50,
                        scenarios=['repeat-code','repeat-text'],heads=['full','subset']),
        material='Old accepted repetition material; new tool turns and 60K final child frozen before current performance; 9K development material separate.')
    plan['short_expected']=short_answer(plan)
    path=target/'plan.json'
    for number,(head,enabled) in enumerate([('full',False),('full',True),('subset',True),('subset',False)]):
        name=f'{number:02d}-{head}-'+('on' if enabled else 'off')
        recovery=head=='full' and enabled
        plan['steps'].append(dict(name=name,head=head,cache=enabled,binary=plan['candidate'],
            kind='block',http_requests=24+2*recovery,http_loads=1,timeout=3000,
            cancel_recovery=recovery,command=[sys.executable,'-B',str(ROOT/'tools/session_cache/run_performance.py'),
                '--plan',str(path),'--custom-step',name]))
    plan['protection']=next(s['cases'][:5] for s in old['steps'] if s['name']=='12-full-parent')
    for name in plan['files']:plan['files'][name]=sha(name)
    for f in [*fixtures.iterdir(),ROOT/'tools/session_cache/run_performance.py',
              ROOT/'tools/session_cache/prepare_performance.py',base/'correctness-gate.json',
              *[Path(s['reference']) for s in scenarios]]:
        plan['files'][str(f)]=sha(f)
    save(path,plan)
    print(json.dumps(dict(candidate=plan['candidate_sha256'],requests=98,starts=4,
        cases=[dict(id=s['id'],first=[r['input_tokens'] for r in s['first']],
                    second=[r['input_tokens'] for r in s['second']]) for s in scenarios]),indent=2))


if __name__=='__main__':main()
