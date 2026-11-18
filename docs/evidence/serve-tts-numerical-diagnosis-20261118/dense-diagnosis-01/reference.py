import fcntl
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.native_tts import model_reference as reference

lease = int(os.environ['Q4T_MANAGED_LEASE_FD'])
fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
os.environ['CUDA_VISIBLE_DEVICES'] = ''
import numpy as np
import torch
import transformers
from safetensors.torch import load_file
assert transformers.__version__ == '4.57.6' and not torch.cuda.is_initialized()
torch.set_num_threads(8)
torch.set_num_interop_threads(1)
stage = Path(__file__).parent.parent
out = Path(__file__).parent / 'reference'
out.mkdir(exist_ok=False)
module = reference.import_official(stage / 'source-01/official/qwen_tts/core/models')
assets = stage / 'assets-1b7-01'
assert reference.digest(assets / 'model.safetensors') == (
    '38b1d5971bdbd982b561cccec982669a53b0537c3cf5e9bd4778ed07bb2f5137')
config = module.Qwen3TTSConfig(**json.loads((assets / 'config.json').read_text()))
config.talker_config.code_predictor_config._attn_implementation = 'eager'
torch.set_default_dtype(torch.bfloat16)
model = module.Qwen3TTSTalkerCodePredictorModelForConditionalGeneration(
    config.talker_config.code_predictor_config, config.talker_config)
torch.set_default_dtype(torch.float32)
state = load_file(assets / 'model.safetensors', device='cpu')
prefix = 'talker.code_predictor.'
model.load_state_dict({k.removeprefix(prefix): v for k,v in state.items()
                       if k.startswith(prefix)}, strict=True, assign=True)
embedding = state['talker.model.codec_embedding.weight']
del state
model.eval()
native = stage / 'component-01/run/native/case-0'
data = np.fromfile(native / 'talker-hidden-0-0.bf16', '<u2')
hidden = torch.tensor(data).view(torch.bfloat16).reshape(1,1,2048)
code = np.fromfile(native / 'codes.i32','<i4')[0]
first = torch.nn.functional.embedding(torch.tensor([[int(code)]]), embedding)

def save(name, layer, value):
    value = value.detach().to(torch.bfloat16).contiguous()
    (out / f'diag-{name}-{layer}-0.bf16').write_bytes(
        value.view(torch.uint16).numpy().tobytes())

hooks = []
for layer, block in enumerate(model.model.layers):
    def hook(name, pre=False, layer=layer):
        return lambda module, args, result=None: save(name, layer,
            args[0] if pre else result)
    for name, child in [('norm',block.input_layernorm),
                        ('q',block.self_attn.q_proj),('k',block.self_attn.k_proj),
                        ('v',block.self_attn.v_proj),('o',block.self_attn.o_proj),
                        ('post',block.post_attention_layernorm),
                        ('gate',block.mlp.gate_proj),('up',block.mlp.up_proj),
                        ('down',block.mlp.down_proj),('hidden',block)]:
        hooks.append(child.register_forward_hook(hook(name)))
    hooks.append(block.self_attn.o_proj.register_forward_pre_hook(hook('attention',True)))
    hooks.append(block.mlp.down_proj.register_forward_pre_hook(hook('silu-up',True)))
    # Compare each head after the official RoPE operator, using its actual QK norm.
    norms = {}
    def norm_hook(name, layer=layer, norms=norms):
        def callback(child, args, value):
            norms[name] = value.transpose(1,2)
            if len(norms) == 2:
                cos, sin = model.model.rotary_emb(value, torch.arange(2).reshape(1,2))
                q,k = module.apply_rotary_pos_emb(norms['q'],norms['k'],cos,sin)
                for tag,tensor in [('qr',q),('kr',k)]:
                    for head in range(tensor.shape[1]):save(tag+str(head),layer,tensor[:,head])
        return callback
    hooks.append(block.self_attn.q_norm.register_forward_hook(norm_hook('q')))
    hooks.append(block.self_attn.k_norm.register_forward_hook(norm_hook('k')))

with torch.inference_mode():
    inputs = model.small_to_mtp_projection(torch.cat((hidden,first),1))
    (out/'projected.bf16').write_bytes(inputs.contiguous().view(torch.uint16).numpy().tobytes())
    result = model.model(inputs_embeds=inputs, use_cache=True)
    save('final',0,result.last_hidden_state[:,-1:])
assert not torch.cuda.is_initialized()
