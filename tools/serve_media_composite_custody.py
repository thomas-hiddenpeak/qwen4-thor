"""Retain composite GPU preparation/CPU evidence without running the candidate."""
import gzip
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.serve_media_composite import stage as s

DEST = ROOT/'docs/evidence/serve-media-composite-cpu-20261103'
h = s.h


def verify(staged=False):
    h.verify_seal(DEST)
    manifest = h.read(DEST/'manifest.json')
    expected = {r['member']: r for r in manifest['records']}
    h.require(len(expected) == len(manifest['records']), 'duplicate manifest')
    seen = set()
    with tarfile.open(DEST/'raw-records.tar.gz', 'r:gz') as archive:
        for member in archive:
            h.require(member.isfile() and member.name in expected and member.name not in seen,
                      'unknown/duplicate member')
            row = expected[member.name]
            raw = archive.extractfile(member).read()
            h.require(len(raw) == row['bytes'] and hashlib.sha256(raw).hexdigest() == row['sha256'],
                      'archived record changed')
            h.check_identity(row); seen.add(member.name)
    h.require(seen == set(expected), 'missing record')
    for row in manifest['local_artifacts']: h.check_identity(row)
    for name, row in manifest['aliases'].items():
        h.check_identity(row)
        h.require(h.sha(DEST/name) == row['sha256'], 'alias differs')
    files = [p for p in DEST.rglob('*') if p.is_file()]
    if staged:
        for path in files:
            data = subprocess.check_output(['git', 'show', ':'+str(path.relative_to(ROOT))], cwd=ROOT)
            h.require(hashlib.sha256(data).hexdigest() == h.sha(path), 'staged blob differs')
    return dict(passed=True, raw_records=len(seen), local_artifacts=len(manifest['local_artifacts']),
        git_files=len(files), git_bytes=sum(p.stat().st_size for p in files),
        staged_blobs=len(files) if staged else 0, model_or_http_started=False,
        gpu_acceptance=False, default_replaced=False, whole_goal_complete=False)


def create():
    s.check(); s.cpu_gate()
    h.require(not s.OUTPUT.exists(), 'unexpected GPU execution')
    h.require(h.sha(ROOT/'build/q4t') == s.DEFAULT_SHA and
              h.sha(ROOT/'build/q4t.release.json') == s.SIDECAR_SHA, 'default changed')
    h.require(h.read(s.BASE/'environment-gate.json')['status'] == 'ENV_ABORT', 'environment gate')
    paths = [p for p in s.BASE.iterdir() if p.is_file()]
    for name in ('cpu-before-deadline-review', 'cpu', 'fixtures', 'package'):
        paths += [p for p in (s.BASE/name).rglob('*') if p.is_file()]
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
                    raw = path.read_bytes(); member = str(path.relative_to(s.BASE))
                    info = tarfile.TarInfo(member)
                    info.size, info.mode, info.mtime = len(raw), 0o600, 0
                    archive.addfile(info, io.BytesIO(raw))
                    records.append(dict(**row, member=member))
    aliases = {'plan.json': s.PACKAGE/'plan.json', 'cpu-result.json': s.cpu.CPU/'tests/result.json',
               'build-result.json': s.cpu.CPU/'build-result.json',
               'fixture-reuse.json': s.cpu.CPU/'tests/fixture-reuse.json',
               'environment-gate.json': s.BASE/'environment-gate.json'}
    for name, path in aliases.items(): h.copy_checked(h.identity(path), DEST/name)
    h.save(DEST/'manifest.json', dict(records=records, local_artifacts=local,
        aliases={k: h.identity(v) for k, v in aliases.items()},
        archiver=h.identity(Path(__file__)), archiver_added_after_cpu_and_freeze=True,
        scope='Composite stage prepared, not executed. CPU fixture/driver checks only; '
              'original 10-case driver and corrected 13-case driver records retained. '
              'Same-ELF fixture reused. Device probe built but never run; vLLM occupied. '
              'No native HTTP, model, real GPU business or deployment acceptance.'))
    (DEST/'README.md').write_text('''# Composite media preparation and CPU evidence

Frozen 32-generation / 21-negative / 11-start GPU plan, exact commands,
source bindings, build and CPU logs, compressed expansion fixture, and both
driver revisions are preserved. CPU checked 11 actual host cases and recovery;
the revised driver passed 13 offline cases. The unchanged fixture ELF was
reused after the control-flow review; its decompression was not repeated.

Production/validation candidates, two new probe ELFs, previous default pair
and source bundle remain local with identities. The device probe is built but
unexecuted. GPU preflight still sees the unrelated vLLM service, preserved as
ENV_ABORT without a test lease or service interference. No model/HTTP/GPU,
default replacement, physical RAM protection or whole-goal acceptance is claimed.
''')
    h.seal(DEST)
    print(json.dumps(verify()))


if __name__ == '__main__':
    if sys.argv[1:] == ['create']: create()
    elif sys.argv[1:] == ['verify']: print(json.dumps(verify()))
    elif sys.argv[1:] == ['custody']:
        result = verify(staged=True)
        result['checker'] = h.identity(Path(__file__))
        h.save(ROOT/'docs/evidence/serve-media-composite-cpu-custody-20261103.json', result)
        print(json.dumps(result))
    else: raise SystemExit('create | verify | custody')
