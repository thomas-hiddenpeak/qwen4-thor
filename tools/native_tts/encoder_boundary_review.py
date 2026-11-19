"""Reclassify pinned existing encoder evidence, preserving original failures."""
import argparse
import json
import os
from pathlib import Path
import time

import numpy as np

from . import encoder_acceptance as original
from .encoder_boundary import classify_trajectory, require

SCHEMA = 'q4t-encoder-float-boundary-review-v1'
LIMITS = dict(seconds=180, gpu_actions=0, model_loads=0, frames=1,
              groups=16, centroids=2048, dimensions=256)
MODEL_SHA256 = '836b7b357f5ea43e889936a3709af68dfe3751881acefe4ecf0dbd30ba571258'
INTEGRATION_CHANGES = {
    'CMakeLists.txt', 'tools/native_tts/CMakeLists.txt',
    'include/q4t/audio/tts_generation.h', 'include/q4t/audio/tts_layout.h',
    'include/q4t/audio/tts_synthesis.h', 'src/audio/tts_generation.cpp',
    'src/audio/tts_layout.cpp', 'src/audio/tts_model.cu',
    'src/audio/tts_synthesis.cpp'}


def read_json(path):
    return json.loads(Path(path).read_text())


def verify_recorded_files(root, records):
    require(isinstance(records, dict) and bool(records), 'missing artifact map')
    for name, expected in records.items():
        path = Path(name)
        require(not path.is_absolute() and '..' not in path.parts,
                'unsafe evidence path')
        require(original.digest(root / path) == expected,
                'evidence content changed: ' + name)


def verify_lease(phase, status):
    record = read_json(phase / 'lease/exit.json')
    require(record['status'] == status and record['compute_backend'] == 'nvml'
            and record['lease_inherited_by_child'], 'old lease not qualified')
    queries = [json.loads(line) for line in
               (phase / 'lease/nvml-queries.jsonl').read_text().splitlines()]
    events = [json.loads(line) for line in
              (phase / 'lease/events.jsonl').read_text().splitlines()]
    require(bool(queries) and all(q['status'] == 'OK' for q in queries),
            'old lease has a monitor failure')
    require(bool(events) and events[-1]['event'] == 'lease_released' and
            not any(e.get('conflicts') or e.get('unreadable_processes')
                    for e in events), 'old lease conflict or missing cleanup')
    return len(queries)


def load_books(path):
    require(original.digest(path) == MODEL_SHA256, 'codebook asset changed')
    size = path.stat().st_size
    with path.open('rb') as file:
        length = int.from_bytes(file.read(8), 'little')
        require(0 < length <= 1 << 20 and length + 8 < size,
                'invalid tensor header')
        header = json.loads(file.read(length))

    def tensor(name, shape):
        meta = header['encoder.quantizer.' + name]
        begin, end = meta['data_offsets']
        require(meta['dtype'] == 'F32' and meta['shape'] == list(shape) and
                0 <= begin < end <= size - length - 8 and
                end - begin == int(np.prod(shape)) * 4, 'invalid tensor extent')
        array = np.memmap(path, dtype='<f4', mode='r', offset=length + 8 + begin,
                          shape=shape)
        require(bool(np.isfinite(array).all()), 'nonfinite codebook tensor')
        return array

    books = []
    for group in range(16):
        base = ('semantic_residual_vector_quantizer.layers.0' if group == 0 else
                f'acoustic_residual_vector_quantizer.layers.{group - 1}')
        usage = tensor(base + '.codebook.cluster_usage', (2048,))
        require(bool((usage >= 0).all()), 'negative codebook population')
        sums = tensor(base + '.codebook.embed_sum', (2048, 256))
        books.append(np.divide(sums, np.maximum(usage, np.float32(1e-5))[:, None],
                               dtype=np.float32))
    return books


def different_frames(native, reference, frames, captured):
    require(native.dtype == reference.dtype == np.dtype('<i4') and
            native.shape == reference.shape == (frames, 16) and
            bool(((native >= 0) & (native < 2048)).all()) and
            bool(((reference >= 0) & (reference < 2048)).all()),
            'invalid recorded code extent or range')
    rows = np.flatnonzero(np.any(native != reference, axis=1)).tolist()
    require(rows == captured, 'different frame has no complete captured evidence')
    return rows


def review(plan):
    require(plan['schema'] == SCHEMA and plan['limits'] == LIMITS and
            plan['thresholds'] == original.THRESHOLDS and
            plan['owner_authorized_boundary_rule'] is True and
            plan['preserve_original_exact_failure'] is True,
            'boundary rule is not authorized or frozen')
    require(not os.environ.get('PYTHONOPTIMIZE'), 'optimized Python is invalid')
    root = original.ROOT
    for name, expected in plan['sources'].items():
        require(original.digest(root / name) == expected, 'review source changed')
    for name, expected in plan['evidence_inputs'].items():
        require(original.digest(Path(name)) == expected, 'frozen evidence changed')
    require(original.defaults() == plan['defaults'], 'protected defaults changed')
    previous, diagnostic = Path(plan['component']), Path(plan['diagnostic'])
    pplan, dplan = read_json(previous / 'plan.json'), read_json(diagnostic / 'plan.json')
    prior, saved = read_json(previous / 'run/result.json'), read_json(diagnostic / 'run/result.json')
    require(prior['status'] == 'FAILED' and
            prior['error'] == 'corrected encoder comparison failed' and
            prior['defaults_unchanged'] and prior['runtime_sources_unchanged'],
            'expected original failure missing')
    require(saved['status'] == 'DIAGNOSIS_COMPLETED_NOT_QUALIFICATION' and
            saved['defaults_unchanged'] and saved['runtime_sources_unchanged'],
            'incomplete prior diagnosis')
    verify_recorded_files(previous / 'run', prior['artifacts'])
    verify_recorded_files(diagnostic / 'run', saved['artifacts'])
    monitors = verify_lease(previous, 'COMMAND_FAILED') + \
        verify_lease(diagnostic, 'COMPLETED')
    require(Path(dplan['previous_run']) == previous / 'run' and
            dplan['diagnostic_limits']['row'] == plan['captured_rows'][0],
            'different diagnostic input')
    # Freeze the current integration graph without claiming its live API has
    # been tested by these historical numerical operands. Encoder math, weight
    # contracts and common dense/codec kernels must still match the old graph.
    changed = {name for name, digest in dplan['runtime_sources'].items()
               if original.digest(root / name) != digest}
    require(changed == INTEGRATION_CHANGES == set(plan['integration_changes']),
            'unreviewed source/dependency drift')
    require(all(original.digest(root / name) == digest
                for name, digest in plan['current_runtime_sources'].items()),
            'current graph changed after plan freeze')
    comparisons = original.compare(previous / 'run', pplan)
    float_checks = [r for r in comparisons if r['operand'] != 'codes.i32']
    failed = [r for r in comparisons if not r['passed']]
    require(len(float_checks) == 222 and all(r['passed'] for r in float_checks),
            'original floating-point gate failed')
    require(len(failed) == 1 and failed[0]['case'] == 2 and
            failed[0]['operand'] == 'codes.i32', 'unexplained original failure')
    frames = (pplan['cases'][2]['samples'] + 1919) // 1920
    codes = []
    projections, residuals = [], []
    for side in ('native', 'reference'):
        current = diagnostic / 'run' / side / 'case-2'
        old = previous / 'run' / side / 'case-2'
        require(original.digest(current / 'codes.i32') ==
                original.digest(old / 'codes.i32'), 'diagnostic codes changed')
        for file in old.glob('*.f32'):
            require(original.digest(file) == original.digest(current / file.name),
                    'diagnostic does not reproduce original float operands')
        code = np.fromfile(current / 'codes.i32', '<i4')
        require(code.size == frames * 16, 'truncated codes')
        codes.append(code.reshape(frames, 16))
        projections.append(np.stack([np.fromfile(current /
            f'encoder-projection-{group}-2.f32', '<f4') for group in range(2)]))
        residuals.append(np.stack([np.fromfile(current /
            f'encoder-rvq-residual-{group}-2.f32', '<f4') for group in range(16)]))
    rows = different_frames(*codes, frames, plan['captured_rows'])
    require(len(rows) == 1 and rows[0] == 94, 'uncaptured trajectory')
    books = load_books(Path(plan['model']))
    results = classify_trajectory(books, projections, residuals,
                                  [side[rows[0]] for side in codes])
    require(any(r['classification'] == 'BOUNDED_FLOAT_BOUNDARY_CROSSING'
                for r in results), 'no explained boundary crossing')
    require(all(original.digest(Path(name)) == expected
                for name, expected in plan['evidence_inputs'].items()) and
            original.defaults() == plan['defaults'],
            'evidence or protected defaults changed during review')
    return dict(status='PASS_FIXED_ENCODER_FLOAT_BOUNDARY',
                original_exact_code_status='FAIL_PRESERVED',
                original_float_checks=len(float_checks),
                exact_code_cases=sum(r['passed'] for r in comparisons
                                     if r['operand'] == 'codes.i32'),
                different_codes=int(np.count_nonzero(codes[0] != codes[1])),
                captured_rows=rows, checks=results, gpu_actions=0, model_loads=0,
                reused_monitor_queries=monitors,
                original_evidence_unchanged=True, defaults_unchanged=True,
                live_registration_or_business_qualified=False,
                listening='PENDING_OWNER_DEFERRED')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    require(not args.output.exists(), 'do not overwrite a prior review')
    plan = read_json(args.plan)
    start = time.monotonic()
    try:
        result = review(plan)
        require(time.monotonic() - start <= LIMITS['seconds'], 'review timed out')
    except Exception as error:
        result = dict(status='FAIL', error=repr(error), gpu_actions=0, model_loads=0)
    result.update(seconds=time.monotonic() - start,
                  plan_sha256=original.digest(args.plan))
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({key: result[key] for key in ('status', 'seconds', 'gpu_actions')}))
    return 0 if result['status'].startswith('PASS_') else 1


if __name__ == '__main__':
    raise SystemExit(main())
