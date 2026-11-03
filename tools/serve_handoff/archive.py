"""Archive original failures and corrected handoff delivery without inference."""
import gzip
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_handoff import stage, rollback

h = stage.h
DEST = ROOT/'docs/evidence/serve-structured-handoff-20261103'


def verify():
    h.verify_seal(DEST)
    manifest = h.read(DEST/'manifest.json')
    expected = {r['member']:r for r in manifest['records']}
    seen = set()
    with tarfile.open(DEST/'raw-records.tar.gz', 'r:gz') as archive:
        for entry in archive:
            h.require(entry.isfile() and entry.name in expected and entry.name not in seen,
                      'unexpected archive member')
            raw = archive.extractfile(entry).read(); row = expected[entry.name]
            h.require(len(raw) == row['bytes'] and hashlib.sha256(raw).hexdigest() == row['sha256'],
                      'archive bytes changed')
            h.check_identity(row); seen.add(entry.name)
    h.require(seen == expected.keys(), 'missing archived record')
    for row in manifest['local_artifacts']: h.check_identity(row)
    for name, row in manifest['aliases'].items():
        h.check_identity(row); h.require(h.sha(DEST/name) == row['sha256'], 'alias changed')
    rollback.check()
    return dict(passed=True, records=len(seen), local_artifacts=len(manifest['local_artifacts']),
                new_model_or_http=0)


def create():
    rollback.check()
    DEST.mkdir()
    selected = []
    for name in ('cpu', 'cpu-build-failed01', 'cpu-before-format', 'driver-cpu',
                 'driver-corrected', 'package', 'execution', 'correction', 'rollback-bundle'):
        folder = stage.BASE/name
        selected += [('handoff/'+str(p.relative_to(stage.BASE)), p)
                     for p in sorted(folder.rglob('*')) if p.is_file()]
    managed = ROOT/'.q4t-work/serve-managed-service-20261103'
    selected += [('managed-original/'+str(p.relative_to(managed)), p)
                 for p in sorted((managed/'execution').rglob('*')) if p.is_file()]
    for name in ('correction-plan.json', 'postflight.json'):
        selected.append(('handoff/'+name, stage.BASE/name))
    records, local = [], []
    with (DEST/'raw-records.tar.gz').open('xb') as stream:
        with gzip.GzipFile(fileobj=stream, mode='wb', mtime=0) as zipped:
            with tarfile.open(fileobj=zipped, mode='w|') as archive:
                for member, path in selected:
                    h.require(not path.is_symlink(), 'evidence symlink')
                    row = h.identity(path)
                    with path.open('rb') as source: magic = source.read(4)
                    if magic == b'\x7fELF': local.append(row); continue
                    raw = path.read_bytes()
                    entry = tarfile.TarInfo(member)
                    entry.size, entry.mode, entry.mtime = len(raw), 0o600, 0
                    archive.addfile(entry, io.BytesIO(raw))
                    records.append(dict(**row, member=member))
    aliases = dict(original_managed=managed/'execution/result.json',
        original_gpu=stage.BASE/'execution/business/result.json',
        accepted=stage.BASE/'correction/business/result.json',
        guard=stage.BASE/'correction/lease/exit.json',
        finalized=stage.BASE/'correction/finalized.json', cpu=stage.CPU/'tests/result.json'
        if hasattr(stage, 'CPU') else stage.cpu.CPU/'tests/result.json',
        postflight=stage.BASE/'postflight.json')
    aliases = {name+'.json':h.identity(path) for name,path in aliases.items()}
    for name,row in aliases.items(): h.copy_checked(row, DEST/name)
    h.save(DEST/'manifest.json', dict(records=records, local_artifacts=local, aliases=aliases,
        archiver=h.identity(Path(__file__)), scope='Original failures, complete wire, CPU and corrected delivery; '
        'original healthz not sent and standalone final sample absent. No performance/long-soak claim.'))
    h.seal(DEST)
    return verify()


if __name__ == '__main__':
    print(json.dumps(create() if sys.argv[1:] == ['create'] else verify()))
