"""CPU float64 head reference with original BF16 intermediate boundaries.

Consumes post-HTTP stage dumps; reports errors, never auto-accepts precision.
Weights are read-only safetensors mappings. No GPU work or performance claim.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import struct
import numpy as np


def bf16_float(bits):
    return (np.asarray(bits, dtype=np.uint32) << 16).view(np.float32)


def rounded(x):
    # FP64 dot -> FP32 accumulator value -> BF16 RNE. Nonlinear functions
    # use float64 here; this is an independent reference, not a CUDA clone.
    x = np.asarray(x, dtype=np.float32)
    bits = x.view(np.uint32)
    out = ((bits + 0x7fff + ((bits >> 16) & 1)) >> 16).astype(np.uint16)
    return bf16_float(out).astype(np.float64)


def metrics(actual, reference):
    delta = actual - reference
    norm = np.linalg.norm(reference)
    top = np.argsort(-reference, kind='stable')[:2]
    return {'l2_rel': float(np.linalg.norm(delta) / max(norm, 1e-300)),
            'max_abs': float(np.abs(delta).max()),
            'argmax': int(np.argmax(actual)), 'reference_argmax': int(top[0]),
            'reference_top_margin': float(reference[top[0]] - reference[top[1]])}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--stages', required=True, type=Path)
    ap.add_argument('--model-dir', required=True, type=Path)
    ap.add_argument('--output', required=True, type=Path)
    args = ap.parse_args()
    root = Path(__file__).resolve().parents[2]
    stage, out = args.stages.resolve(), args.output.resolve()
    assert any(out.is_relative_to(root / p) for p in ['build', '.q4t-work'])
    assert json.loads((stage / 'exit.json').read_text())['numerical'] == 0
    assert 'NUMERICAL_REVIEW_REQUIRED' in (stage / 'result.log').read_text()
    out.mkdir(parents=True, exist_ok=False)
    shutil.copy2(__file__, out / Path(__file__).name)
    index = json.loads((args.model_dir / 'model.safetensors.index.json').read_text())
    weight_metadata = {}

    def weight(name):
        path = args.model_dir / index['weight_map'][name]
        with path.open('rb') as f:
            length = struct.unpack('<Q', f.read(8))[0]
            entry = json.loads(f.read(length))[name]
        assert entry['dtype'] == 'BF16'
        weight_metadata[name] = {'file': str(path), **entry}
        return np.memmap(path, mode='r', dtype='<u2', shape=tuple(entry['shape']),
                         offset=8 + length + entry['data_offsets'][0])

    prefix = 'model.language_model.hyper_connection_mixer.'
    down = bf16_float(weight(prefix + 'input_mix_weight_down.weight')).astype(np.float64)
    up = bf16_float(weight(prefix + 'input_mix_weight_up.weight')).astype(np.float64)
    lm = weight('lm_head.weight')
    assert down.shape == (320, 10240) and up.shape == (10240, 320)
    assert lm.shape == (248320, 2560)
    cases = sorted(p.name.removesuffix('-mixed-all.bf16')
                   for p in stage.glob('*-mixed-all.bf16'))
    assert len(cases) == 10
    inputs, records = [], []
    hashes = {}

    def read(name):
        path = stage / (name + '.bf16')
        hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        return bf16_float(np.fromfile(path, dtype='<u2')).astype(np.float64)

    for case in cases:
        norm = read(case + '-normed-all')
        assert np.array_equal(norm, read(case + '-normed-last'))
        low = rounded(down @ norm)
        z = low / 4
        activated = rounded(z / (1 + np.exp(-z)))
        expanded = rounded(up @ activated)
        mixed_ref = rounded((norm / (1 + np.exp(-expanded))).reshape(4, 2560).mean(0))
        old, new = read(case + '-mixed-all'), read(case + '-mixed-last')
        np.save(out / (case + '-mixed-reference.npy'), mixed_ref)
        records.append({'case': case, 'mixed_all': metrics(old, mixed_ref),
                        'mixed_last': metrics(new, mixed_ref)})
        inputs.extend([old, new, mixed_ref])
    # Each group: old mixed, new mixed, common reference mixed. Tiled read
    # bounds RAM while all cases share one pass over the vocabulary weights.
    x = np.stack(inputs, axis=1)
    exact = np.empty((lm.shape[0], x.shape[1]), dtype=np.float64)
    for start in range(0, lm.shape[0], 2048):
        stop = min(start + 2048, lm.shape[0])
        exact[start:stop] = bf16_float(lm[start:stop]).astype(np.float64) @ x
    for i, record in enumerate(records):
        case = record['case']
        old, new = read(case + '-all'), read(case + '-last')
        common = rounded(exact[:, i * 3 + 2])
        record.update(
            projection_all_own_input=metrics(old, exact[:, i * 3]),
            projection_last_own_input=metrics(new, exact[:, i * 3 + 1]),
            head_all_common_reference=metrics(old, common),
            head_last_common_reference=metrics(new, common))
        np.save(out / (case + '-logits-reference.npy'), common)
        print(json.dumps(record), flush=True)
    (out / 'reference-review.json').write_text(json.dumps(records, indent=2) + '\n')
    (out / 'manifest.json').write_text(json.dumps({
        'stages_manifest': json.loads((stage / 'manifest.json').read_text()),
        'weight_metadata': weight_metadata, 'input_sha256': hashes,
        'reference': 'FP64 matmul/nonlinear; FP32 then BF16 RNE at original boundaries',
        'limits': 'Ten observed rows, not all prompts; CPU reference is not production math. '
                  'Reports relative evidence, no automatic precision acceptance.'}, indent=2) + '\n')


if __name__ == '__main__':
    main()
