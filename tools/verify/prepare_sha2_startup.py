"""Prepare one fixed SHA2 startup acceptance plan; does not run candidate tests."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', type=Path, required=True)
    parser.add_argument('--previous-stage', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    stage, previous = args.stage.resolve(), args.previous_stage.resolve()
    output = root / '.q4t-work/sha2-startup-v1'
    artifact = previous / 'delivery-package-v2/artifact'
    model = Path('/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream')
    prior = root / '.q4t-work/acceptance-v2'
    vectors = stage / 'vectors.tsv'
    lengths = [0, 1, 55, 56, 63, 64, 65, 119, 120, 127, 128, 129,
               255, 256, 257, 65535, 65536, 65537, 1048576]
    inputs = [('pattern', n, bytes((i * 131 + i // 7) & 255 for i in range(n)))
              for n in lengths]
    inputs += [('abc', 3, b'abc'), ('a', 1000000, b'a' * 1000000),
               ('nist', 56, b'abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq')]
    vectors.write_text(''.join(f'{kind}\t{size}\t{hashlib.sha256(data).hexdigest()}\n'
                              for kind, size, data in inputs))
    binary = stage / 'build/q4t'
    parent = stage / 'parent/q4t'
    assert sha(parent) == '75e85bac509b793b9b69612539809a17b64d45febe1b408e019641fe01c2eb2d'
    plan = dict(version=1, classification='performance optimization: startup',
                source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip(),
                parent_sha256=sha(parent), candidate_sha256=sha(binary),
                output=str(output), stage_timeout_seconds=5400,
                http_requests=54, http_loads=6, retries=0, warmups=0,
                goal='same 65536 BF16 subset startup_total at least 20 seconds lower',
                guards=['all SHA digests exact; all identity/payload checks retained',
                        '22/22 quality; same-mode output identity',
                        'both-mode five-tier TTFT and final decode no confirmed attributable regression',
                        'same capacity, k3, GPU calculation, ownership and unified RAM budget'],
                statistics='descriptive bounded evidence; historical five-tier parent reused; no cross-start noninferiority claim',
                stop='first failed HTTP/contract, identity mismatch, timeout or ENV_ABORT; preserve baseline, no extra samples',
                scope='one SHA implementation; unchanged 65536 BF16 subset; full head default',
                cache='warm/ordinary page-cache state, no drop_caches; no physical cold-storage claim',
                reused='75e acceptance-v2 quality and candidate-on/off five tiers; unchanged GPU projection, lifecycle, accept/recovery contracts',
                steps=[], files={})
    def http(name, mode, on, *, old=False, health=False):
        fixture = (previous / 'delivery-package-v2/health-fixture' if health
                   else previous / 'fixtures' / mode)
        command = ['python3', '-B', str(root / 'tools/evalscope/run_acceptance.py'),
                   '--mode', mode, '--output', str(output / name),
                   '--binary', str(parent if old else binary), '--model-dir', str(model),
                   '--fixtures', str(fixture), '--startup-timeout', '900',
                   '--port', '8753', '--max-len', '208896', '--allow-unqualified-binary']
        if on:
            command += ['--mtp', '--mtp-subset-head', str(artifact)]
        reference = prior / (('quality-' if mode == 'quality' else 'candidate-')
                             + ('on' if on else 'off')) / 'results.json'
        command += ['--reference', str(reference)]
        plan['steps'].append(dict(name=name, command=command, timeout=2400))
    http('quality-on', 'quality', True)
    manifest = json.loads((artifact / 'manifest.json').read_text())
    plan['steps'].append(dict(name='sha-contract', timeout=300, command=[
        str(stage / 'build/q4t_sha256_contract'), str(vectors), str(output / 'hash-files'),
        str(model / manifest['source_shard']), manifest['source_sha256'],
        str(artifact / 'head.bf16'), manifest['head_sha256']]))
    plan['steps'].append(dict(name='artifact-contract', timeout=600, command=[
        'python3', '-B', str(root / 'tools/mtp_subset/validate_artifact.py'),
        '--artifact', str(artifact), '--model', str(model),
        '--reader', str(stage / 'build/q4t_subset_read'),
        '--output', str(output / 'artifact-cases')]))
    http('parent-start-a', 'quality', True, old=True, health=True)
    http('quality-off', 'quality', False)
    http('candidate-off', 'performance', False)
    http('parent-start-b', 'quality', True, old=True, health=True)
    http('candidate-on', 'performance', True)
    paths = [root / 'CMakeLists.txt', vectors, binary, parent,
             stage / 'build/q4t_subset_read', stage / 'build/q4t_sha256_contract',
             stage / 'build/CMakeCache.txt', stage / 'parent/CMakeCache.txt',
             stage / 'parent/q4t.release.json', root / 'tools/verify/sha256_contract.cpp',
             Path(__file__).resolve()]
    for folder in ['src', 'include', 'tools/evalscope', 'tools/mtp_subset']:
        paths += [p for p in (root / folder).rglob('*') if p.is_file()
                  and '.venv' not in p.parts and '__pycache__' not in p.parts
                  and '.git' not in p.parts]
    for folder in [artifact, previous / 'fixtures', previous / 'delivery-package-v2/health-fixture']:
        paths += [p for p in folder.rglob('*') if p.is_file()]
    for folder in ['quality-on', 'quality-off', 'candidate-on', 'candidate-off']:
        paths += [p for p in (prior / folder).rglob('*') if p.is_file()]
    snapshot = stage / 'source-snapshot'
    snapshot.mkdir(exist_ok=False)
    for path in sorted(set(paths)):
        plan['files'][str(path)] = sha(path)
        if path.is_relative_to(root) and '.q4t-work' not in path.relative_to(root).parts:
            target = snapshot / path.relative_to(root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    plan['source_snapshot'] = str(snapshot)
    (stage / 'frozen-plan.json').write_text(json.dumps(plan, indent=2) + '\n')
    print(json.dumps({k: v for k, v in plan.items() if k not in ('files', 'steps')}, indent=2))


if __name__ == '__main__':
    main()
