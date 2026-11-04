"""Check archive, originals, staged Git bytes and changed-document links."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote

ROOT=next(p for p in Path(__file__).resolve().parents if (p/'AGENTS.md').exists())
sys.path.insert(0,str(ROOT))
from tools.serve_ops import stage
h=stage.h;archive=ROOT/'docs/evidence/serve-ops-20261103'
h.verify_seal(archive)
sources=h.read(archive/'source-map.json');local=h.read(archive/'local-identities.json')
for name,record in sources.items():
    h.check_identity(record);h.require(h.sha(archive/name)==record['sha256'],'raw bytes differ')
for record in local.values():h.check_identity(record)
tracked=subprocess.check_output(['git','ls-files','-s','-z','--',str(archive)],cwd=ROOT)
index={}
for row in tracked.split(b'\0'):
    if not row:continue
    fields,name=row.split(b'\t',1);mode,oid,number=fields.decode().split()
    h.require(number=='0','unmerged archive');index[name.decode()]=oid
files=sorted(p for p in archive.rglob('*') if p.is_file())
h.require(set(index)=={str(p.relative_to(ROOT)) for p in files},'archive files missing from Git')
raw=subprocess.check_output(['git','cat-file','--batch'],input=('\n'.join(
    index[str(p.relative_to(ROOT))] for p in files)+'\n').encode(),cwd=ROOT)
offset=0;verified=[]
for path in files:
    end=raw.index(b'\n',offset);oid,kind,size=raw[offset:end].decode().split();size=int(size)
    start=end+1;digest=hashlib.sha256(raw[start:start+size]).hexdigest()
    h.require(kind=='blob' and size==path.stat().st_size and digest==h.sha(path),'Git blob differs')
    verified.append(dict(path=str(path.relative_to(ROOT)),sha256=digest,bytes=size));offset=start+size+1
h.require(offset==len(raw),'extra Git batch output')
names=subprocess.check_output(['git','diff','--name-only','e2f8f83b','--','docs/*.md'],cwd=ROOT,text=True).splitlines()
documents=[ROOT/n for n in names if (ROOT/n).is_file()]+[archive/'README.md']
missing=[];links=0
for document in documents:
    for target in re.findall(r'\]\(([^\s)]+)\)',document.read_text()):
        target=unquote(target.strip('<>')).split('#',1)[0]
        if not target or re.match(r'^[a-zA-Z][\w+.-]*:',target):continue
        links+=1
        if not (document.parent/target).exists():missing.append(dict(source=str(document),target=target))
h.require(not missing,'changed document link missing: '+str(missing))
stage.previous.installed();stage.previous.previous.rollback_plan()
receipt=dict(stage_custody_passed=True,raw_records_verified=len(sources),local_artifacts_verified=len(local),
    staged_evidence_files=len(files),byte_verified=verified,changed_document_links_checked=links,
    existing_repository_link_gap='14 preexisting missing links remain; see serve-pressure-custody-20261103.json',
    raw_bytes_preserved=True,models_retested=False,default_pair_unchanged=True,
    rollback_pair_verified_without_mutation=True,source_commit=h.read(archive/'package/plan.json')['source_commit'])
h.save(ROOT/'docs/evidence/serve-ops-custody-20261103.json',receipt)
print(json.dumps({k:v for k,v in receipt.items() if k!='byte_verified'},indent=2))
