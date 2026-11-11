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
        if case.get('trace_primitives'):
            from transformers.models.qwen3.modeling_qwen3 import apply_rotary_pos_emb
            layer = model.layers[0]
            captured = {}
            length, padded = len(ids), (len(ids) + 15) // 16 * 16
            def capture_rope(module, inputs, kwargs):
                cosine, sine = kwargs['position_embeddings']
                captured['cos'], captured['sin'] = cosine, sine
                save(torch.cat([cosine[..., :64], sine[..., :64]], dim=-1),
                     'rope.bf16')
            hooks.append(layer.self_attn.register_forward_pre_hook(
                capture_rope, with_kwargs=True))
            def capture_value(name):
                def capture(module, inputs, value):
                    captured[name] = value
                    save(value, name + '.bf16')
                return capture
            for name in ['q', 'k', 'v']:
                hooks.append(getattr(layer.self_attn, name + '_proj').register_forward_hook(
                    capture_value(name)))
            for name in ['up', 'gate']:
                hooks.append(getattr(layer.mlp, name + '_proj').register_forward_hook(
                    capture_value(name + '_raw')))
            hooks.append(layer.post_attention_layernorm.register_forward_hook(
                capture_value('post_norm')))
            hooks.append(layer.mlp.down_proj.register_forward_hook(
                capture_value('projected')))
            hooks.append(layer.self_attn.q_norm.register_forward_hook(
                capture_value('q_norm')))
            def capture_attention(module, inputs):
                save(inputs[0], 'attention.bf16')
                save(inputs[0][..., -128:], 'head_output.bf16')
            hooks.append(layer.self_attn.o_proj.register_forward_pre_hook(capture_attention))
            def capture_key_norm(module, inputs, key):
                query = captured['q_norm'].transpose(1, 2)
                key = key.transpose(1, 2)
                query, key = apply_rotary_pos_emb(query, key,
                    captured['cos'], captured['sin'])
                for name, value, heads in [('head_q', query, 16), ('head_k', key, 8)]:
                    out = torch.zeros((heads, padded, 128), dtype=torch.bfloat16)
                    out[:, :length, :] = value[0]
                    save(out, name + '.bf16')
                # Last-head probability is computed by the same eager formula
                # as the reference model, solely to diagnose the failed run.
                scores = query[0, -1] @ key[0, -1].T * (128 ** -0.5)
                causal = torch.ones((length, length), dtype=torch.bool).triu(1)
                scores = scores.masked_fill(causal, torch.finfo(torch.bfloat16).min)
                probabilities = torch.softmax(scores, dim=-1, dtype=torch.float32).bfloat16()
                padded_scores = torch.zeros((length, padded), dtype=torch.bfloat16)
                padded_scores[:, :length] = probabilities
                save(padded_scores, 'scores.bf16')
            hooks.append(layer.self_attn.k_norm.register_forward_hook(capture_key_norm))
        step = time.monotonic()
        try:
            with torch.inference_mode():
                hidden = model(input_ids=torch.tensor([ids], dtype=torch.long),
                               use_cache=False).last_hidden_state
            save(hidden[0, -1], 'last.bf16')
            if case.get('trace_primitives'):
                values = captured['v'].view(length, 8, 128).permute(1, 2, 0)
                transposed = torch.zeros((8, 128, padded), dtype=torch.bfloat16)
                transposed[..., :length] = values
                save(transposed, 'transposed_v.bf16')
                save(captured['up_raw'], 'up.bf16')
                save(torch.nn.functional.silu(captured['gate_raw']) *
                     captured['up_raw'], 'gate.bf16')
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
