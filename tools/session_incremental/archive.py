"""Preserve the sole candidate and all raw evidence after bounded execution."""
import hashlib
import json
from pathlib import Path
import shutil
import time

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT/'.q4t-work/session-incremental-delivery-20261029'
OUT = BASE.with_name(BASE.name+'-archive')


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    decision = json.loads((BASE/'decision.json').read_text())
    if json.loads((BASE/'run/state.json').read_text())['status'] == 'RUNNING':
        raise RuntimeError('Never archive live measurements')
    if (BASE/'io-preparation.data').exists():
        raise RuntimeError('Owned preparation file not released')
    plan = json.loads((BASE/'plan.json').read_text())
    OUT.mkdir()
    records = {}
    def copy(path, name, expected=None):
        digest = sha(path)
        if expected is not None and digest != expected:
            raise RuntimeError('Input changed: '+str(path))
        target = OUT/name
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        if sha(target) != digest:
            raise RuntimeError('Archive copy mismatch')
        records[str(name)] = dict(sha256=digest, bytes=path.stat().st_size)
    for path, digest in plan['files'].items():
        copy(Path(path), Path('inputs')/digest, digest)
    for path in sorted(BASE.rglob('*')):
        if path.is_file():
            if path.is_symlink():
                raise RuntimeError('Unexpected raw symlink')
            copy(path, Path('raw')/path.relative_to(BASE))
    for path in (ROOT/'tools/session_incremental').glob('*'):
        if path.is_file():
            copy(path, Path('tools')/path.name)
    for path in (ROOT/'docs/evidence/session-incremental-20261029').glob('*'):
        if path.is_file():
            copy(path, Path('research-provenance')/path.name)
    manifest = dict(created=time.time(), files=records, bindings=plan['files'],
        candidate_sha256=plan['candidate_sha256'],
        engineering_decision=decision['engineering_decision'],
        note='All raw output preserved; no log-size gate or changed failed evidence.')
    (OUT/'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    if {str(p.relative_to(OUT)) for p in OUT.rglob('*') if p.is_file()} != set(records)|{'manifest.json'}:
        raise RuntimeError('Archive file set mismatch')
    for name, record in records.items():
        if sha(OUT/name) != record['sha256']:
            raise RuntimeError('Final archive mismatch')
    receipt = dict(passed=True, files=len(records),
        bytes=sum(r['bytes'] for r in records.values()),
        directory=str(OUT), manifest_sha256=sha(OUT/'manifest.json'))
    (BASE/'archive-receipt.json').write_text(json.dumps(receipt, indent=2)+'\n')
    print(json.dumps(receipt, indent=2))


if __name__ == '__main__':
    main()
