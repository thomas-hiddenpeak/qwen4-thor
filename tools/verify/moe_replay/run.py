#!/usr/bin/env python3
"""One bounded A1 suite + independent persisted-file replay, under one GPU lease."""
import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path
import subprocess


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
    identity = output / 'identity.json'
    manifest = dict(
        original_binary=str(args.binary.resolve()),
        original_fixture=str(args.fixture.resolve()),
        model=str(args.model.resolve()),
        environment={key: value for key, value in os.environ.items()
                     if key.startswith(('Q4T_', 'CUDA_'))},
        commands=[], status='PREPARING')
    identity.write_text(json.dumps(manifest, indent=2) + '\n')
    try:
        # Snapshot every input that can otherwise be overwritten by rebuilding
        # or editing a fixture. Execute these copies, not the mutable originals.
        binary = output / 'executable'
        fixture = output / 'fixture.json'
        shutil.copy2(args.binary.resolve(), binary)
        shutil.copy2(args.fixture.resolve(), fixture)
        manifest.update(binary=str(binary), binary_sha256=digest(binary),
                        fixture=str(fixture), fixture_sha256=digest(fixture))
        manifest['source_commit'] = subprocess.check_output(
            ['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
        patch = subprocess.check_output(['git', 'diff', 'HEAD'], cwd=root)
        (output / 'source.patch').write_bytes(patch)
        manifest['source_patch_sha256'] = hashlib.sha256(patch).hexdigest()
        # These are observed source identities; they do not alone prove which
        # source tree built a caller-supplied binary. Preserve its separate SHA.
        sources = ['tools/verify/moe_replay/main.cpp',
                   'tools/verify/moe_replay/run.py',
                   'tools/verify/moe_replay_gpu.cu',
                   'tools/verify/moe_replay_io.cpp', 'src/model/moe.cu',
                   'include/q4t/model/moe_replay.h',
                   'include/q4t/model/moe_replay_gpu.h',
                   'include/q4t/model/moe_replay_io.h', 'CMakeLists.txt']
        manifest['sources'] = {}
        for name in sources:
            snapshot = output / 'source' / name
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / name, snapshot)
            manifest['sources'][name] = digest(snapshot)

        def verify_snapshots():
            if digest(binary) != manifest['binary_sha256']:
                raise RuntimeError('binary snapshot changed during run')
            if digest(fixture) != manifest['fixture_sha256']:
                raise RuntimeError('fixture snapshot changed during run')
            for name, expected in manifest['sources'].items():
                if digest(output / 'source' / name) != expected:
                    raise RuntimeError(f'source snapshot changed: {name}')

        manifest['status'] = 'RUNNING'
        for mode in ('suite', 'replay'):
            verify_snapshots()
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
        verify_snapshots()
        if digest(args.fixture.resolve()) != manifest['fixture_sha256']:
            raise RuntimeError('original fixture changed during run')
        for name, expected in manifest['sources'].items():
            if digest(root / name) != expected:
                raise RuntimeError(f'source changed during run: {name}')
        manifest['status'] = 'PASS'
    except BaseException as error:
        manifest['status'] = ('INTERRUPTED' if isinstance(error, KeyboardInterrupt)
                              else 'FAIL')
        manifest['error'] = f'{type(error).__name__}: {error}'
        raise
    finally:
        identity.write_text(json.dumps(manifest, indent=2) + '\n')
        files = {str(path.relative_to(output)): digest(path)
                 for path in sorted(output.rglob('*')) if path.is_file()}
        (output / 'files.json').write_text(json.dumps(files, indent=2) + '\n')
    print('PASS: bounded GPU suite and separate persisted CLI replay')


if __name__ == '__main__':
    main()
