"""Check captured NormRoPE using the actual FP32 reduction and CUDA rsqrt.

No model initialization or CUDA calls. Only two fixed norm-weight vectors and
already captured operands are read. NVIDIA reports a maximum observed rsqrtf
error of 2 ULP from extensive, nonexhaustive testing, not a universal guarantee.
Enumerate that finite set for each complete captured head.
https://docs.nvidia.com/cuda/cuda-programming-guide/05-appendices/mathematical-functions.html
"""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.native_embedding.acceptance import save, sha


def check(x, weight, rope, actual):
    import numpy as np
    import torch
    def bf(value):
        return torch.from_numpy(np.asarray(value, dtype='float32')).bfloat16().float().numpy()
    # NormRope uses 128 threads and a balanced 7-level FP32 reduction.
    square = np.multiply(x, x, dtype='float32')
    assert np.array_equal(square.astype('float64'), x.astype('float64') ** 2)
    reduction = square.copy()
    width = 64
    while width:
        reduction[..., :width] = np.add(reduction[..., :width],
                                        reduction[..., width:2*width], dtype='float32')
        width //= 2
    variance = np.add(reduction[..., :1] / np.float32(128),
                      np.float32(1e-6), dtype='float32')
    nearest = (1.0 / np.sqrt(variance.astype('float64'))).astype('float32')
    low1 = np.nextafter(nearest, np.float32(-np.inf))
    high1 = np.nextafter(nearest, np.float32(np.inf))
    choices = [np.nextafter(low1, np.float32(-np.inf)), low1, nearest,
               high1, np.nextafter(high1, np.float32(np.inf))]
    cosine = np.concatenate([rope[:, :64], rope[:, :64]], -1)[:, None, :]
    sine = np.concatenate([rope[:, 64:], rope[:, 64:]], -1)[:, None, :]
    allowed = np.zeros(actual.shape[:2], dtype=bool)
    matches = []
    for index, inverse in enumerate(choices):
        normalized = bf(bf(np.multiply(x, inverse, dtype='float32')) * weight)
        rotated = np.concatenate([-normalized[..., 64:], normalized[..., :64]], -1)
        predicted = bf(bf(normalized*cosine) + bf(rotated*sine))
        same_head = (predicted == actual).all(-1)
        allowed |= same_head
        matches.append(dict(rsqrt_offset_ulps=index-2, matching_heads=int(same_head.sum())))
    return dict(heads=int(allowed.size), uncovered_heads=int((~allowed).sum()),
                choices=matches, whole_head_common_rsqrt=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    import numpy as np
    import torch
    torch.set_num_threads(8)
    plan = json.loads((args.capture/'plan.json').read_text())
    path = Path(plan['assets'])/'model.safetensors'
    assert sha(path) == '0437e45c94563b09e13cb7a64478fc406947a93cb34a7e05870fc8dcd48e23fd'
    payload = np.memmap(path, dtype='u1', mode='r')
    size = int.from_bytes(payload[:8], 'little')
    header = json.loads(bytes(payload[8:8+size]))
    root = args.capture/'native/case-0/operands'
    def floats(bits):
        return (bits.astype('<u4') << 16).view('<f4')
    def read(name):
        return floats(np.fromfile(root/(name+'.bf16'), dtype='<u2'))
    rope = read('rope').reshape(129, 128)
    rows = []
    for name, heads in [('q', 16), ('k', 8)]:
        meta = header['layers.27.self_attn.'+name+'_norm.weight']
        a, b = meta['data_offsets']
        weight = floats(payload[8+size+a:8+size+b].view('<u2'))
        x = read(name).reshape(129, heads, 128)
        actual = read('head_'+name).reshape(heads, 144, 128)[:, :129].transpose(1, 0, 2)
        result = check(x, weight, rope, actual)
        wrong = actual.copy(); wrong[0, 0, 0] += np.float32(1)
        negative = check(x, weight, rope, wrong)
        result.update(name=name, negative_control_uncovered=negative['uncovered_heads'])
        rows.append(result)
    record = dict(profile='same_operand_norm_rope_rsqrt_review', rows=rows,
        accepted=all(r['uncovered_heads'] == 0 and r['negative_control_uncovered'] > 0 for r in rows),
        input_hashes={p.name:sha(p) for p in root.glob('*.bf16')},
        capture_plan_sha256=sha(args.capture/'plan.json'), source_sha256=sha(__file__),
        original_gate_accepted=False, final_output_thresholds_changed=False,
        gpu_calls=0, full_model_loads=0,
        authority='https://docs.nvidia.com/cuda/cuda-programming-guide/05-appendices/mathematical-functions.html')
    save(args.output, record)
    print(json.dumps({k:v for k,v in record.items() if k != 'input_hashes'}), flush=True)
    assert record['accepted']


if __name__ == '__main__':
    main()
