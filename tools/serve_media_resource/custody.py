"""Retain the standalone host group's exact records, with no new test execution."""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[2]
RUN = ROOT/'.q4t-work/serve-media-plan-core-20261103/run-01'
DEST = ROOT/'docs/evidence/serve-media-plan-core-20261103'


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read(path):
    return json.loads(path.read_text())


def save(path, value):
    path.write_text(json.dumps(value, indent=2)+'\n')


def require(condition, why):
    if not condition:
        raise RuntimeError(why)


def identity(path):
    return dict(path=str(path), bytes=path.stat().st_size, sha256=sha(path))


def create():
    result = read(RUN/'result.json')
    require(result['passed'] and result['sources_unchanged'] and
            result['default_pair_unchanged'] and result['frozen_02h_gate_passed'],
            'CPU/source/default gate')
    require('80 cases, 80 passed, 0 failed; 0 allocation attempts' in
            (RUN/'contract.stdout').read_text(), 'contract count')
    require('36 real processor cases passed' in
            (RUN/'processor.stdout').read_text(), 'processor count')
    for path, digest in read(RUN/'sources.json').items():
        require(sha(ROOT/path) == digest, 'tested source changed: '+path)
    for path, digest in read(RUN/'begin.json')['defaults'].items():
        require(sha(ROOT/path) == digest, 'default pair changed')
    for row in result['binaries'].values():
        require(identity(Path(row['path'])) == row, 'binary changed')
    records = sorted(p for p in RUN.iterdir() if p.is_file())
    records += sorted(p for p in (RUN/'fixtures').rglob('*') if p.is_file())
    records += [RUN/'build/CMakeCache.txt', RUN/'build/compile_commands.json']
    DEST.mkdir()
    manifest = []
    with (DEST/'raw-records.tar.gz').open('xb') as stream:
        with gzip.GzipFile(fileobj=stream, mode='wb', mtime=0) as zipped:
            with tarfile.open(fileobj=zipped, mode='w|') as archive:
                for path in records:
                    require(not path.is_symlink(), 'symlink evidence')
                    member = str(path.relative_to(RUN))
                    raw = path.read_bytes()
                    info = tarfile.TarInfo(member)
                    info.size, info.mode, info.mtime = len(raw), 0o600, 0
                    archive.addfile(info, io.BytesIO(raw))
                    manifest.append(dict(**identity(path), member=member))
    save(DEST/'manifest.json', dict(records=manifest,
        local_artifacts=list(result['binaries'].values()),
        compressed=identity(DEST/'raw-records.tar.gz'),
        archiver=identity(Path(__file__)),
        archiver_added_after_tests=True, stage_gpu_tests=0,
        scope='Standalone host planner only. Unchanged real processor geometry; '
              'not runtime integration, physical RAM bounds, GPU or HTTP acceptance.'))
    (DEST/'result.json').write_bytes((RUN/'result.json').read_bytes())
    (DEST/'README.md').write_text('''# Standalone media planner CPU evidence

80 contract cases, 36 real PNG/JPEG processor geometry comparisons, zero
warnings and no ASan/UBSan findings. The planner attempted no allocation
under persistent C++/C allocator denial. Exact logs, small generated images,
commands, tested source hashes and build configuration are archived.

The two host executables stay local with SHA-256 identities. No model, GPU
test or HTTP service was started. This core is not yet integrated in serve;
the default binary and frozen SRV-02h candidate stayed unchanged. Header/RGB
budgets do not bound decoder internal memory or reserve physical RAM.
''')
    print(json.dumps(verify()))


def verify(staged=False):
    manifest = read(DEST/'manifest.json')
    require(identity(DEST/'raw-records.tar.gz') == manifest['compressed'],
            'archive changed')
    expected = {r['member']: r for r in manifest['records']}
    require(len(expected) == len(manifest['records']), 'duplicate manifest')
    seen = set()
    with tarfile.open(DEST/'raw-records.tar.gz', 'r:gz') as archive:
        for member in archive:
            require(member.isfile() and member.name in expected and
                    member.name not in seen, 'unknown/duplicate member')
            row = expected[member.name]
            data = archive.extractfile(member).read()
            require(len(data) == row['bytes'] and
                    hashlib.sha256(data).hexdigest() == row['sha256'],
                    'archived record changed')
            require(identity(Path(row['path'])) ==
                    {k: row[k] for k in ('path', 'bytes', 'sha256')},
                    'local record changed')
            seen.add(member.name)
    require(seen == set(expected), 'missing member')
    for row in manifest['local_artifacts']:
        require(identity(Path(row['path'])) == row, 'local binary changed')
    require(sha(DEST/'result.json') == sha(RUN/'result.json'), 'result alias')
    files = sorted(p for p in DEST.iterdir() if p.is_file())
    if staged:
        for path in files:
            raw = subprocess.check_output(['git', 'show',
                ':'+str(path.relative_to(ROOT))], cwd=ROOT)
            require(hashlib.sha256(raw).hexdigest() == sha(path),
                    'staged blob changed')
    return dict(passed=True, raw_records=len(seen), local_artifacts=2,
                git_files=len(files), git_bytes=sum(p.stat().st_size for p in files),
                staged_blobs=len(files) if staged else 0,
                model_or_http_started=False, default_replaced=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('create', 'verify', 'custody'))
    args = parser.parse_args()
    if args.action == 'create':
        create()
    elif args.action == 'verify':
        print(json.dumps(verify()))
    else:
        result = verify(staged=True)
        save(ROOT/'docs/evidence/serve-media-plan-core-custody-20261103.json', result)
        print(json.dumps(result))
