"""Archive exact CPU records and both failures; no model or test execution."""
import gzip
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_media_integration import freeze_cpu as f

h, BASE = f.h, f.BASE
DEST = ROOT/'docs/evidence/serve-media-integration-cpu-20261103'


def verify(staged=False):
    h.verify_seal(DEST)
    manifest = h.read(DEST/'manifest.json')
    expected = {row['member']: row for row in manifest['records']}
    h.require(len(expected) == len(manifest['records']), 'duplicate manifest')
    seen = set()
    with tarfile.open(DEST/'raw-records.tar.gz', 'r:gz') as archive:
        for member in archive:
            h.require(member.isfile() and member.name in expected and
                      member.name not in seen, 'unknown/duplicate member')
            row = expected[member.name]
            data = archive.extractfile(member).read()
            h.require(len(data) == row['bytes'] and
                      hashlib.sha256(data).hexdigest() == row['sha256'],
                      'archived bytes changed')
            h.check_identity(row)
            seen.add(member.name)
    h.require(seen == set(expected), 'missing record')
    for row in manifest['local_artifacts']: h.check_identity(row)
    for name, row in manifest['aliases'].items():
        h.check_identity(row)
        h.require(h.sha(DEST/name) == row['sha256'], 'alias changed')
    files = sorted(p for p in DEST.rglob('*') if p.is_file())
    if staged:
        for path in files:
            data = subprocess.check_output(['git', 'show',
                ':'+str(path.relative_to(ROOT))], cwd=ROOT)
            h.require(hashlib.sha256(data).hexdigest() == h.sha(path),
                      'staged blob changed')
    return dict(passed=True, raw_records=len(seen),
        local_artifacts=len(manifest['local_artifacts']), git_files=len(files),
        git_bytes=sum(p.stat().st_size for p in files),
        staged_blobs=len(files) if staged else 0,
        gpu_acceptance=False, default_replaced=False, whole_goal_complete=False)


def create():
    f.gate(); h.verify_seal(f.PACKAGE)
    paths = [p for p in BASE.iterdir() if p.is_file()]
    for name in ('cpu-01', 'cpu-correction-01', 'cpu-accepted-incomplete-01',
                 'cpu-accepted'):
        paths += [p for p in (BASE/name).rglob('*') if p.is_file()]
    DEST.mkdir()
    records, local = [], []
    with (DEST/'raw-records.tar.gz').open('xb') as stream:
        with gzip.GzipFile(fileobj=stream, mode='wb', mtime=0) as zipped:
            with tarfile.open(fileobj=zipped, mode='w|') as archive:
                for path in sorted(paths):
                    h.require(not path.is_symlink(), 'evidence symlink')
                    row = h.identity(path)
                    with path.open('rb') as source: magic = source.read(4)
                    if magic == b'\x7fELF' or path.suffix == '.bundle':
                        local.append(row); continue
                    data = path.read_bytes()
                    member = str(path.relative_to(BASE))
                    info = tarfile.TarInfo(member)
                    info.size, info.mode, info.mtime = len(data), 0o600, 0
                    archive.addfile(info, io.BytesIO(data))
                    records.append(dict(**row, member=member))
    aliases = {'result.json': f.PACKAGE/'result.json',
               'original-failure.json': f.fix.FIRST/'tests/result.json',
               'correction.json': f.fix.OUT/'tests/result.json',
               'pixel-reference.json': f.fix.OUT/'tests/pixels/reference.json',
               'gpu-occupied.json': BASE/'cpu-complete-gpu.json'}
    for name, path in aliases.items(): h.copy_checked(h.identity(path), DEST/name)
    h.save(DEST/'manifest.json', dict(records=records, local_artifacts=local,
        aliases={k: h.identity(v) for k, v in aliases.items()},
        archiver=h.identity(Path(__file__)), archiver_added_after_tests=True,
        original_probe_binaries_retained=True,
        scope='CPU runtime integration only. First host/ledger failure and '
              'incomplete packaging attempt retained. Corrected C count is 27, '
              'not the previous mixed C/C++ count of 68. No GPU, native HTTP, '
              'model quality, deployment, physical RAM or whole-goal acceptance.'))
    (DEST/'README.md').write_text('''# SRV-02i integration CPU evidence

Exact build/test logs, source snapshots, small PNG/JPEG fixtures, processor
pixels and candidate configuration are archived. Large ELF files and objects
stay local with identities, including the original failed probe executables.
The candidate and validation ELF, plus the previous default pair, are frozen.

The first CPU group failed one host test in each mode and the allocation
ledger probe. Only affected probes were corrected and rerun; unchanged
successes are reused. C-only injection now covers 27 distinct boundaries;
the old count 68 included 41 C++ calls and is not retained as C-only evidence.
The initial packaging attempt assumed missing public-host compile command
exports. That attempt is preserved; packaging was corrected without tests.

This is CPU acceptance only. vLLM still occupies the GPU; no model, listener,
GPU acceptance or default replacement occurred. Cooperative cancellation and
decoder-owned quotas do not prove preemption or a physical RAM hard limit.
''')
    h.seal(DEST)
    print(json.dumps(verify()))


if __name__ == '__main__':
    if sys.argv[1:] == ['create']: create()
    elif sys.argv[1:] == ['verify']: print(json.dumps(verify()))
    elif sys.argv[1:] == ['custody']:
        result = verify(staged=True)
        result['checker'] = h.identity(Path(__file__))
        h.save(ROOT/'docs/evidence/serve-media-integration-cpu-custody-20261103.json', result)
        print(json.dumps(result))
    else: raise SystemExit('create | verify | custody')
