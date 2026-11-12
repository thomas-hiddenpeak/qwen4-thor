"""Replay only failed layer boundaries from immutable actual operand dumps.

The original full-chain gate is never overwritten. This diagnostic separates
inherited input differences from a wrong local layer computation. It performs
no GPU calls, executes at most 16 individual CPU layers, and loads no complete
model. Run under the shared lease as other model-weight validation does.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.native_embedding.acceptance import save, sha

LIMITS = dict(seconds=120, cpu_layer_forwards=16, full_model_starts=0,
              gpu_model_starts=0, http_requests=0)


def prepare(args):
    original = json.loads((args.original / 'comparison.json').read_text())
    frozen = json.loads((args.original / 'plan.json').read_text())
    failed = [row for row in original['numerical'] if not row['accepted']]
    assert len(failed) == 8 and not original['accepted']
    assert all(row['tensor'].startswith('layer-') for row in failed)
    rows, sources = [], {}
    for row in failed:
        number = int(row['tensor'][6:8])
        assert 1 <= number <= 28
        item = dict(case=row['case'], layer=number,
                    original_comparison=row, operands={})
        for backend in ['native', 'reference']:
            item['operands'][backend] = {}
            for key, n in [('input', number - 1), ('output', number)]:
                path = (args.original / backend / f"case-{row['case']}" /
                        f'layer-{n:02d}.bf16').absolute()
                sources[str(path)] = sha(path)
                item['operands'][backend][key] = str(path)
        rows.append(item)
    for name in ['plan.json', 'comparison.json']:
        path = (args.original / name).absolute()
        sources[str(path)] = sha(path)
    source = Path(__file__).absolute()
    sources[str(source)] = sha(source)
    args.output.mkdir(parents=True, exist_ok=False)
    save(args.output / 'plan.json', dict(limits=LIMITS, rows=rows,
        sources=sources, assets=str(args.assets.absolute()),
        weight_sha256='0437e45c94563b09e13cb7a64478fc406947a93cb34a7e05870fc8dcd48e23fd',
        thresholds=frozen['thresholds'], original_gate_accepted=False,
        default={str(ROOT / p): sha(ROOT / p) for p in
                 ['build/q4t', 'build/q4t.release.json']}))


def measures(a, b):
    import numpy as np
    a, b = a.astype('float64'), b.astype('float64')
    assert a.shape == b.shape and np.isfinite(a).all() and np.isfinite(b).all()
    na, nb = np.linalg.norm(a, axis=-1, keepdims=True), np.linalg.norm(b, axis=-1, keepdims=True)
    assert (na > 0).all() and (nb > 0).all()
    return dict(min_cosine=float(((a / na) * (b / nb)).sum(-1).min()),
                max_normalized_absolute_error=float(abs(a / na - b / nb).max()),
                max_relative_l2_error=float((np.linalg.norm(a-b, axis=-1) / nb[..., 0]).max()))


def run(args):
    os.fstat(int(os.environ['Q4T_MANAGED_LEASE_FD']))
    plan = json.loads((args.output / 'plan.json').read_text())
    assert plan['limits'] == LIMITS
    for name, expected in plan['sources'].items():
        assert sha(Path(name)) == expected, name
    assets = Path(plan['assets'])
    assert sha(assets / 'model.safetensors') == plan['weight_sha256']
    import numpy as np
    import torch
    from safetensors import safe_open
    from transformers import Qwen3Config
    import transformers.models.qwen3.modeling_qwen3 as qwen3
    torch.set_num_threads(8)
    torch.set_num_interop_threads(1)
    config = Qwen3Config(**json.loads((assets / 'config.json').read_text()))
    config._attn_implementation = 'eager'
    rotary = qwen3.Qwen3RotaryEmbedding(config).eval()
    started = time.monotonic()
    results = []
    record = dict(status='FAILED', diagnostic_only=True, original_gate_accepted=False,
                  source_sha256=sha(Path(qwen3.__file__)), rows=results,
                  torch=torch.__version__, limits=LIMITS)
    try:
        with safe_open(assets / 'model.safetensors', framework='pt', device='cpu') as weights:
            for row in plan['rows']:
                assert time.monotonic() - started < LIMITS['seconds']
                number = row['layer'] - 1
                with torch.device('meta'):
                    layer = qwen3.Qwen3DecoderLayer(config, number).eval()
                state = {k: weights.get_tensor(f'layers.{number}.{k}')
                         for k in layer.state_dict()}
                layer.load_state_dict(state, strict=True, assign=True)
                result = dict(case=row['case'], layer=row['layer'])
                for backend in ['reference', 'native']:
                    files = row['operands'][backend]
                    x = torch.from_numpy(np.fromfile(files['input'], dtype='<u2').copy())
                    x = x.view(torch.bfloat16).reshape(1, -1, 1024)
                    length = x.shape[1]
                    mask = torch.zeros((1, 1, length, length), dtype=torch.bfloat16)
                    mask.masked_fill_(torch.ones((length, length), dtype=torch.bool).triu(1),
                                      torch.finfo(torch.bfloat16).min)
                    ids = torch.arange(length).unsqueeze(0)
                    with torch.inference_mode():
                        y = layer(x, attention_mask=mask, position_ids=ids,
                                  position_embeddings=rotary(x, ids), use_cache=False)
                    raw = np.fromfile(files['output'], dtype='<u2').reshape(length, 1024)
                    actual = (raw.astype('<u4') << 16).view('<f4')
                    expected = y[0].float().numpy()
                    result[backend] = measures(actual, expected)
                    result[backend]['bit_identical'] = bool(
                        np.array_equal(raw, y[0].contiguous().view(torch.uint16).numpy()))
                t = plan['thresholds']
                v = result['native']
                result['local_accepted'] = (result['reference']['bit_identical'] and
                    v['min_cosine'] >= t['min_cosine'] and
                    v['max_normalized_absolute_error'] <= t['max_normalized_absolute_error'] and
                    v['max_relative_l2_error'] <= t['max_relative_l2_error'])
                results.append(result)
                print(json.dumps(result), flush=True)
                del layer, state
        record['status'] = 'CAPTURED'
        record['all_local_layers_accepted'] = all(r['local_accepted'] for r in results)
    finally:
        record['seconds'] = time.monotonic() - started
        record['default_unchanged'] = all(sha(Path(p)) == h for p, h in plan['default'].items())
        save(args.output / 'result.json', record)
        assert record['default_unchanged']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['prepare', 'run'])
    parser.add_argument('--original', type=Path)
    parser.add_argument('--assets', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    (prepare if args.operation == 'prepare' else run)(args)


if __name__ == '__main__':
    main()
