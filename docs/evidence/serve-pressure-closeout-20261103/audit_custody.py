"""Verify archived bytes, originals and Git blobs without another model run."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote

ROOT = next(p for p in Path(__file__).resolve().parents if (p/'AGENTS.md').exists())
sys.path.insert(0, str(ROOT))
from tools.serve_pressure import stage
h = stage.h
archive = ROOT/'docs/evidence/serve-pressure-20261103'
h.verify_seal(archive)
sources, local = h.read(archive/'source-map.json'), h.read(archive/'local-identities.json')
for name, record in sources.items():
    h.check_identity(record)
    h.require(h.sha(archive/name)==record['sha256'], 'archived original differs: '+name)
for record in local.values(): h.check_identity(record)
tracked = subprocess.check_output(['git','ls-files','-s','-z','--',str(archive)],cwd=ROOT)
index = {}
for row in tracked.split(b'\0'):
    if not row: continue
    fields, name = row.split(b'\t',1)
    mode, oid, number = fields.decode().split()
    h.require(number=='0', 'unmerged archive')
    index[name.decode()] = oid
files = sorted(p for p in archive.rglob('*') if p.is_file())
h.require(set(index)=={str(p.relative_to(ROOT)) for p in files}, 'archive files missing from Git')
oids = [index[str(path.relative_to(ROOT))] for path in files]
result = subprocess.run(['git','cat-file','--batch'], input=('\n'.join(oids)+'\n').encode(),
    cwd=ROOT,capture_output=True,check=True)
raw, offset, verified = result.stdout, 0, []
for path in files:
    end=raw.index(b'\n',offset)
    oid, kind, size = raw[offset:end].decode().split()
    h.require(kind=='blob','not a blob')
    size=int(size)
    start=end+1
    digest=hashlib.sha256(raw[start:start+size]).hexdigest()
    h.require(digest==h.sha(path) and size==path.stat().st_size,'Git bytes differ: '+str(path))
    verified.append(dict(path=str(path.relative_to(ROOT)),sha256=digest,bytes=size))
    offset=start+size+1
h.require(offset==len(raw),'extra Git batch output')
missing, link_count = [], 0
docs=list((ROOT/'docs').glob('*.md'))+[ROOT/'README.md',ROOT/'AGENTS.md',archive/'README.md']
for document in docs:
    for target in re.findall(r'\]\(([^\s)]+)\)',document.read_text()):
        target=unquote(target.strip('<>')).split('#',1)[0]
        if not target or re.match(r'^[a-zA-Z][\w+.-]*:',target): continue
        link_count+=1
        if not (document.parent/target).exists(): missing.append(dict(source=str(document),target=target))
baseline_docs = {}
for item in missing:
    path = Path(item['source'])
    relative = str(path.relative_to(ROOT))
    original = subprocess.check_output(['git','show','4fd5ec7c:'+relative],cwd=ROOT)
    h.require(hashlib.sha256(original).hexdigest()==h.sha(path),
              'current stage introduced or edited a broken-link document: '+relative)
    baseline_docs[relative] = h.identity(path)
h.save(stage.BASE/'historical-link-audit.json',dict(
    full_repository_links_passed=not missing, scanned_links=link_count,
    missing_links=missing, baseline_commit='4fd5ec7c', unchanged_documents=baseline_docs,
    original_checker= h.identity(stage.BASE/'custody.py'),
    original_checker_outcome='Failed on these existing links after byte checks; original stderr in tool transcript',
    resolved=False, model_acceptance_affected=False))
stage.installed()
stage.previous.rollback_plan()
receipt=dict(stage_custody_passed=True,full_repository_links_passed=not missing,stage_source_commit=h.read(archive/'package/plan.json')['source_commit'],
    staged_evidence_files=len(files),byte_verified=verified,raw_records_verified=len(sources),
    local_artifacts_verified=len(local),local_document_links_checked=link_count,
    missing_links=missing,preexisting_broken_documents=baseline_docs,raw_bytes_preserved=True,models_retested=False,
    installed_pair_unchanged=True,rollback_pair_verified_without_mutation=True)
h.save(ROOT/'docs/evidence/serve-pressure-custody-20261103.json',receipt)
print(json.dumps({k:v for k,v in receipt.items() if k!='byte_verified'},indent=2))
