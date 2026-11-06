"""Independent CPU fixture using the installed, fixed HF vision model.

Do not import the project's NumPy tower: it shared the old rotary defect.
Keeps original synthetic pixels, rounds inputs to production BF16, executes
official FP32 CPU modules with real weights, and records dependency identity.
"""
import argparse
import hashlib
import inspect
import json
import os
from pathlib import Path
import time

import torch
from safetensors import safe_open
import transformers
from transformers.models.qwen4_exp import modeling_qwen4_exp as hf
from transformers.models.qwen4_exp.configuration_qwen4_exp import Qwen4ExpVisionConfig
from transformers import vision_utils


def sha(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def main(args):
    assert os.environ.get('CUDA_VISIBLE_DEVICES') == '', 'CPU reference only'
    torch.set_num_threads(4)
    args.output.mkdir(parents=True, exist_ok=False)
    config = json.loads((args.model/'config.json').read_text())['vision_config']
    index = json.loads((args.model/'model.safetensors.index.json').read_text())
    cfg = Qwen4ExpVisionConfig(**config)
    cfg._attn_implementation = 'eager'
    # No random initialization or unrelated language model allocations.
    with torch.device('meta'):
        model = hf.Qwen4ExpVisionModel(cfg)
    weights = {}
    for shard in sorted(set(v for k,v in index['weight_map'].items()
                            if k.startswith('model.visual.'))):
        with safe_open(str(args.model/shard), framework='pt', device='cpu') as f:
            for key in f.keys():
                if key.startswith('model.visual.'):
                    weights[key.removeprefix('model.visual.')] = f.get_tensor(key).float()
    model.load_state_dict(weights, strict=True, assign=True)
    model.rotary_pos_emb = hf.Qwen4ExpVisionRotaryEmbedding(cfg.hidden_size//cfg.num_heads//2)
    model.eval()
    old = json.loads(args.original.read_text())
    # Existing identical inputs allow reusing preserved CUDA outputs as a
    # defect negative control; rectangular case exercises different h/w.
    generator = torch.Generator().manual_seed(20261106)
    fixtures = {'small': old, 'rectangular': {
        'image': dict(grid_thw=[[1,8,12]], pixel_values=torch.randn(96,1536,generator=generator).tolist()),
        'video': dict(grid_thw=[[2,6,10]], pixel_values=torch.randn(120,1536,generator=generator).tolist())}}
    begin=time.monotonic()
    records=[]
    with torch.inference_mode():
        for group,cases in fixtures.items():
            result={}
            for name,row in cases.items():
                pixels=torch.tensor(row['pixel_values'],dtype=torch.float32).to(torch.bfloat16).float()
                grid=torch.tensor(row['grid_thw'])
                out=model(pixels,grid).pooler_output
                result[name]=dict(grid_thw=row['grid_thw'],pixel_values=pixels.tolist(),
                    output=out.tolist(), output_shape=list(out.shape))
                records.append(dict(group=group,case=name,grid=row['grid_thw'],shape=list(out.shape)))
                print(group,name,list(out.shape),flush=True)
            (args.output/(group+'.json')).write_text(json.dumps(result))
    sources=[Path(inspect.getfile(hf)),Path(inspect.getfile(vision_utils)),Path(__file__),
        args.original,args.model/'config.json',args.model/'model.safetensors.index.json']
    # Quantify the defect without launching another CUDA worker: compare
    # the original phase's raw output against the independent same inputs.
    negative=[]
    import numpy as np
    correct=json.loads((args.output/'small.json').read_text())
    for name,row in correct.items():
        path=Path(str(args.old_output)+'-'+name+'.bf16')
        values=(np.fromfile(path,dtype=np.uint16).astype(np.uint32)<<16).view(np.float32)
        expected=np.asarray(row['output'],dtype=np.float32).ravel()
        delta=values-expected
        negative.append(dict(case=name,max_abs=float(abs(delta).max()),
            rel_l2=float(np.linalg.norm(delta)/np.linalg.norm(expected)),
            passed=bool(abs(delta).max()<.05 and np.linalg.norm(delta)/np.linalg.norm(expected)<.05)))
        sources.append(path)
    (args.output/'manifest.json').write_text(json.dumps(dict(
        implementation='official HF Qwen4ExpVisionModel, eager FP32 CPU',
        transformers=transformers.__version__,torch=torch.__version__,
        sources={str(p):sha(p) for p in sources},records=records,
        old_cuda_against_independent_reference=negative,seconds=time.monotonic()-begin,
        tolerance=dict(max_abs=.05,rel_l2=.05)),indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--model',type=Path,required=True)
    p.add_argument('--original',type=Path,required=True)
    p.add_argument('--old-output',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    main(p.parse_args())
