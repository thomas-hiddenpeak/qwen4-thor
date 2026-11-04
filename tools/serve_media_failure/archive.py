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

from tools.serve_media_failure import stage as c

h,ROOT=c.h,c.ROOT
DEST=ROOT/'docs/evidence/serve-media-failure-20261103'


def raw_files():
    groups=[('stage',c.BASE,('cpu','cpu-before-format','cpu-before-decode-oom',
        'fixtures','package','execution','rollback-bundle','release-history.json',
        'inventory-review.json','numerical-review.json','binding-review.json',
        'inventory-review-before-decode-oom.json','numerical-review-before-decode-oom.json',
        'binding-review-before-decode-oom.json','preflight-gpu.json',
        'environment-gate.json','postflight.json','postflight-gpu.json','analysis.json'))]
    for label,base,names in groups:
        for name in names:
            root=base/name
            for path in sorted(root.rglob('*')) if root.is_dir() else [root]:
                if path.is_file():
                    h.require(not path.is_symlink(),'symlink evidence disallowed')
                    yield label+'/'+str(path.relative_to(base)),path


def create():
    for path in (c.PACKAGE,c.OUTPUT,c.BASE/'rollback-bundle'):
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
        'plan.json':c.PACKAGE/'plan.json',
        'result.json':c.OUTPUT/'business/result.json',
        'guard.json':c.OUTPUT/'lease/exit.json',
        'finalized.json':c.OUTPUT/'finalized.json',
        'installed-health.json':c.OUTPUT/'health/result.json',
        'native-result.json':c.OUTPUT/'native/result.json',
        'native-vision.json':c.OUTPUT/'native-vision/result.json',
        'cpu-result.json':c.cpu.CPU/'tests/result.json',
        'original-cpu-result.json':c.BASE/'cpu-before-decode-oom/tests/result.json',
        'postflight.json':c.BASE/'postflight.json',
        'analysis.json':c.BASE/'analysis.json',
        'numerical-reuse.json':c.PACKAGE/'numerical-reuse.json'}
    for name,path in aliases.items():h.copy_checked(h.identity(path),DEST/name)
    h.save(DEST/'manifest.json',dict(version=1,records=records,local_artifacts=local,
        aliases={n:h.identity(p) for n,p in aliases.items()},
        compressed=h.identity(DEST/'raw-records.tar.gz'),original_generated_document_cpu_failure_retained=True,
        scope='All selected raw record bytes preserved, including failures, wire, continuous samples and probes. ELF/object/bundles are local identities. No claim that original logs are redacted.'))
    (DEST/'README.md').write_text("""# SRV-02h evidence

Lossless actual HTTP wire, logs/counters, continuous samples, CPU/native
contracts and CUDA-status fault records. API and host allocation failures
are deliberately simulated; no physical OOM/device damage is claimed.
The production q4t executable has no environment-controlled fault hooks.

The original generated-document CPU failure is retained. Later C decoder
allocation classification and final rebuilt identities are separate records.
The archive maps each raw member to its path/SHA; ELF/object/source bundles
remain local identities. Old pre-format build identities were superseded,
not accepted test artifacts. Media quality covers only a fixed red image and
two identical frames, not broad multimodal quality. Source decode/geometry
bounds and physical RAM protection are still pending. No blanket log
redaction, long-soak or performance conclusion is implied.
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
    receipt=ROOT/'docs/evidence/serve-media-failure-custody-20261103.json'
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
        original_generated_document_cpu_failure_retained=True,no_additional_model_or_http_tests=True,
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
