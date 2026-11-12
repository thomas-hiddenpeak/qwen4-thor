"""One failed layer's real operands: FP64 dot bounds and exact BF16 stages.

Diagnostic only: leaves the original full-chain gate and its thresholds intact.
No statistical/performance acceptance or automatic deployment decision.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.native_embedding.acceptance import save, sha

LIMITS = dict(seconds=180, gpu_model_starts=1, native_forwards=1,
              full_reference_model_starts=0, http_requests=0)


def prepare(args):
    original = json.loads((args.original / 'plan.json').read_text())
    case = dict(original['cases'][3], trace_operand_layer=27)
    assert len(case['ids']) == 129
    sources = {str((ROOT / p).absolute()): sha(ROOT / p) for p in [
        'tools/native_embedding/operand_review.py',
        'tools/native_embedding/model_replay.cpp', 'src/model/embedding_model.cu',
        'include/q4t/model/embedding_model.h', 'src/model/embedding_layout.cpp',
        'src/server/embedding_assets.cpp', 'CMakeLists.txt']}
    expected = {p.name: sha(p) for p in (args.original / 'native/case-3').glob('*.bf16')}
    assert len(expected) == 30
    args.output.mkdir(parents=True, exist_ok=False)
    save(args.output / 'plan.json', dict(limits=LIMITS, cases=[case],
        assets=str(args.assets.absolute()), binary=str(args.binary.absolute()),
        binary_sha256=sha(args.binary), sources=sources,
        original_native_outputs=expected,
        default={str(ROOT / p): sha(ROOT / p) for p in
                 ['build/q4t', 'build/q4t.release.json']}))


def run(args):
    lease = int(os.environ['Q4T_MANAGED_LEASE_FD'])
    os.fstat(lease)
    plan = json.loads((args.output / 'plan.json').read_text())
    assert plan['limits'] == LIMITS
    for path, expected in plan['sources'].items():
        assert sha(path) == expected, path
    assert sha(plan['binary']) == plan['binary_sha256']
    started = time.monotonic()
    result = dict(status='FAILED', diagnostic_only=True, commands=[])
    commands = [
        ('native', [plan['binary'], plan['assets'], str(args.output / 'plan.json'),
                    str(args.output / 'native')]),
        ('analysis', [str(ROOT / '.q4t-work/refenv/bin/python'), __file__,
                      'analyze', '--output', str(args.output)])]
    try:
        for name, cmd in commands:
            with (args.output / (name + '.stdout')).open('x') as out, \
                 (args.output / (name + '.stderr')).open('x') as err:
                before = time.monotonic()
                run = subprocess.run(cmd, cwd=ROOT, pass_fds=(lease,),
                    stdout=out, stderr=err, timeout=LIMITS['seconds']-(before-started),
                    env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1',
                             OPENBLAS_NUM_THREADS='8', OMP_NUM_THREADS='8'))
            result['commands'].append(dict(name=name, returncode=run.returncode,
                                           seconds=time.monotonic()-before))
            assert run.returncode == 0, name
            if name == 'native':
                result['instrumentation_bit_identical'] = all(
                    sha(args.output / 'native/case-0' / n) == h
                    for n, h in plan['original_native_outputs'].items())
                assert result['instrumentation_bit_identical']
        result['status'] = 'CAPTURED'
    finally:
        result['seconds'] = time.monotonic()-started
        result['default_unchanged'] = all(sha(p) == h for p,h in plan['default'].items())
        save(args.output / 'result.json', result)
        print(json.dumps(result), flush=True)
        assert result['default_unchanged']


def analyze(args):
    os.fstat(int(os.environ['Q4T_MANAGED_LEASE_FD']))
    import numpy as np
    import torch
    torch.set_num_threads(8)
    torch.set_num_interop_threads(1)
    plan = json.loads((args.output / 'plan.json').read_text())
    path = Path(plan['assets']) / 'model.safetensors'
    assert sha(path) == '0437e45c94563b09e13cb7a64478fc406947a93cb34a7e05870fc8dcd48e23fd'
    payload = np.memmap(path, mode='r', dtype='u1')
    header_bytes = int.from_bytes(payload[:8], 'little')
    header = json.loads(bytes(payload[8:8+header_bytes]))
    base = args.output / 'native/case-0'
    root = base / 'operands'
    t, p = 129, 144
    report = []
    def floats(value):
        return (value.astype('<u4') << 16).view('<f4')
    def raw(name):
        return np.fromfile(root / (name + '.bf16'), dtype='<u2')
    def read(name, *shape):
        value = floats(raw(name))
        return value.reshape(shape) if shape else value
    def weight(name):
        meta = header['layers.27.' + name + '.weight']
        a, b = meta['data_offsets']
        return floats(payload[8+header_bytes+a:8+header_bytes+b].view('<u2')).reshape(meta['shape'])
    def bf(value):
        return torch.from_numpy(np.asarray(value, dtype='float32')).bfloat16().float().numpy()
    def rounding_violations(exact, actual, bound):
        value = torch.from_numpy(np.asarray(actual, dtype='float32')).bfloat16()
        bits = value.view(torch.uint16).numpy()
        with np.errstate(invalid='ignore'):
            a = floats((bits - 1).astype('uint16')).astype('float64')
            b = floats((bits + 1).astype('uint16')).astype('float64')
        low, high = (np.minimum(a, b) + actual) / 2, (np.maximum(a, b) + actual) / 2
        low = np.where(actual == 0, -(2.0 ** -134), low)
        high = np.where(actual == 0, 2.0 ** -134, high)
        assert np.isfinite(exact).all() and np.isfinite(bound).all()
        assert np.isfinite(low).all() and np.isfinite(high).all()
        return int(((exact < low-bound) | (exact > high+bound)).sum())
    def dot(name, x, w, actual):
        x, w = x.astype('float64'), w.astype('float64')
        exact = x @ w.T
        n = x.shape[-1]
        gamma = n * 2**-24 / (1 - n * 2**-24)
        bound = gamma * (abs(x) @ abs(w).T)
        report.append(dict(name=name, category='fp32_dot_bf16_rounding',
            values=actual.size, violations=rounding_violations(exact, actual, bound),
            max_absolute_error=float(abs(exact-actual).max()),
            max_fp32_error_bound=float(bound.max())))
    def exact(name, actual, expected):
        assert actual.shape == expected.shape
        report.append(dict(name=name, category='same_operand_bf16', values=actual.size,
            differences=int((actual != expected).sum()),
            max_absolute_error=float(abs(actual.astype('float64')-expected).max())))
    def norm(x, w):
        tensor = torch.from_numpy(x)
        value = (tensor * torch.rsqrt(tensor.pow(2).mean(-1, keepdim=True)+1e-6)).bfloat16()
        return (value * torch.from_numpy(w).bfloat16()).float().numpy()
    original = floats(np.fromfile(base / 'layer-27.bf16', dtype='<u2')).reshape(t, 1024)
    normalized = read('input_norm', t, 1024)
    exact('input_norm', normalized, norm(original, weight('input_layernorm')))
    for name, width in [('q', 2048), ('k', 1024), ('v', 1024)]:
        dot(name, normalized, weight('self_attn.'+name+'_proj'), read(name, t, width))
    rope = read('rope', t, 128)
    for name, heads in [('q', 16), ('k', 8)]:
        x = read(name, t, heads, 128)
        x = norm(x, weight('self_attn.'+name+'_norm'))
        rotated = np.concatenate([-x[..., 64:], x[..., :64]], axis=-1)
        cosine = np.concatenate([rope[:, :64], rope[:, :64]], axis=-1)[:, None, :]
        sine = np.concatenate([rope[:, 64:], rope[:, 64:]], axis=-1)[:, None, :]
        result = bf(bf(x * cosine) + bf(rotated * sine)).transpose(1, 0, 2)
        exact('norm_rope_'+name, read('head_'+name, heads, p, 128)[:, :t], result)
    inv = 1.0 / (1000000.0 ** (torch.arange(0, 128, 2).float() / 128))
    angle = torch.arange(t).float()[:, None] * inv
    expected_rope = torch.cat([angle.cos(), angle.sin()], -1).bfloat16().float().numpy()
    exact('rope_table', rope, expected_rope)
    values = read('v', t, 8, 128).transpose(1, 2, 0)
    transposed = read('transposed_v', 8, 128, p)
    expected = np.zeros_like(transposed); expected[:, :, :t] = values
    exact('transpose_values', transposed, expected)
    q, k = read('head_q', 16, p, 128), read('head_k', 8, p, 128)
    merged = np.empty((t, 16, 128), dtype='float32')
    for h in range(16):
        qk = read('qk_'+str(h), t, p)
        dot('qk_'+str(h), q[h, :t], k[h//2], qk)
        probabilities = read('probabilities_'+str(h), t, p)
        score = bf(qk * np.float32(128 ** -0.5)).astype('float64')
        score[np.arange(p)[None, :] > np.arange(t)[:, None]] = -np.inf
        exp = np.exp(score - score.max(-1, keepdims=True))
        expected = exp / exp.sum(-1, keepdims=True)
        # FP32 reduction plus conservative exp/div rounding allowance; BF16
        # output is checked against its actual rounding cell, not a global %.
        gamma = (p + 16) * 2**-24 / (1 - (p + 16) * 2**-24)
        violations = rounding_violations(expected, probabilities, expected * gamma)
        report.append(dict(name='softmax_'+str(h), category='fp32_softmax_bf16',
                           values=probabilities.size, violations=violations))
        output = read('pv_'+str(h), t, 128)
        dot('pv_'+str(h), probabilities, transposed[h//2], output)
        merged[:, h, :] = output
    attention = read('attention', t, 2048)
    exact('merge_heads', attention, merged.reshape(t, 2048))
    projected = read('attention_projected', t, 1024)
    dot('attention_projection', attention, weight('self_attn.o_proj'), projected)
    residual = read('attention_residual', t, 1024)
    exact('attention_residual', residual, bf(original + projected))
    normalized = read('post_norm', t, 1024)
    exact('post_norm', normalized, norm(residual, weight('post_attention_layernorm')))
    gate, up = read('gate_raw', t, 3072), read('up', t, 3072)
    for name, actual in [('gate', gate), ('up', up)]:
        dot(name+'_projection', normalized, weight('mlp.'+name+'_proj'), actual)
    activated = torch.nn.functional.silu(torch.from_numpy(gate).bfloat16())
    expected = (activated * torch.from_numpy(up).bfloat16()).float().numpy()
    gate = read('gate', t, 3072)
    exact('swiglu', gate, expected)
    projected = read('projected', t, 1024)
    dot('down_projection', gate, weight('mlp.down_proj'), projected)
    output = floats(np.fromfile(base / 'layer-28.bf16', dtype='<u2')).reshape(t, 1024)
    exact('final_residual', output, bf(residual + projected))
    save(args.output / 'operators.json', dict(diagnostic_only=True, operators=report,
        all_checks_pass=all(row.get('violations', row.get('differences', -1)) == 0
                            for row in report),
        original_gate_accepted=False, same_input_final_layer_target=True))
    for item in report:
        print(json.dumps(item), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['prepare', 'run', 'analyze'])
    for field in ['original', 'assets', 'binary']:
        parser.add_argument('--'+field, type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    globals()[args.operation](args)


if __name__ == '__main__':
    main()
