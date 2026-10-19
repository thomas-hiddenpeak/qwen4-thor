#!/usr/bin/env python3
"""Freeze full model provenance, capture real tokens, and report raw differences.

Run the complete driver under gpu_exclusive.py. It never retries a failure.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'moe_replay_real'))
from analyze import analyze

from report import require, sha
from bundle import validate_bundle, markdown


def file_hash(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def stamp(path):
    stat = path.stat()
    return dict(bytes=stat.st_size, mtime_ns=stat.st_mtime_ns,
                ctime_ns=stat.st_ctime_ns, inode=stat.st_ino, device=stat.st_dev)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def prepare_model(model, output):
    index = json.loads((model / 'model.safetensors.index.json').read_text())
    names = sorted(set(index['weight_map'].values()) |
                   {'config.json', 'model.safetensors.index.json',
                    'tokenizer.json', 'ple/qwen3.8-flash-next-ple-fp8.bin'})
    rows = []
    for number, name in enumerate(names):
        path = (model / name).resolve()
        require(path.is_relative_to(model), 'model file escapes root')
        before = stamp(path)
        digest = file_hash(path)
        require(before == stamp(path), f'model changed while hashing: {name}')
        rows.append(dict(path=name, sha256=digest, **before))
        if number % 20 == 0 or number + 1 == len(names):
            print(f'hashed model files {number + 1}/{len(names)}', flush=True)
    (output / 'model-files.json').write_text(json.dumps(rows, indent=2) + '\n')
    # Stable content identity excludes machine-specific inode/timestamp fields.
    weights = [dict(path=r['path'], bytes=r['bytes'], sha256=r['sha256'])
               for r in rows if r['path'].endswith(('.safetensors', '.bin'))]
    metadata = [dict(path=r['path'], sha256=r['sha256'])
                for r in rows if r['path'].endswith('.json')]
    return rows, sha(canonical(metadata)), sha(canonical(weights))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--text', type=Path,
                        default=Path(__file__).resolve().parents[1] / 'moe_replay_real/text.txt')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[3]
    output = args.output.resolve()
    model = args.model.resolve()
    require(any(output.is_relative_to(root / part) for part in ('build', '.q4t-work')),
            'output outside work/build directory')
    output.mkdir(parents=True, exist_ok=False)
    state = dict(status='PREPARING', started=time.time(), gpu_processes=0,
                 environment={key: value for key, value in os.environ.items()
                              if key.startswith(('Q4T_', 'CUDA_'))})
    identity_file = output / 'run.json'
    identity_file.write_text(json.dumps(state, indent=2) + '\n')
    try:
        # Reject all experimental Q4T settings, not only the currently known FP8
        # switches. The diagnostic uses normal all-resident production arithmetic.
        require(not any(key.startswith('Q4T_') for key in os.environ),
                'unset Q4T_* overrides before capture')
        executable, text = output / 'executable', output / 'text.txt'
        shutil.copy2(args.binary.resolve(), executable)
        shutil.copy2(args.text.resolve(), text)
        state.update(binary_sha256=file_hash(executable), text_sha256=file_hash(text))
        sources = sorted(set(subprocess.check_output(
            ['git', 'ls-files', 'src', 'include', 'CMakeLists.txt'],
            cwd=root, text=True).splitlines()) |
            {str(p.relative_to(root)) for directory in ['moe_replay_real', 'moe_replay', 'upstream_first_fork']
             for p in (root / 'tools/verify' / directory).glob('*') if p.is_file()} |
            {'tools/verify/moe_replay_gpu.cu', 'tools/verify/moe_replay_io.cpp'})
        source_hashes = {}
        for name in sources:
            target = output / 'source' / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / name, target)
            source_hashes[name] = file_hash(target)
        source_digest = sha(canonical(source_hashes))
        state.update(source_sha256=source_digest, sources=source_hashes,
                     source_commit=subprocess.check_output(
                         ['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip())
        rows, model_digest, weights_digest = prepare_model(model, output)
        state.update(model_sha256=model_digest, weights_sha256=weights_digest)
        command = [str(executable), str(model), str(text), str(output / 'capture'),
                   model_digest, weights_digest, source_digest]
        plan = dict(binary_sha256=state['binary_sha256'],
                    source_sha256=source_digest, model_sha256=model_digest,
                    weights_sha256=weights_digest, text_sha256=state['text_sha256'],
                    command=command, model_loads=1, model_invocations=7,
                    layer_replays=17, records=24, upstream_layers=[0, 1, 2], http_requests=0,
                    token_rule='encode complete UTF-8; final four tokens forced',
                    exact_tokens='capture/tokens.i32 written before CUDA creation',
                    retries=0)
        (output / 'plan.json').write_text(json.dumps(plan, indent=2) + '\n')
        state.update(command=command, status='RUNNING', gpu_processes=1)
        identity_file.write_text(json.dumps(state, indent=2) + '\n')
        with (output / 'capture.log').open('w') as log:
            result = subprocess.run(command, cwd=root, stdout=log,
                                    stderr=subprocess.STDOUT)
        state['returncode'] = result.returncode
        require(result.returncode == 0, f'capture failed: {result.returncode}')
        require(json.loads((output / 'capture/completion.json').read_text())['status']
                == 'PASS', 'missing completion boundary')
        for row in rows:
            expected = {key: row[key] for key in stamp(model / row['path'])}
            require(stamp(model / row['path']) == expected,
                    f'model changed during capture: {row["path"]}')
        require(file_hash(executable) == state['binary_sha256'], 'binary changed')
        require(file_hash(text) == state['text_sha256'], 'text changed')
        for name, digest in source_hashes.items():
            require(file_hash(root / name) == digest and
                    file_hash(output / 'source' / name) == digest,
                    f'source changed: {name}')
        identity = dict(model=model_digest, weights=weights_digest,
                        binary=state['binary_sha256'], source=source_digest,
                        tokenizer=file_hash(model / 'tokenizer.json'))
        reports = validate_bundle(output / 'capture', identity)
        (output / 'comparisons.json').write_text(json.dumps(
            reports, indent=2, allow_nan=False) + '\n')
        (output / 'report.md').write_text(markdown(reports))
        upstream = analyze(output / 'capture', root /
                           '.q4t-work/moe-replay-real-20261019/gpu-v1/capture')
        (output / 'upstream.json').write_text(json.dumps(upstream, indent=2) + '\n')
        state['status'] = 'PASS'
    except BaseException as error:
        state.update(status='INTERRUPTED' if isinstance(error, KeyboardInterrupt)
                     else 'FAIL', error=repr(error))
        raise
    finally:
        state['finished'] = time.time()
        identity_file.write_text(json.dumps(state, indent=2) + '\n')
        files = {str(p.relative_to(output)): file_hash(p)
                 for p in sorted(output.rglob('*')) if p.is_file()}
        (output / 'files.json').write_text(json.dumps(files, indent=2) + '\n')
    print('PASS real-text capture and independent offline comparisons', flush=True)


if __name__ == '__main__':
    main()
