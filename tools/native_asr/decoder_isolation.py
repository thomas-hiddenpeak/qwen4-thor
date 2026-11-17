#!/usr/bin/env python3
"""Isolate encoder propagation from decoder arithmetic using frozen captures."""
import argparse
import json
import os
from pathlib import Path
import time

from model_reference import digest, import_official


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('assets', 'official-source', 'plan', 'native', 'reference', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    os.fstat(int(os.environ['Q4T_MANAGED_LEASE_FD']))
    os.environ['CUDA_VISIBLE_DEVICES'] = ''
    import numpy as np
    import torch
    import transformers
    assert transformers.__version__ == '4.57.6'
    torch.set_num_threads(8)
    torch.set_num_interop_threads(1)
    started = time.monotonic()
    module = import_official(args.official_source)
    assert digest(args.assets / 'model.safetensors') == (
        '79d6cbd4c98c7bbffe9db2edac07f56cd6637d0d5944b27f6c2b8353840323ea')
    model, loading = module.Qwen3ASRForConditionalGeneration.from_pretrained(
        args.assets, dtype=torch.bfloat16, attn_implementation='eager',
        local_files_only=True, output_loading_info=True)
    assert not any(loading.get(key) for key in (
        'missing_keys', 'unexpected_keys', 'mismatched_keys', 'error_msgs'))
    model.eval()
    thinker = model.thinker
    plan = json.loads(args.plan.read_text())
    assert len(plan['cases']) == 2
    args.output.mkdir(parents=True, exist_ok=False)
    checks = []
    sources = {}

    def load(path):
        sources[str(path)] = digest(path)
        return torch.from_numpy(np.fromfile(path, '<u2')).view(torch.bfloat16)

    def metric(a, b):
        x, y = a.float().double().flatten().numpy(), b.float().double().flatten().numpy()
        return {'cosine': float(x @ y / (np.linalg.norm(x) * np.linalg.norm(y))),
                'relative_l2': float(np.linalg.norm(x - y) / np.linalg.norm(y)),
                'max_abs': float(np.max(np.abs(x - y))),
                'different_bf16': int((a != b).sum()),
                'argmax_equal': int(a.argmax()) == int(b.argmax())}

    with torch.inference_mode():
        for index, case in enumerate(plan['cases']):
            native = args.native / f'case-{index}'
            reference = args.reference / f'case-{index}'
            history = np.fromfile(native / 'generated.u32', '<u4').tolist()
            sources[str(native / 'generated.u32')] = digest(native / 'generated.u32')
            assert len(history) >= 4
            audio_values = {name: load(root / 'encoder.bf16').reshape(-1, 1024)
                            for name, root in [('native', native), ('reference', reference)]}
            for step in range(4):
                ids = case['tokens'] + history[:step]
                operands = {}
                for name, audio in audio_values.items():
                    assert time.monotonic() - started < 180
                    embedded = thinker.model.embed_tokens(torch.tensor([ids]))
                    start = case['audio_start']
                    embedded[:, start:start + audio.shape[0]] = audio
                    result = thinker.model(inputs_embeds=embedded,
                        attention_mask=torch.ones((1, len(ids)), dtype=torch.long),
                        use_cache=False)
                    logits = thinker.lm_head(result.last_hidden_state[:, -1])[0]
                    assert torch.isfinite(logits).all()
                    logits.contiguous().view(torch.uint16).numpy().tofile(
                        args.output / f'case-{index}-step-{step}-{name}-audio.bf16')
                    operands[name] = logits
                original_reference = load(reference / f'logits-{step}.bf16')
                original_native = load(native / f'logits-{step}.bf16')
                checks.append({'case': index, 'step': step,
                    'reference_reproduces_bitwise': bool(torch.equal(
                        operands['reference'], original_reference)),
                    'decoder_same_audio': metric(original_native, operands['native']),
                    'encoder_propagation_only': metric(operands['native'], operands['reference']),
                    'original_end_to_end': metric(original_native, original_reference)})
    assert not torch.cuda.is_initialized()
    result = {'status': 'DIAGNOSTIC_ONLY_NOT_ACCEPTANCE', 'checks': checks,
              'sources': sources, 'new_gpu_loads': 0, 'cpu_model_loads': 1,
              'cpu_decoder_forwards': 16, 'seconds': time.monotonic() - started,
              'default_changed': False}
    (args.output / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    assert all(row['reference_reproduces_bitwise'] for row in checks)
    print(json.dumps({'status': result['status'], 'seconds': result['seconds'],
                      'reference_reproductions': 8, 'new_gpu_loads': 0}))


if __name__ == '__main__':
    main()
