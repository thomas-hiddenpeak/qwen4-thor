import json
from pathlib import Path
p=Path(__file__).resolve().parent
rows={v:json.loads((p/f'{v}-instances.json').read_text()) for v in ('A','B')}
name='_ZN3q4t5quant12_GLOBAL__N_110ExpertNameEiiPKcS3_'
expert={v:next(r for r in rows[v] if r['name']==name and r['size']==6040) for v in rows}
a,b=expert['A'],expert['B'];assert a['instructions'][1503].startswith('b\t8f4e8') and b['instructions'][1503]=='ldr\tx1, [x28]'
old=json.loads(Path('.q4t-work/hc-up-thunk-cause-20261016/derivation.json').read_text())
patch=next(x for x in old['variants']['A1']['patches'] if x['thunk_va']==0x8f4e8)
assert patch['site_va']==a['address']+4*1503 and patch['new_word']=='0xf9400381'
name='_ZN3q4t5model14Bf16SmallMGemmEPKtS2_PtiiifP11CUstream_st'
paths={}
for v in ('A','B'):
 r=next(r for r in rows[v] if r['name']==name)
 branch=18 if v=='A' else 16
 target=27 if v=='A' else 25
 end=39 if v=='A' else 36
 assert r['instructions'][branch].startswith('b.hi\t')
 assert int(r['instructions'][branch].split()[1],16)==r['address']+target*4
 assert r['instructions'][end]=='ret'
 indices=list(range(branch+1))+list(range(target,end+1))
 seq=[r['instructions'][i] for i in indices]
 assert all(not s.startswith(('bl\t','blr\t')) for s in seq)
 for m in (1,8192,8181):assert ((m-2)&0xffffffff)>6
 paths[v]={'conditions':'all pointers non-null, M in {1,8192,8181}, valid stack canary; unsigned (M-2)>6',
           'address':hex(r['address']),'instructions_executed':len(indices),
           'path':[{'address':hex(r['address']+4*i),'assembly':r['instructions'][i]} for i in indices],
           'HC_comparison_reached':False,'calls_on_path':0}
result={'new_ExpertName_difference':{'A_address':hex(a['address']),'B_address':hex(b['address']),'mnemonic_difference_index':1503,'old_thunk_intervention_already_covered':patch},
        'off_Bf16SmallMGemm_paths':paths,
        'interpretation':'HC is not called but compiled host early return paths differ: B executes three fewer instructions under these conditions. This is not proof of lower latency, identical cache behavior, or TTFT causality.',
        'next':'operand-aware audit of remaining matched host instances, with address/relocation distinctions; no new GPU test from mnemonic counts alone'}
(p/'focus.json').write_text(json.dumps(result,indent=2)+'\n')
print({v:x['instructions_executed'] for v,x in paths.items()})
