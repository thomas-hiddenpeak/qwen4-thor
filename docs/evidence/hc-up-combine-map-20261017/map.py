"""Map persistent CombineWithGate arguments using existing allocation logs."""
import hashlib
import json
from pathlib import Path
root=Path('/home/rm01/models/dev/qwen4-thor')
out=Path(__file__).resolve().parent
source=root/'.q4t-work/hc-up-address-observer-20261017/allocations.json'
alloc=json.loads(source.read_text())
labels=['d_ids','d_positions','d_seq_id','d_token_seq_id','d_ragged_seq_offset',
        'd_ragged_token_local','d_rope_pos','d_emb','d_img_pos','d_trunk',
        'd_trunk2','d_ple_emb']
sizes=[32768,32768,4,32768,8,32768,2506752,41943040,32768,
       167772160,167772160,41943040]
result={}
for v in ['A','B']:
    rows=[x for x in alloc[v] if x['signature'][:2]==['startup','cudaMalloc']]
    starts=[i for i in range(len(rows)-len(sizes))
            if [x['signature'][2] for x in rows[i:i+len(sizes)]]==sizes]
    assert len(starts)==1,starts
    i=starts[0]
    selected=rows[i:i+len(sizes)+1]
    assert selected[-1]['signature'][2]==2348023808
    result[v]={name:{'id':r['id'],'bytes':r['signature'][2],
                     'pointer_hex':hex(r['ptr']), 'pointer':r['ptr'],
                     'mod_2MiB':r['ptr']%(2**21)}
               for name,r in zip(labels+['d_ws'],selected)}
comparison={}
for name in labels+['d_ws']:
    a,b=result['A'][name],result['B'][name]
    comparison[name]={'same_virtual_pointer':a['pointer']==b['pointer'],
                      'B_minus_A_bytes':b['pointer']-a['pointer']}
arguments={
 'block_output':'d_ws + MakeDecoderWorkspaceLayout(T, layer).block; MoE writes d_block',
 'R':'d_ws + layout.combined; attn_frame.WriteAndRead materializes residual',
 'inject_gated':'d_ws + layout.gate; persistent gate_storage passed to next_frame.ReadImpl',
 'out':'zero-based even layer: d_trunk2; odd layer: d_trunk; RunLayers swaps after each layer'}
files=['src/model/model.cu','src/model/decoder_layer.cu','src/model/hyperconnection.cu','tools/evalscope/scratch_layout_probe.cpp']
report={'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
        'source_code_sha256':{p:hashlib.sha256((root/p).read_bytes()).hexdigest() for p in files},
        'mapping_method':'unique complete ordered 12-allocation suffix plus following workspace allocation, matched source LoadModel order; no size-only lookup',
        'allocations':result,'comparison':comparison,'combine_arguments':arguments,
        'old_intervention_coverage':'fixed seven cudaMallocAsync slots: six linear attention scratch arrays plus routing metadata; neither startup d_ws nor either trunk was replaced',
        'evidence_limits':['Addresses belong to prior preload observer epoch, not the clean trace pair','Three input addresses equal at equal T/layer layout in that epoch; out differs only for odd zero-based layers','Equal virtual addresses or 2MiB modulo does not prove equal physical placement/cache state','No proof that 512MiB trunk displacement causes the observed duration differences'],
        'new_gpu_requests':0,'root_cause_proven':False}
(out/'result.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(comparison,indent=2))
