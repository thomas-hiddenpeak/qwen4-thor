"""Retain original A06 records, including failed driver and targeted repair."""
from pathlib import Path
import hashlib
import json
import shutil

ROOT=Path.cwd().resolve()
BASE=ROOT/'.q4t-work/serve-completions-20261108'
OUT=ROOT/'docs/evidence/serve-completions-20261108'
OUT.mkdir(parents=True,exist_ok=False)
selected={}
suffixes={'.json','.jsonl','.log','.txt','.wire','.stdout','.stderr','.py'}
for path in sorted(BASE.rglob('*')):
    if not path.is_file() or path.suffix not in suffixes:continue
    relative=path.relative_to(BASE)
    if relative.parts[0] in ('source','release-01','delivery-01','reference'):continue
    if any(part in ('private-cache','shared-cache','__pycache__') for part in relative.parts):continue
    if path.name in ('client-policy.json','gateway-policy.json','private-key.pem'):continue
    selected[str(relative)]=path
for name in ('reference/manifest.json','release-01/decision.json','release-01/receipt.json',
             'release-01/control-review.json','release-01/native-build.json',
             'release-01/source-snapshot.json','release-01/engine/runtime-source.json',
             'delivery-01/result.json','delivery-01/rollback/receipt.json',
             'delivery-01/help.json','delivery-01/status.json',
             'delivery-01/help.stdout','delivery-01/help.stderr',
             'delivery-01/status.stdout','delivery-01/status.stderr'):
    selected[name]=BASE/name
files={}
for relative,path in sorted(selected.items()):
    target=OUT/relative;target.parent.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(path,target)
    original=path.read_bytes();assert target.read_bytes()==original
    files[relative]=dict(sha256=hashlib.sha256(original).hexdigest(),bytes=len(original),source=str(path))
review=json.loads((BASE/'independent-review.json').read_text())
index=dict(version=1,source_commit=review['source_commit'],files=files,
    files_count=len(files),bytes=sum(v['bytes'] for v in files.values()),totals=review['totals'],
    excluded='ELF/model/cache binaries, source snapshot, private policies and credential fixtures, '
             'duplicate SDK source. Original identities and receipts retained.',
    failures_retained=['build-02/result.json','cpu-02/result.json',
        'business-01/launch-correction.json','business-01/result.json'],
    acceptance='Original business result remains FAILED for wrong final cancellation counter assertion. '
        'Separate reviewed raw-wire evidence plus targeted CPU correct the assertion; no runtime change or GPU repeat.')
(OUT/'index.json').write_text(json.dumps(index,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(dict(files=len(files),bytes=index['bytes'])))
