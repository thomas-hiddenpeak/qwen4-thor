"""Seal A05 original records without model/state binaries or private policies."""
from pathlib import Path
import hashlib
import json
import shutil

ROOT=Path.cwd().resolve()
BASE=ROOT/'.q4t-work/serve-schema-v2-20261108'
OUT=ROOT/'docs/evidence/serve-schema-v2-20261108'
OUT.mkdir(parents=True,exist_ok=False)
index={}
suffixes={'.json','.jsonl','.log','.txt','.wire','.stdout','.stderr','.py'}
for path in sorted(BASE.rglob('*')):
    if not path.is_file() or path.suffix not in suffixes:continue
    relative=path.relative_to(BASE)
    if relative.parts[0] in ('source','public-host','release-01','delivery-01'):continue
    if any(part.startswith('fixtures-') or part in
           ('session','prefix','session-cache','prefix-cache','__pycache__')
           for part in relative.parts):continue
    if relative.parts[0] in ('reference','format-corpus'):continue
    if path.name in ('client-policy.json','gateway-policy.json','private-key.pem'):continue
    index[str(relative)]=path
for name in ('release-01/decision.json','release-01/receipt.json',
             'release-01/unchanged-control.json','release-01/native-build.json',
             'release-01/engine/runtime-source.json','delivery-01/result.json',
             'delivery-01/rollback/receipt.json','delivery-01/help.json',
             'delivery-01/status.json','delivery-01/help.stdout','delivery-01/help.stderr',
             'delivery-01/status.stdout','delivery-01/status.stderr'):
    index[name]=BASE/name
files={}
for relative,path in sorted(index.items()):
    target=OUT/relative
    target.parent.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(path,target)
    original=path.read_bytes()
    assert target.read_bytes()==original
    files[relative]=dict(sha256=hashlib.sha256(original).hexdigest(),
                         bytes=len(original),source=str(path))
review=json.loads((BASE/'independent-review.json').read_text())
result=dict(version=1,source_commit=review['source_commit'],files=files,
    files_count=len(files),bytes=sum(v['bytes'] for v in files.values()),
    excluded='ELF/model/cache binaries, native credential fixtures/private policy files, '
             'duplicate reference/license corpus. Release metadata and rollback identities are '
             'retained in immutable local packages, with their hashes in receipts.',
    zero_model_failures=['business-01/launch-correction.json','prompt-cache-01/result.json'],
    lifecycle='All seven model-bearing attempts cleaned owned processes and ports; '
              'four request failures and startup/preparation/CPU failures retained.',
    totals=review['totals'])
(OUT/'index.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(dict(files=len(files),bytes=result['bytes'])))
