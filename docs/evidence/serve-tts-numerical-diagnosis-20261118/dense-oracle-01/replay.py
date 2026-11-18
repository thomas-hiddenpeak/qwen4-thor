import json
import os
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT))
from tools.native_tts.acceptance import digest
os.fstat(int(os.environ['Q4T_MANAGED_LEASE_FD']))
os.environ['CUDA_VISIBLE_DEVICES']=''
import numpy as np
import torch
from safetensors.torch import load_file
torch.set_num_threads(8)
torch.set_num_interop_threads(1)
assert not torch.cuda.is_initialized()
started=time.monotonic()
out=Path(__file__).parent
stage=out.parent
assets=stage/'assets-1b7-01/model.safetensors'
assert digest(assets)=='38b1d5971bdbd982b561cccec982669a53b0537c3cf5e9bd4778ed07bb2f5137'
state=load_file(assets,device='cpu')
checks=[]
for layer in range(5):
    prefix=f'talker.code_predictor.model.layers.{layer}.'
    native=stage/'dense-diagnosis-02/native'
    ref=stage/'dense-diagnosis-02/reference'
    def read(directory,name):
        data=np.fromfile(directory/f'diag-{name}-{layer}-0.bf16','<u2')
        return torch.tensor(data).view(torch.bfloat16).reshape(2,-1)
    for source,target,weight in [('norm','q','self_attn.q_proj.weight'),
                                ('norm','k','self_attn.k_proj.weight'),
                                ('norm','v','self_attn.v_proj.weight'),
                                ('attention','o','self_attn.o_proj.weight'),
                                ('post','gate','mlp.gate_proj.weight'),
                                ('post','up','mlp.up_proj.weight'),
                                ('silu-up','down','mlp.down_proj.weight')]:
        assert time.monotonic()-started<120
        x=read(native,source).double()
        w=state[prefix+weight].double()
        exact=x@w.T
        rounded=exact.bfloat16().double()
        observed=read(native,target).double()
        # A BF16 dot has exact products in FP32. Its reduction has at most K
        # rounded additions. Compare against the BF16 rounding interval plus
        # gamma_K * sum(abs(products)); cancellation is accounted for.
        epsilon=2**-24
        gamma=x.shape[-1]*epsilon/(1-x.shape[-1]*epsilon)
        accumulation=gamma*(x.abs()@w.abs().T)
        bits=read(native,target)
        low=torch.nextafter(bits,torch.full_like(bits,-float('inf'))).double()
        high=torch.nextafter(bits,torch.full_like(bits,float('inf'))).double()
        lo=(low+observed)/2;hi=(high+observed)/2
        distance=torch.maximum(lo-exact,exact-hi).clamp(min=0)
        violations=distance>accumulation
        wrong=-observed
        negative_violations=((wrong-exact).abs()>((hi-lo).abs()+accumulation)).sum()
        checks.append(dict(layer=layer,operator=target,
            values=observed.numel(),differing_from_exact_bf16=int((rounded!=observed).sum()),
            outside_fp32_bf16_error_bound=int(violations.sum()),
            sign_negative_control_rejected=bool(negative_violations>0),
            maximum_abs_error=float((observed-exact).abs().max()),
            largest_accumulation_bound=float(accumulation.max())))
assert len(checks)==35 and not torch.cuda.is_initialized()
(out/'result.json').write_text(json.dumps(dict(status='DIAGNOSIS_ONLY',
    seconds=time.monotonic()-started,checks=checks,model_constructions=0,
    cuda_initialized=False,source_sha256=digest(Path(__file__))),indent=2)+'\n')
