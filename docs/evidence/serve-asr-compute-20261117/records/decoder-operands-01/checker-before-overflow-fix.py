#!/usr/bin/env python3
"""Check the failed Chinese first step using its actual GPU BF16 operands.

No model construction or GPU calls. FP64 dots plus FP32 accumulation bounds
and BF16 rounding cells distinguish legal rounding from an incorrect operator.
The original end-to-end comparison remains FAILED; this is independent evidence.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.native_embedding.rsqrt_review import check as check_norm_rope
from tools.native_asr.acceptance import digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('assets', 'capture', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    os.fstat(int(os.environ['Q4T_MANAGED_LEASE_FD']))
    os.environ['CUDA_VISIBLE_DEVICES'] = ''
    import numpy as np
    import torch
    torch.set_num_threads(8)
    torch.set_num_interop_threads(1)
    started = time.monotonic()
    args.output.mkdir(parents=True, exist_ok=False)
    plan = json.loads((args.capture / 'plan.json').read_text())
    assert len(plan['cases']) == 1 and plan['cases'][0]['max_new_tokens'] == 1
    case = plan['cases'][0]
    t, p = len(case['tokens']), (len(case['tokens']) + 15) // 16 * 16
    assert t == 70 and p == 80
    base = args.capture / 'run/case-0'
    path = args.assets / 'model.safetensors'
    assert digest(path) == '79d6cbd4c98c7bbffe9db2edac07f56cd6637d0d5944b27f6c2b8353840323ea'
    payload = np.memmap(path, dtype='u1', mode='r')
    header_size = int.from_bytes(payload[:8], 'little')
    header = json.loads(bytes(payload[8:8 + header_size]))
    reports, controls = [], []
    layer = -1

    def floats(bits):
        return (bits.astype('<u4') << 16).view('<f4')

    def bf(values):
        return torch.from_numpy(np.asarray(values, dtype='float32')).bfloat16().float().numpy()

    def weight(name):
        meta = header[name]
        a, b = meta['data_offsets']
        return floats(payload[8 + header_size + a:8 + header_size + b].view('<u2')).reshape(meta['shape'])

    def read(name, *shape):
        value = floats(np.fromfile(base / 'decoder-operands' / f'layer-{layer}' /
                                  (name + '.bf16'), '<u2'))
        return value.reshape(shape) if shape else value

    def add(name, category, values, violations, **extra):
        reports.append(dict(layer=layer, name=name, category=category,
                            values=int(values), violations=int(violations), **extra))

    def exact(name, actual, expected):
        assert actual.shape == expected.shape
        add(name, 'same_operand_exact_bf16', actual.size, (actual != expected).sum())

    def violations(exact_value, actual, bound):
        bits = torch.from_numpy(np.asarray(actual, dtype='float32')).bfloat16().view(torch.uint16).numpy()
        near_a = floats((bits - 1).astype('uint16')).astype('float64')
        near_b = floats((bits + 1).astype('uint16')).astype('float64')
        low, high = (np.minimum(near_a, near_b) + actual) / 2, (np.maximum(near_a, near_b) + actual) / 2
        low = np.where(actual == 0, -(2.0 ** -134), low)
        high = np.where(actual == 0, 2.0 ** -134, high)
        assert np.isfinite(exact_value).all() and np.isfinite(bound).all()
        assert np.isfinite(low).all() and np.isfinite(high).all()
        return int(((exact_value < low - bound) | (exact_value > high + bound)).sum())

    def dot(name, x, w, actual, control=False):
        assert x.shape[-1] == w.shape[-1] and actual.shape == (x.shape[0], w.shape[0])
        x = x.astype('float64')
        n = x.shape[-1]
        gamma = n * 2**-24 / (1 - n * 2**-24)
        gamma += n * 2**-53 / (1 - n * 2**-53)
        failures = 0
        # Bound conversion/scratch independently of the 151936-row LM head.
        for start in range(0, len(w), 4096):
            matrix = w[start:start + 4096].astype('float64')
            expected = x @ matrix.T
            bound = gamma * (abs(x) @ abs(matrix).T)
            failures += violations(expected, actual[:, start:start + 4096], bound)
            if control and start == 0:
                wrong = actual[:, :len(matrix)].copy()
                wrong[0, 0] += np.float32(1)
                controls.append({'name': name, 'rejected': violations(expected, wrong, bound) > 0})
        add(name, 'fp32_dot_bf16_rounding_cell', actual.size, failures)

    def choices(nearest):
        low = np.nextafter(nearest, np.float32(-np.inf))
        high = np.nextafter(nearest, np.float32(np.inf))
        return [np.nextafter(low, np.float32(-np.inf)), low, nearest,
                high, np.nextafter(high, np.float32(np.inf))]

    def norm(name, x, w, actual, control=False):
        assert x.shape == actual.shape and x.shape[-1] == 1024
        square = np.multiply(x, x, dtype='float32')
        sums = np.zeros(x.shape[:-1] + (256,), dtype='float32')
        for start in range(0, 1024, 256):
            sums = np.add(sums, square[..., start:start + 256], dtype='float32')
        width = 128
        while width:
            sums[..., :width] = np.add(sums[..., :width], sums[..., width:width * 2], dtype='float32')
            width //= 2
        variance = np.add(sums[..., :1] / np.float32(1024), np.float32(1e-6), dtype='float32')
        nearest = (1 / np.sqrt(variance.astype('float64'))).astype('float32')
        covered = np.zeros(x.shape[:-1], dtype=bool)
        wrong_covered = np.zeros_like(covered)
        wrong = actual.copy()
        wrong.reshape(-1, 1024)[0, 0] += np.float32(1)
        for inverse in choices(nearest):
            predicted = bf(bf(np.multiply(x, inverse, dtype='float32')) * w)
            covered |= (predicted == actual).all(-1)
            wrong_covered |= (predicted == wrong).all(-1)
        add(name, 'actual_fp32_reduction_common_rsqrt_2ulp', actual.size, (~covered).sum(),
            rows=int(covered.size), bound_is_observed_instance_check=True)
        if control:
            controls.append({'name': name, 'rejected': bool((~wrong_covered).any())})

    def swiglu(gate, up, actual):
        with np.errstate(over='ignore', invalid='ignore'):
            nearest = np.exp(-gate.astype('float64')).astype('float32')
        assert np.isfinite(nearest).all(), 'review overflow separately'
        covered = np.zeros_like(gate, dtype=bool)
        for exponential in choices(nearest):
            activated = bf(np.divide(gate, np.add(np.float32(1), exponential,
                                                  dtype='float32'), dtype='float32'))
            covered |= bf(np.multiply(activated, up, dtype='float32')) == actual
        add('swiglu', 'fp32_exp_2ulp_rounding_instance_check', actual.size, (~covered).sum())

    # Verify the input is exactly the stored token table plus captured audio.
    layer = 0
    table = weight('thinker.model.embed_tokens.weight')
    embedded = table[np.asarray(case['tokens'])].copy()
    del table
    audio = floats(np.fromfile(base / 'encoder.bf16', '<u2')).reshape(-1, 1024)
    start = case['audio_start']
    embedded[start:start + len(audio)] = audio
    exact('complete_embedded_input', read('hidden_input', t, 1024), embedded)
    inverse = 1.0 / (1000000.0 ** (torch.arange(0, 128, 2).float() / 128))
    angles = torch.arange(t).float()[:, None] * inverse
    expected_rope = torch.cat([angles.cos(), angles.sin()], -1).bfloat16().float().numpy()
    for layer in range(28):
        assert time.monotonic() - started < 300
        prefix = f'thinker.model.layers.{layer}.'
        get_weight = lambda name: weight(prefix + name + '.weight')
        hidden = read('hidden_input', t, 1024)
        if layer:
            previous = floats(np.fromfile(base / 'decoder-operands' /
                f'layer-{layer - 1}/hidden_output.bf16', '<u2')).reshape(t, 1024)
            exact('layer_state_continuity', hidden, previous)
        normalized = read('input_norm', t, 1024)
        norm('input_norm', hidden, get_weight('input_layernorm'), normalized, layer == 0)
        for name, width in [('q', 2048), ('k', 1024), ('v', 1024)]:
            dot(name, normalized, get_weight('self_attn.' + name + '_proj'),
                read(name, t, width), layer == 0 and name == 'q')
        rope = read('rope', t, 128)
        exact('rope_table', rope, expected_rope)
        for name, heads in [('q', 16), ('k', 8)]:
            x = read(name, t, heads, 128)
            captured = read('head_' + name, heads, p, 128)
            actual = captured[:, :t].transpose(1, 0, 2)
            result = check_norm_rope(x, get_weight('self_attn.' + name + '_norm'), rope, actual)
            add('norm_rope_' + name, 'actual_fp32_reduction_common_rsqrt_2ulp',
                actual.size, result['uncovered_heads'], heads=result['heads'])
            exact('padding_' + name, captured[:, t:], np.zeros_like(captured[:, t:]))
        vt = read('transposed_v', 8, 128, p)
        expected = np.zeros_like(vt)
        expected[:, :, :t] = read('v', t, 8, 128).transpose(1, 2, 0)
        exact('transpose_values', vt, expected)
        query, key = read('head_q', 16, p, 128), read('head_k', 8, p, 128)
        merged = np.empty((t, 16, 128), dtype='float32')
        for head in range(16):
            qk = read('qk_' + str(head), t, p)
            dot('qk_' + str(head), query[head, :t], key[head // 2], qk)
            probabilities = read('probabilities_' + str(head), t, p)
            scores = bf(qk * np.float32(128 ** -0.5)).astype('float64')
            scores[np.arange(p)[None, :] > np.arange(t)[:, None]] = -np.inf
            exp = np.exp(scores - scores.max(-1, keepdims=True))
            expected = exp / exp.sum(-1, keepdims=True)
            gamma = (p + 16) * 2**-24 / (1 - (p + 16) * 2**-24)
            add('softmax_' + str(head), 'fp32_softmax_bf16_rounding_cell',
                probabilities.size, violations(expected, probabilities, expected * gamma))
            output = read('pv_' + str(head), t, 128)
            dot('pv_' + str(head), probabilities, vt[head // 2], output)
            merged[:, head] = output
        attention = read('attention', t, 2048)
        exact('merge_heads', attention, merged.reshape(t, 2048))
        projected = read('attention_projected', t, 1024)
        dot('attention_projection', attention, get_weight('self_attn.o_proj'), projected)
        residual = read('attention_residual', t, 1024)
        exact('attention_residual', residual, bf(hidden + projected))
        normalized = read('post_norm', t, 1024)
        norm('post_norm', residual, get_weight('post_attention_layernorm'), normalized)
        gate, up = read('gate_raw', t, 3072), read('up', t, 3072)
        for name, actual in [('gate', gate), ('up', up)]:
            dot(name, normalized, get_weight('mlp.' + name + '_proj'), actual)
        swiglu(gate, up, read('gate', t, 3072))
        projected = read('projected', t, 1024)
        dot('down', read('gate', t, 3072), get_weight('mlp.down_proj'), projected)
        exact('final_residual', read('hidden_output', t, 1024), bf(residual + projected))
        print(json.dumps({'layer': layer, 'checks': len(reports),
                          'violations': sum(row['violations'] for row in reports)}), flush=True)
    last = read('hidden_output', t, 1024)[-1:]
    layer = 28
    final = read('final_norm', 1, 1024)
    norm('final_norm', last, weight('thinker.model.norm.weight'), final)
    logits = floats(np.fromfile(base / 'logits-0.bf16', '<u2')).reshape(1, 151936)
    dot('lm_head', final, weight('thinker.lm_head.weight'), logits, True)
    assert not torch.cuda.is_initialized()
    result = {'status': 'OPERAND_REVIEW_ONLY', 'original_gate_accepted': False,
              'thresholds_changed': False, 'checks': reports, 'negative_controls': controls,
              'all_checks_pass': all(row['violations'] == 0 for row in reports) and
                  all(row['rejected'] for row in controls),
              'seconds': time.monotonic() - started, 'gpu_calls': 0, 'full_model_loads': 0,
              'source_sha256': digest(Path(__file__)),
              'norm_rope_checker_sha256': digest(ROOT / 'tools/native_embedding/rsqrt_review.py'),
              'inputs': {str(path.relative_to(base)): digest(path)
                         for path in base.rglob('*.bf16')}}
    (args.output / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({k: v for k, v in result.items()
                      if k not in ('checks', 'inputs', 'negative_controls')}), flush=True)
    return 0 if result['all_checks_pass'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
