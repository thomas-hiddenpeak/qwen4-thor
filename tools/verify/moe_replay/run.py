#!/usr/bin/env python3
"""One bounded A1 suite + independent persisted-file replay, under one GPU lease."""
import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--fixture', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[3]
    output = args.output.resolve()
    if not any(output.is_relative_to(root / part) for part in ('build', '.q4t-work')):
        parser.error('output must be under build/ or .q4t-work/')
    output.mkdir(parents=True, exist_ok=False)
    binary = args.binary.resolve()
    supplied_binary = binary
    binary = output / "executable"
    shutil.copy2(supplied_binary, binary)
    fixture = args.fixture.resolve()
    manifest = dict(
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'],
                                               cwd=root, text=True).strip(),
        binary=str(binary), original_binary=str(supplied_binary),
        binary_sha256=digest(binary),
        fixture=str(fixture), fixture_sha256=digest(fixture),
        model=str(args.model.resolve()),
        environment={key: value for key, value in os.environ.items()
                     if key.startswith(('Q4T_', 'CUDA_'))},
        commands=[], status='RUNNING')
    patch = subprocess.check_output(['git', 'diff', 'HEAD'], cwd=root)
    (output / 'source.patch').write_bytes(patch)
    manifest['source_patch_sha256'] = hashlib.sha256(patch).hexdigest()
    # Include untracked implementation sources, not just git diff.
    sources = ['tools/verify/moe_replay/main.cpp',
               'tools/verify/moe_replay/run.py',
               'tools/verify/moe_replay_gpu.cu',
               'tools/verify/moe_replay_io.cpp', 'src/model/moe.cu',
               'include/q4t/model/moe_replay.h',
               'include/q4t/model/moe_replay_gpu.h',
               'include/q4t/model/moe_replay_io.h', 'CMakeLists.txt']
    manifest['sources'] = {name: digest(root / name) for name in sources}
    for name in sources:
        snapshot = output / 'source' / name
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / name, snapshot)
    identity = output / 'identity.json'
    identity.write_text(json.dumps(manifest, indent=2) + '\n')
    try:
        for mode in ('suite', 'replay'):
            command = [str(binary), mode, str(args.model.resolve()), str(fixture),
                       str(output / mode)]
            if mode == 'replay':
                command.append(str(output / 'suite/t4-row0.route'))
            manifest['commands'].append(command)
            identity.write_text(json.dumps(manifest, indent=2) + '\n')
            with (output / (mode + '.log')).open('w') as log:
                result = subprocess.run(command, cwd=root, stdout=log,
                                        stderr=subprocess.STDOUT)
            manifest[mode + '_returncode'] = result.returncode
            if result.returncode:
                raise RuntimeError(f'{mode} exited {result.returncode}')
        for suffix in ('logits.bf16', 'ids.i32', 'weights.f32', 'output.bf16'):
            reference = output / ('suite/t4-row0-normal.' + suffix)
            replay = output / ('replay/replay.' + suffix)
            if reference.read_bytes() != replay.read_bytes():
                raise RuntimeError(f'persisted CLI replay mismatch: {suffix}')
        if digest(binary) != manifest['binary_sha256']:
            raise RuntimeError('binary changed during run')
        for name, expected in manifest['sources'].items():
            if digest(root / name) != expected:
                raise RuntimeError(f'source changed during run: {name}')
        manifest['status'] = 'PASS'
    except Exception as error:
        manifest['status'] = 'FAIL'
        manifest['error'] = str(error)
        raise
    finally:
        identity.write_text(json.dumps(manifest, indent=2) + '\n')
        files = {str(path.relative_to(output)): digest(path)
                 for path in sorted(output.rglob('*')) if path.is_file()}
        (output / 'files.json').write_text(json.dumps(files, indent=2) + '\n')
    print('PASS: bounded GPU suite and separate persisted CLI replay')


if __name__ == '__main__':
    main()
