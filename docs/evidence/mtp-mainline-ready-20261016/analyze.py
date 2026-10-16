import hashlib,json,statistics
from pathlib import Path
r=Path(__file__).resolve().parent
prior=r.parent/'mtp-merge-e2e-20261015'
freeze=json.loads((r/'freeze.json').read_text())
lengths=[1024,4096,8192,45056,204800]
result={'binary_sha256':freeze['sha256']['build/q4t'],'tiers':[],'quality':{},'limits':'pending'}
for side in ['off','on']:
    assert (r/side/'binary.sha256').read_text().strip()==result['binary_sha256']
    status=json.loads((r/side/'exit.json').read_text())
    assert status['http_output_checks_passed'] and status['full_five_tier_completed']
    rows=json.loads((r/side/'results.json').read_text())
    assert [x['length'] for x in rows]==lengths
    for row in rows:
        assert row['deterministic'] and len(row['outputs'])==3 and len(set(row['outputs']))==1
for off,on in zip(json.loads((r/'off/results.json').read_text()),json.loads((r/'on/results.json').read_text())):
    n=off['length']; assert n==on['length'] and off['prompt_sha256']==on['prompt_sha256']
    a=statistics.mean(x['decode_tps'] for x in off['metrics']); b=statistics.mean(x['decode_tps'] for x in on['metrics'])
    row={'length':n,'off_decode_tps':a,'on_decode_tps':b,'gain_percent':100*(b/a-1),'target_pass':1.5<=b/a<=3,'off_ttft':statistics.mean(x['ttft'] for x in off['metrics']),'on_ttft':statistics.mean(x['ttft'] for x in on['metrics'])}
    for side in ['off','on']:
        old=(prior/side/f'context-{n}/output-0.txt').read_text()
        new=(r/side/f'context-{n}/output-0.txt').read_text()
        shared=next((i for i,(x,y) in enumerate(zip(old,new)) if x!=y),min(len(old),len(new)))
        row[side+'_old_text_equal']=old==new
        row[side+'_common_prefix_characters']=shared
    result['tiers'].append(row)
for side in ['off','on']:
    path=r/('quality-'+side)/'results.json'
    if path.exists():
        rows=json.loads(path.read_text()); result['quality'][side]={'count':len(rows),'exact_match':sum(x['exact_match'] for x in rows),'length_match':sum(x['length_match'] for x in rows),'failures':[{'id':x['id'],'expected':x['expected'],'text':x['text'],'finish':x['finish']} for x in rows if not x['exact_match']]}
boundary=r/'boundaries/exit.json'
if boundary.exists():result['limits']=json.loads(boundary.read_text())
result['performance_target_pass']=all(x['target_pass'] for x in result['tiers'])
(r/'summary.json').write_text(json.dumps(result,indent=2,ensure_ascii=False)+'\n')
print(json.dumps(result,indent=2,ensure_ascii=False))
