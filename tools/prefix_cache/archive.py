"""Preserve raw stages, failed reference and SHA-bound inputs without size gates."""
import hashlib
import json
from pathlib import Path
import shutil
import time

ROOT=Path(__file__).resolve().parents[2]
BASE=ROOT/'.q4t-work/prefix-cache-20261028'
def sha(path):
    with Path(path).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()


def main():
    audit=json.loads((BASE/'final-audit.json').read_text())
    if not audit['passed']:raise ValueError('completed audit required')
    out=BASE/'evidence-archive-v1';out.mkdir()
    records={};bindings={}
    def copy(source,relative,digest=None):
        source=Path(source);actual=sha(source)
        if digest is not None and actual!=digest:raise ValueError('input identity: '+str(source))
        target=out/relative
        if not target.exists():
            target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,target)
        if sha(target)!=actual:raise ValueError('copy identity')
        records[str(relative)]=dict(sha256=actual,bytes=source.stat().st_size)
    for phase in audit['phases']:
        directory=BASE/phase['name'];plan=json.loads((directory/'plan.json').read_text())
        bindings[phase['name']]={}
        for name,digest in plan['files'].items():
            source=Path(name)
            if sha(source)!=digest:
                old=BASE/'phase-b-reference/original-state-contract.cpp'
                if name==str(ROOT/'tools/prefix_cache/state_contract.cpp') and sha(old)==digest:source=old
                else:raise ValueError('unresolved historic binding: '+name)
            copy(source,Path('inputs')/digest,digest)
            bindings[phase['name']][name]=digest
        for source in directory.rglob('*'):
            if source.is_file():copy(source,Path('stages')/phase['name']/source.relative_to(directory))
    for source in BASE.iterdir():
        if source.is_file():copy(source,Path('review')/source.name)
    # Keep the unexecuted protection plan and original frozen source manifest.
    for directory in [BASE/'phase-d',BASE/'frozen',
                      BASE/'unexecuted-release-tools']:
        for source in directory.rglob('*'):
            if source.is_file() and not source.is_symlink():
                copy(source,Path('preserved')/source.relative_to(BASE))
    for source in (ROOT/'tools/prefix_cache').iterdir():
        if source.is_file():copy(source,Path('tools')/source.name)
    manifest=dict(files=records,bindings=bindings,created=time.time(),
        note='Original failed stage retained. Old fixture binding reconstructed from preserved 94266fc source; no inference rerun or raw replacement.')
    (out/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    for name,value in records.items():
        if sha(out/name)!=value['sha256']:raise ValueError('final archive mismatch')
    receipt=dict(passed=True,directory=str(out),files=len(records),bytes=sum(r['bytes'] for r in records.values()),
        manifest_sha256=sha(out/'manifest.json'),new_inference_requests=0)
    (BASE/'archive-receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
    print(json.dumps(receipt,indent=2))


if __name__=='__main__':main()
