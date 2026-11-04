"""Archive CPU-only candidate evidence while an unrelated GPU service is live."""
import gzip
import hashlib
import io
import json
from pathlib import Path
import subprocess

import tarfile
from tools.serve_media_failure import stage as s

DEST = s.ROOT/'docs/evidence/serve-media-failure-cpu-20261103'
NAMES = ('cpu', 'cpu-before-format', 'cpu-before-decode-oom', 'fixtures', 'package',
         'inventory-review.json', 'numerical-review.json', 'binding-review.json',
         'inventory-review-before-decode-oom.json', 'numerical-review-before-decode-oom.json',
         'binding-review-before-decode-oom.json', 'preflight-gpu.json', 'environment-gate.json')


def verify(staged=False):
    s.h.verify_seal(DEST)
    manifest = s.h.read(DEST/'manifest.json')
    expected = {r['member']: r for r in manifest['records']}
    seen = set()
    with tarfile.open(DEST/'raw-records.tar.gz', 'r:gz') as archive:
        for member in archive:
            s.h.require(member.isfile() and member.name in expected and member.name not in seen,
                        'unknown/duplicate archive member')
            row = expected[member.name]
            raw = archive.extractfile(member).read()
            s.h.require(len(raw) == row['bytes'] and hashlib.sha256(raw).hexdigest() == row['sha256'],
                        'archived bytes changed')
            s.h.check_identity(row); seen.add(member.name)
    s.h.require(seen == set(expected), 'missing raw evidence')
    for row in manifest['local_artifacts']: s.h.check_identity(row)
    for name, row in manifest['aliases'].items():
        s.h.check_identity(row)
        s.h.require(s.h.sha(DEST/name) == row['sha256'], 'alias mismatch')
    files = [p for p in DEST.rglob('*') if p.is_file()]
    if staged:
        for path in files:
            data = subprocess.check_output(['git', 'show', ':'+str(path.relative_to(s.ROOT))], cwd=s.ROOT)
            s.h.require(hashlib.sha256(data).hexdigest() == s.h.sha(path), 'staged blob mismatch')
    s.h.require(s.h.sha(s.PACKAGE/'previous/q4t') == s.DEFAULT_SHA and
                s.h.sha(s.PACKAGE/'previous/q4t.release.json') == s.SIDECAR_SHA,
                'frozen previous pair changed')
    return dict(passed=True, raw_records=len(seen), local_artifacts=len(manifest['local_artifacts']),
        git_files=len(files), git_bytes=sum(p.stat().st_size for p in files),
        staged_git_blobs_verified=len(files) if staged else 0, gpu_acceptance=False,
        default_replaced=False, goal_complete=False)


def create():
    s.check(); s.cpu_gate()
    s.h.require(s.h.sha(s.ROOT/'build/q4t') == s.DEFAULT_SHA and
                s.h.sha(s.ROOT/'build/q4t.release.json') == s.SIDECAR_SHA,
                'default changed before CPU-only archive')
    s.h.require(not s.OUTPUT.exists(), 'CPU-only archive cannot include unreviewed GPU execution')
    s.h.require(s.h.read(s.BASE/'environment-gate.json')['status'] == 'ENV_ABORT', 'missing environment gate')
    DEST.mkdir()
    records, local = [], []
    with (DEST/'raw-records.tar.gz').open('xb') as stream:
        with gzip.GzipFile(fileobj=stream, mode='wb', mtime=0) as zipped:
            with tarfile.open(fileobj=zipped, mode='w|') as archive:
                for name in NAMES:
                    root = s.BASE/name
                    for path in sorted(root.rglob('*')) if root.is_dir() else [root]:
                        if not path.is_file(): continue
                        s.h.require(not path.is_symlink(), 'evidence symlink')
                        row = s.h.identity(path)
                        with path.open('rb') as source: magic = source.read(4)
                        if magic == b'\x7fELF' or path.suffix == '.bundle':
                            local.append(row); continue
                        raw = path.read_bytes()
                        member = str(path.relative_to(s.BASE))
                        item = tarfile.TarInfo(member)
                        item.size, item.mode, item.mtime = len(raw), 0o600, 0
                        archive.addfile(item, io.BytesIO(raw))
                        records.append(dict(**row, member=member))
    aliases = {'plan.json': s.PACKAGE/'plan.json', 'cpu-result.json': s.cpu.CPU/'tests/result.json',
        'original-cpu-result.json': s.BASE/'cpu-before-decode-oom/tests/result.json',
        'pixel-reference.json': s.cpu.CPU/'tests/pixels/reference.json',
        'environment-gate.json': s.BASE/'environment-gate.json',
        'build-result.json': s.cpu.CPU/'build-result.json'}
    for name, path in aliases.items(): s.h.copy_checked(s.h.identity(path), DEST/name)
    s.h.save(DEST/'manifest.json', dict(records=records, local_artifacts=local,
        aliases={k: s.h.identity(v) for k, v in aliases.items()},
        superseded_pre_format_and_pre_decoder_build_binaries_not_all_retained=True,
        superseded_results_not_reused_for_final_candidate=True,
        scope='CPU candidate only; original generated-document failure retained. GPU preflight occupied, no native HTTP/model tests or deployment. Exact final candidate/probes and frozen previous pair retained locally.'))
    (DEST/'README.md').write_text('''# SRV-02h CPU candidate evidence

CPU contracts, allocation-denial/processor bytes, build logs, source bindings,
frozen candidate/previous pair and GPU-occupied preflight. No GPU or live HTTP
acceptance, default replacement or completion of the industrialization goal.
The original generated-document check failure is retained. Superseded earlier
build hashes in raw logs are historical: their runtime binaries were rebuilt,
are not all retained, and are not reused as final-candidate acceptance.
Current ELF/source bundles stay local with explicit identities. This archive
does not claim that all original logs were redacted or that physical RAM is bounded.
''')
    s.h.seal(DEST)
    print(json.dumps(verify()))


if __name__ == '__main__':
    import sys
    if sys.argv[1:] == ['create']: create()
    elif sys.argv[1:] == ['verify']: print(json.dumps(verify()))
    elif sys.argv[1:] == ['custody']:
        result = verify(staged=True)
        result['checker'] = s.h.identity(Path(__file__))
        s.h.save(s.ROOT/'docs/evidence/serve-media-failure-cpu-custody-20261103.json', result)
        print(json.dumps(result))
    else: raise SystemExit('create | verify | custody')
