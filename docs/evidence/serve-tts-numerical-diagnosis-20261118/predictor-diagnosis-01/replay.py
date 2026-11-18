import fcntl
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.native_tts import model_reference as reference
from tools.native_tts import acceptance as gate

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
started = time.monotonic()
out = Path(__file__).parent
stage = out.parent
plan = json.loads((out / 'plan.json').read_text())
module = reference.import_official(stage / 'source-01/official/qwen_tts/core/models')
assets = stage / 'assets-1b7-01'
assert reference.digest(assets / 'model.safetensors') == (
    '38b1d5971bdbd982b561cccec982669a53b0537c3cf5e9bd4778ed07bb2f5137')
config = module.Qwen3TTSConfig(**json.loads((assets / 'config.json').read_text()))
config.talker_config.code_predictor_config._attn_implementation = 'eager'
torch.set_default_dtype(torch.bfloat16)
predictor = module.Qwen3TTSTalkerCodePredictorModelForConditionalGeneration(
    config.talker_config.code_predictor_config, config.talker_config)
torch.set_default_dtype(torch.float32)
state = load_file(assets / 'model.safetensors', device='cpu')
prefix = 'talker.code_predictor.'
weights = {k.removeprefix(prefix): v for k, v in state.items() if k.startswith(prefix)}
predictor.load_state_dict(weights, strict=True, assign=True)
embedding = state['talker.model.codec_embedding.weight']
del weights, state
predictor.eval()
checks = []
forwards = 0
with torch.inference_mode():
    for index in range(2):
        native = stage / f'component-01/run/native/case-{index}'
        target = out / f'case-{index}'
        target.mkdir(exist_ok=False)
        codes = np.fromfile(native / 'codes.i32', '<i4').reshape(2, 16)
        for frame in range(2):
            data = np.fromfile(native / f'talker-hidden-{frame}-0.bf16', '<u2')
            hidden = torch.tensor(data).view(torch.bfloat16).reshape(1, 1, 2048)
            known = torch.tensor(codes[frame].astype(np.int64)).reshape(1, 16)
            inputs = torch.cat((hidden, torch.nn.functional.embedding(
                known[:, :1], embedding)), 1)
            cache = None
            for group in range(1, 16):
                assert time.monotonic() - started < plan['limits']['seconds']
                if group == 1:
                    result = predictor(inputs_embeds=inputs, use_cache=True,
                                       output_hidden_states=True)
                else:
                    result = predictor(input_ids=known[:, group-1:group],
                        generation_steps=group-1, past_key_values=cache,
                        use_cache=True, output_hidden_states=True)
                cache = result.past_key_values
                forwards += 1
                for name, value in [('predictor-hidden', result.hidden_states[-1]),
                                    ('predictor-logits', result.logits)]:
                    name = f'{name}-{frame}-{group}.bf16'
                    value = value[:, -1:].contiguous()
                    (target / name).write_bytes(value.view(torch.uint16).numpy().tobytes())
                    checks.append(dict(case=index, operand=name,
                        **gate.compare_pair(native / name, target / name,
                            value.numel(), gate.THRESHOLDS['dense'])))
                checks.append(dict(case=index, frame=frame, group=group,
                    check='argmax', native=int(known[0, group]),
                    reference=int(result.logits[0, -1].float().argmax()),
                    passed=int(known[0, group]) == int(result.logits[0, -1].float().argmax())))
assert forwards == 60 and not torch.cuda.is_initialized()
(out / 'result.json').write_text(json.dumps(dict(
    status='DIAGNOSIS_ONLY', seconds=time.monotonic()-started, gpu_model_loads=0,
    cpu_predictor_loads=1, predictor_forwards=forwards, checks=checks,
    failed_checks=sum(not x['passed'] for x in checks),
    source_sha256=gate.digest(Path(__file__))), indent=2)+'\n')
