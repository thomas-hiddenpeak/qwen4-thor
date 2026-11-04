"""Verify original/archive/Git bytes and changed-document links without inference."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote

ROOT = next(p for p in Path(__file__).resolve().parents if (p/'AGENTS.md').exists())
sys.path.insert(0, str(ROOT))
from tools.serve_observation_capacity import stage
h = stage.h
archive = ROOT/'docs/evidence/serve-observation-capacity-20261103'
receipt = ROOT/'docs/evidence/serve-observation-capacity-custody-20261103.json'
h.require(not receipt.exists(), 'preserve receipt')
h.verify_seal(archive)
sources = h.read(archive/'source-map.json');local = h.read(archive/'local-identities.json')
for name, record in sources.items():
    h.check_identity(record)
    h.require(h.sha(archive/name) == record['sha256'], 'raw bytes differ')
for record in local.values():h.check_identity(record)
tracked = subprocess.check_output(['git','ls-files','-s','-z','--',str(archive)], cwd=ROOT)
index = {}
for row in tracked.split(b'\0'):
    if not row:continue
    fields, name = row.split(b'\t',1);_, oid, number = fields.decode().split()
    h.require(number=='0','unmerged archive');index[name.decode()] = oid
files = sorted(p for p in archive.rglob('*') if p.is_file())
h.require(set(index)=={str(p.relative_to(ROOT)) for p in files}, 'archive missing from Git')
raw = subprocess.check_output(['git','cat-file','--batch'], input=('\n'.join(
    index[str(p.relative_to(ROOT))] for p in files)+'\n').encode(), cwd=ROOT)
offset = 0;verified = []
for path in files:
    end = raw.index(b'\n',offset);_, kind, size = raw[offset:end].decode().split();size=int(size)
    start=end+1;digest=hashlib.sha256(raw[start:start+size]).hexdigest()
    h.require(kind=='blob' and size==path.stat().st_size and digest==h.sha(path), 'Git blob differs')
    verified.append(dict(path=str(path.relative_to(ROOT)), sha256=digest, bytes=size));offset=start+size+1
h.require(offset==len(raw),'extra Git output')
names = subprocess.check_output(['git','diff','--name-only','5174ab76','--','docs/*.md'], cwd=ROOT, text=True).splitlines()
documents = sorted({ROOT/n for n in names if (ROOT/n).is_file()}|{archive/'README.md'})
missing=[];links=0
for document in documents:
    for target in re.findall(r'\]\(([^\s)]+)\)', document.read_text()):
        target=unquote(target.strip('<>')).split('#',1)[0]
        if not target or re.match(r'^[a-zA-Z][\w+.-]*:',target):continue
        links+=1
        path=(document.parent/target).resolve()
        # This exact receipt is created below only after all other checks pass.
        if path != receipt and not path.exists():missing.append(dict(source=str(document),target=target))
h.require(not missing,'changed-document missing links: '+str(missing))
stage.rollback_plan()
final=h.read(stage.OUTPUT/'finalized.json')
stage.a.verify_hashes(ROOT/'build',final['installed'])
h.save(receipt,dict(stage_custody_passed=True,raw_records_verified=len(sources),
    local_artifacts_verified=len(local),staged_evidence_files=len(files),byte_verified=verified,
    changed_document_links_checked=links,existing_repository_link_gap='14 preexisting historical missing links; not modified by this stage',
    raw_bytes_preserved=True,models_retested=False,installed=final['installed'],
    rollback_pair_verified_without_mutation=True,source_commit=final['source_commit']))
h.require(receipt.is_file(),'receipt was not created')
print(json.dumps(dict(files=len(files),raw=len(sources),local=len(local),links=links,passed=True)))
