"""Lossless Git custody of small raw records; ELF/build bundles stay local."""
import argparse
import gzip
import hashlib
import io
import json
import re
from urllib.parse import unquote
from pathlib import Path
import subprocess
import tarfile

from tools.serve_device_memory import correction as c

h,ROOT=c.h,c.ROOT
DEST=ROOT/'docs/evidence/serve-device-memory-20261103'


def raw_files():
    groups=[('original',c.original.BASE,('cpu','package','execution',
        'inventory-review.json','drain-review.json','cleanup-lock-review.json')),
        ('correction',c.BASE,('cpu','package','execution','rollback-bundle',
            'audit-before-freeze.json','release-history.json','postflight.json','postflight-gpu.json'))]
    for label,base,names in groups:
        for name in names:
            root=base/name
            for path in sorted(root.rglob('*')) if root.is_dir() else [root]:
                if path.is_file():
                    h.require(not path.is_symlink(),'symlink evidence disallowed')
                    yield label+'/'+str(path.relative_to(base)),path


def create():
    for path in (c.original.PACKAGE,c.ORIGINAL_OUTPUT,c.PACKAGE,c.OUTPUT,c.BASE/'rollback-bundle'):
        h.verify_seal(path)
    h.require(h.read(c.OUTPUT/'finalized.json')['status']=='DELIVERED','delivery incomplete')
    DEST.mkdir()
    records,local=[],[]
    with (DEST/'raw-records.tar.gz').open('xb') as stream:
        with gzip.GzipFile(fileobj=stream,mode='wb',mtime=0) as compressed:
            with tarfile.open(fileobj=compressed,mode='w|') as archive:
                for member,path in raw_files():
                    record=h.identity(path)
                    with path.open('rb') as source:magic=source.read(4)
                    if magic==b'\x7fELF' or path.suffix=='.bundle' or 'private-cache' in path.parts:
                        local.append(dict(**record,reason='executable/object, Git source bundle or private session snapshot retained locally'))
                        continue
                    data=path.read_bytes()
                    h.require(hashlib.sha256(data).hexdigest()==record['sha256'],'raw file changed')
                    item=tarfile.TarInfo(member);item.size=len(data);item.mode=0o600;item.mtime=0
                    archive.addfile(item,io.BytesIO(data))
                    records.append(dict(**record,member=member))
    aliases={
        'original-plan.json':c.original.PACKAGE/'plan.json',
        'original-result.json':c.ORIGINAL_OUTPUT/'business/result.json',
        'original-guard.json':c.ORIGINAL_OUTPUT/'lease/exit.json',
        'reused-evidence.json':c.PACKAGE/'reused-evidence.json',
        'plan.json':c.PACKAGE/'plan.json',
        'result.json':c.OUTPUT/'business/result.json',
        'guard.json':c.OUTPUT/'lease/exit.json',
        'finalized.json':c.OUTPUT/'finalized.json',
        'installed-health.json':c.OUTPUT/'health/result.json',
        'native-result.json':c.ORIGINAL_OUTPUT/'native/result.json',
        'cpu-result.json':c.original.cpu.CPU/'tests/result.json',
        'cpu-refinement.json':c.original.cpu.CPU/'refinement2/reuse.json',
        'cpu-reuse-rejection.json':c.original.cpu.CPU/'refinement/first-reuse-rejection.json',
        'postflight.json':c.BASE/'postflight.json',
        'numerical-reuse.json':c.original.PACKAGE/'numerical-reuse.json'}
    for name,path in aliases.items():h.copy_checked(h.identity(path),DEST/name)
    h.save(DEST/'manifest.json',dict(version=1,records=records,local_artifacts=local,
        aliases={n:h.identity(p) for n,p in aliases.items()},
        compressed=h.identity(DEST/'raw-records.tar.gz'),original_sse_failure_and_cpu_reuse_rejection_retained=True,
        scope='All selected raw record bytes preserved, including failures, wire, continuous samples and probes. ELF/object/bundles are local identities. No claim that original logs are redacted.'))
    (DEST/'README.md').write_text("""# SRV-02f evidence

The archive contains original HTTP bytes, logs, counters, raw CPU/native output,
resource samples and deliberate CUDA-status fault results. Faults are simulated
API return codes in a separate validation ELF, not physical OOM or GPU damage.
The production ELF has no environment-controlled fault injection.

`manifest.json` maps lossless compressed members to their original path and
SHA-256; ELF/object/source-bundle and private session snapshot identities remain
local. No blanket log redaction, complete allocation coverage, physical RAM
limit or sub-poll interference exclusion is claimed. The original FAILED SSE capture and CPU evidence
reuse rejection is preserved separately from the targeted final identity checks.
""")
    h.seal(DEST)
    print(json.dumps(verify(),ensure_ascii=False))


def verify(*,staged=False):
    h.verify_seal(DEST)
    manifest=h.read(DEST/'manifest.json')
    expected={row['member']:row for row in manifest['records']}
    seen=set()
    with tarfile.open(DEST/'raw-records.tar.gz','r:gz') as archive:
        for member in archive:
            h.require(member.isfile() and member.name in expected and member.name not in seen and
                      not Path(member.name).is_absolute() and '..' not in Path(member.name).parts,
                      'unexpected/duplicate tar member')
            record=expected[member.name];raw=archive.extractfile(member).read()
            h.require(len(raw)==record['bytes'] and hashlib.sha256(raw).hexdigest()==record['sha256'],'raw archive mismatch')
            h.check_identity(record);seen.add(member.name)
    h.require(seen==set(expected),'missing raw archive records')
    for row in manifest['local_artifacts']:h.check_identity(row)
    for name,row in manifest['aliases'].items():
        h.check_identity(row);h.require(h.sha(DEST/name)==row['sha256'],'alias changed')
    git_blobs=0
    if staged:
        for path in sorted(DEST.rglob('*')):
            if not path.is_file():continue
            relative=str(path.relative_to(ROOT))
            data=subprocess.check_output(['git','show',':'+relative],cwd=ROOT)
            h.require(hashlib.sha256(data).hexdigest()==h.sha(path),'staged Git bytes changed: '+relative)
            git_blobs+=1
    return dict(passed=True,raw_records=len(seen),local_artifacts=len(manifest['local_artifacts']),
        git_files=sum(p.is_file() for p in DEST.rglob('*')),git_bytes=sum(p.stat().st_size for p in DEST.rglob('*') if p.is_file()),
        staged_git_blobs_verified=git_blobs,compressed_sha256=h.sha(DEST/'raw-records.tar.gz'))


def custody():
    result=verify(staged=True)
    receipt=ROOT/'docs/evidence/serve-device-memory-custody-20261103.json'
    names=subprocess.check_output(['git','diff','--name-only','0af90eb8','--','*.md'],cwd=ROOT,text=True).splitlines()
    links,missing=0,[]
    for name in names:
        document=ROOT/name
        if not document.is_file():continue
        for target in re.findall(r'\]\(([^\s)]+)\)',document.read_text()):
            target=unquote(target.strip('<>')).split('#',1)[0]
            if not target or re.match(r'^[a-zA-Z][\w+.-]*:',target):continue
            links+=1;path=(document.parent/target).resolve()
            if path!=receipt and not path.exists():missing.append(dict(source=name,target=target))
    h.require(not missing,'changed document links missing: '+str(missing))
    pair=h.read(c.OUTPUT/'release/binding.json')['new']
    c.a.verify_hashes(ROOT/'build',pair)
    h.verify_pair(ROOT/'build/q4t',ROOT/'build/q4t.release.json',pair['q4t'])
    h.require(not list((ROOT/'build').glob('*recovery*.json')),'pending recovery')
    result.update(changed_document_links_checked=links,installed=pair,
        original_sse_failure_and_cpu_reuse_rejection_retained=True,no_additional_model_or_http_tests=True,
        preexisting_repository_link_gap='14 historical missing links remain separate backlog',
        goal_complete=False,checker=h.identity(Path(__file__)))
    h.save(receipt,result)
    print(json.dumps(result,ensure_ascii=False))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('create','verify','custody'))
    parser.add_argument('--staged',action='store_true')
    args=parser.parse_args()
    if args.action=='create':create()
    elif args.action=='custody':custody()
    else:print(json.dumps(verify(staged=args.staged)))


if __name__=='__main__':main()
