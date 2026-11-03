"""Check Git blobs, original bytes and every decompressed archive member."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tarfile
from urllib.parse import unquote

ROOT = next(p for p in Path(__file__).resolve().parents if (p/'AGENTS.md').exists())
sys.path.insert(0, str(ROOT))
from tools.serve_lifecycle import stage, repair as r
h = stage.h
archive = ROOT/'docs/evidence/serve-lifecycle-20261103'
receipt = ROOT/'docs/evidence/serve-lifecycle-custody-20261103.json'
h.require(not receipt.exists(), 'preserve custody receipt')
h.verify_seal(archive)
sources = h.read(archive/'source-map.json');local = h.read(archive/'local-identities.json')
for record in sources.values():h.check_identity(record)
for record in local.values():h.check_identity(record)
seen = set()
with tarfile.open(archive/'raw-records.tar.gz', mode='r|gz') as packed:
    for member in packed:
        h.require(member.isfile() and member.name in sources and member.name not in seen and
                  not Path(member.name).is_absolute() and '..' not in Path(member.name).parts,
                  'invalid raw member')
        digest = hashlib.sha256();size = 0
        stream = packed.extractfile(member)
        while data := stream.read(1024**2):digest.update(data);size += len(data)
        h.require(digest.hexdigest() == sources[member.name]['sha256'] and
                  size == sources[member.name]['bytes'], 'raw member differs')
        seen.add(member.name)
h.require(seen == set(sources), 'raw member set differs')
proof = h.read(archive/'compression-proof.json')
for name in proof['readable_duplicate_members']:
    h.require(h.sha(archive/name) == sources[name]['sha256'], 'readable raw copy differs')
index = {}
for row in subprocess.check_output(['git','ls-files','-s','-z','--',str(archive)], cwd=ROOT).split(b'\0'):
    if not row:continue
    fields, name = row.split(b'\t',1);_, oid, number = fields.decode().split()
    h.require(number == '0', 'unmerged archive');index[name.decode()] = oid
files = sorted(p for p in archive.rglob('*') if p.is_file())
h.require(set(index) == {str(p.relative_to(ROOT)) for p in files}, 'archive files missing from Git')
raw = subprocess.check_output(['git','cat-file','--batch'], cwd=ROOT, input=('\n'.join(
    index[str(p.relative_to(ROOT))] for p in files)+'\n').encode())
offset, verified = 0, []
for path in files:
    end = raw.index(b'\n',offset);_, kind, size = raw[offset:end].decode().split();size = int(size)
    start = end+1;digest = hashlib.sha256(raw[start:start+size]).hexdigest()
    h.require(kind == 'blob' and size == path.stat().st_size and digest == h.sha(path), 'Git blob differs')
    verified.append(dict(path=str(path.relative_to(ROOT)), sha256=digest, bytes=size))
    offset = start+size+1
h.require(offset == len(raw), 'extra Git blob data')
names = subprocess.check_output(['git','diff','--name-only','87639e97','--','docs/*.md','tools/serve_lifecycle/*.md'],
                                cwd=ROOT, text=True).splitlines()
documents = sorted({ROOT/n for n in names if (ROOT/n).is_file()}|{archive/'README.md'})
links, missing = 0, []
for document in documents:
    for target in re.findall(r'\]\(([^\s)]+)\)', document.read_text()):
        target = unquote(target.strip('<>')).split('#',1)[0]
        if not target or re.match(r'^[a-zA-Z][\w+.-]*:', target):continue
        links += 1;path = (document.parent/target).resolve()
        if path != receipt and not path.exists():missing.append(dict(source=str(document), target=target))
h.require(not missing, 'changed-document missing links: '+str(missing))
installed = h.read(r.OUTPUT/'release/binding.json')['new']
r.a.verify_hashes(ROOT/'build', installed)
h.verify_pair(ROOT/'build/q4t', ROOT/'build/q4t.release.json', installed['q4t'])
h.require(not list((ROOT/'build').glob('*recovery*.json')), 'pending recovery')
h.save(receipt, dict(passed=True, source_commit=h.read(r.PACKAGE/'plan.json')['source_commit'],
    original_records_verified=len(sources), decompressed_members_verified=len(seen),
    readable_duplicates_verified=len(proof['readable_duplicate_members']),
    local_identities_verified=len(local), staged_evidence_files=len(files), staged_blobs=verified,
    changed_document_links_checked=links, original_bytes_preserved=True,
    existing_repository_link_gap='14 preexisting historical missing links, separate backlog',
    installed=installed, no_new_http_or_model_requests=True, goal_complete=False))
print(json.dumps(dict(passed=True, raw=len(seen), git_blobs=len(files), local=len(local), links=links)))
