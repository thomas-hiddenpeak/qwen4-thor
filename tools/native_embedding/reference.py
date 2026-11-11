"""Pinned-model CPU reference; run model loading inside the shared GPU lease.

Transformers/PyTorch are acceptance tools only, never Serve dependencies.
No downloaded code is executed; use the installed Qwen3Model implementation.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import time


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as source:
        while chunk := source.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--assets', type=Path, required=True)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    lease = int(os.environ['Q4T_MANAGED_LEASE_FD'])
    os.fstat(lease)
    import torch
    import transformers
    from transformers import AutoModel
    assert digest(args.assets / 'model.safetensors') == (
        '0437e45c94563b09e13cb7a64478fc406947a93cb34a7e05870fc8dcd48e23fd')
    plan = json.loads(args.plan.read_text())
    assert 0 < len(plan['cases']) <= 32
    args.output.mkdir(parents=True, exist_ok=False)
    torch.set_num_threads(8)
    torch.set_num_interop_threads(1)
    started = time.monotonic()
    model = AutoModel.from_pretrained(
        args.assets, dtype=torch.bfloat16, attn_implementation='eager',
        local_files_only=True, trust_remote_code=False).eval()
    assert type(model).__name__ == 'Qwen3Model'
    assert model.config.hidden_size == 1024 and len(model.layers) == 28
    results = []
    for index, case in enumerate(plan['cases']):
        ids = case['ids']
        assert 0 < len(ids) <= 8192
        output = args.output / f'case-{index}'
        output.mkdir()
        hooks = []
        def save(value, name):
            assert value.dtype == torch.bfloat16 and torch.isfinite(value).all()
            value.detach().contiguous().view(torch.uint16).numpy().tofile(
                output / name)
        if case.get('trace_layers'):
            hooks.append(model.embed_tokens.register_forward_hook(
                lambda module, inputs, value: save(value, 'layer-00.bf16')))
            for number, layer in enumerate(model.layers, 1):
                def capture(module, inputs, value, n=number):
                    save(value[0] if isinstance(value, tuple) else value,
                         f'layer-{n:02d}.bf16')
                hooks.append(layer.register_forward_hook(capture))
        step = time.monotonic()
        try:
            with torch.inference_mode():
                hidden = model(input_ids=torch.tensor([ids], dtype=torch.long),
                               use_cache=False).last_hidden_state
            save(hidden[0, -1], 'last.bf16')
        finally:
            for hook in hooks:
                hook.remove()
        results.append(dict(index=index, tokens=len(ids),
                            seconds=time.monotonic() - step))
        print(json.dumps(results[-1]), flush=True)
    record = dict(torch=torch.__version__, transformers=transformers.__version__,
                  device='cpu', dtype='bfloat16', attention='eager',
                  seconds=time.monotonic() - started, cases=results,
                  source_sha256=digest(Path(transformers.__file__).parent /
                      'models/qwen3/modeling_qwen3.py'))
    (args.output / 'result.json').write_text(json.dumps(record, indent=2) + '\n')


if __name__ == '__main__':
    main()
