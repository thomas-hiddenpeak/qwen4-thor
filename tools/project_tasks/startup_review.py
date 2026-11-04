"""Seal a directed public task contract review before updating registries."""
import difflib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
from tools.serve_service import package as pkg
from tools.project_tasks import engine,protocol,store,test_tasks

BASE=ROOT/'.q4t-work/serve-task-startup-protocol-20261104'
DEST=ROOT/'docs/evidence/serve-task-startup-review-20261104'
OLD=ROOT/'.q4t-work/serve-startup-listeners-20261104/package/previous'
CURRENT=ROOT/'.q4t-work/serve-supervisor-deadlines-20261104/backend-package'
BEFORE='ddfba8c002d8fb899b4c00d42612b0a26669036a1acc65e8292a10c0a9b69342'
AFTER='15d7ccb103258ea5ec8ca3cfa66c9bfe1245eadb7d38ad6ee149ad97075ec30e'
TEMPLATE='c3cf9e34abf4f9e36c2d72165aa9c132d3e2a725b6c2586aaa3a8af9d7a81041'
HISTORY='docs/evidence/serve-task-deadline-20261104/records.tar.gz'
HISTORY_SHA='9b127e44188fe7fc8eb38bfbddb3df30232f03750925e789b30282b94e9a2bb7'
CHANGED=('include/q4t/server/chat_server.h','include/q4t/server/http_listener.h',
         'src/server/chat_http.cpp','src/server/chat_server.cpp')
sha=lambda raw:hashlib.sha256(raw).hexdigest()


def build():
    os.umask(0o077);BASE.mkdir(mode=0o700,exist_ok=True)
    out=BASE/'review';out.mkdir(mode=0o700)
    data={}
    def add(name,raw):
        pkg.require(name not in data,'duplicate review record');data[name]=raw
    binding=dict(test_tasks.BINDING,model='qwen3.8-flash-next',
        protocol_sha256=BEFORE,template_sha256=TEMPLATE)
    try:protocol.reviewed(binding,dict(binding,protocol_sha256=AFTER))
    except store.ContractError as error:
        pkg.require(error.code=='protocol_unsupported','unexpected original refusal')
        add('baseline-refusal.json',store.encode(dict(code=error.code,message=str(error),models=0,http=0)))
    else:raise RuntimeError('review must precede registry authorization')
    metadata={};sources={};raw_source={};caps={}
    for label,directory,expected in (('old',OLD,BEFORE),('new',CURRENT,AFTER)):
        sidecar=directory/'q4t.release.json';value=pkg.read(sidecar)
        pkg.require(pkg.sha(directory/'q4t')==value['binary_sha256'] and
            value['chat_protocol']['protocol_sha256']==expected and
            value['chat_protocol']['template']['reference_sha256']==TEMPLATE,'release identity')
        metadata[label]=value;add(label+'/q4t.release.json',sidecar.read_bytes())
        manifest=Path(value['tested_source_manifest'])
        pkg.require(pkg.sha(manifest)==value['tested_source_manifest_sha256'],'runtime manifest')
        sources[label]=pkg.read(manifest);add(label+'/runtime-source.json',manifest.read_bytes())
        for name,digest in sources[label].items():
            raw=subprocess.check_output(['git','show',value['source_commit']+':'+name],cwd=ROOT)
            pkg.require(sha(raw)==digest,'accepted Git bytes: '+name)
            if name in CHANGED:
                raw_source[label,name]=raw
                add(label+'/source/'+name,raw)
        for name in ('config','message','preflight','reasoning','response','stop','thinking'):
            path=directory/'declarations/schemas'/(name+'.json')
            pkg.require(pkg.sha(path)==value['chat_protocol'][name+'_schema_sha256'],'schema binding')
            add(label+'/schemas/'+name+'.json',path.read_bytes())
        path=directory/'declarations/capabilities.json';caps[label]=pkg.read(path)
        add(label+'/capabilities.json',path.read_bytes())
    delta=sorted(n for n in sources['old'].keys()|sources['new'].keys() if
                 sources['old'].get(n)!=sources['new'].get(n))
    pkg.require(delta==list(CHANGED) and len(sources['old'])==222 and len(sources['new'])==223,
                'unreviewed runtime delta')
    for name in CHANGED:
        before=raw_source.get(('old',name),b'').decode();after=raw_source['new',name].decode()
        add('reviewed-diff/'+name+'.diff',''.join(difflib.unified_diff(before.splitlines(True),
            after.splitlines(True),fromfile='old/'+name,tofile='new/'+name)).encode())
    marker=b'  detail::ServeFaultPoint("connection.before_publish");'
    pkg.require(raw_source['old','src/server/chat_http.cpp'].split(marker,1)[1]==
                raw_source['new','src/server/chat_http.cpp'].split(marker,1)[1],'HTTP dispatch changed')
    for name in ('config','message','preflight','reasoning','response','stop','thinking'):
        pkg.require(data['old/schemas/'+name+'.json']==data['new/schemas/'+name+'.json'],'schema changed')
    fields=[n for n in caps['old'].keys()|caps['new'].keys() if caps['old'].get(n)!=caps['new'].get(n)]
    pkg.require(fields==['protocol_source_sha256'],'public capability change')
    pkg.require(metadata['old']['chat_protocol']['template']==metadata['new']['chat_protocol']['template'],
                'template changed')
    pkg.require(pkg.sha(ROOT/HISTORY)==HISTORY_SHA,'accepted app archive changed')
    runtime=engine.sources()
    with tarfile.open(ROOT/HISTORY) as archive:
        for name,digest in runtime.items():
            matches=[m for m in archive.getmembers() if m.isfile() and
                m.name.endswith('/source/'+name) and sha(archive.extractfile(m).read())==digest]
            pkg.require(matches,'accepted application bytes missing: '+name)
            add('historical-source/'+name,archive.extractfile(matches[0]).read())
    add('historical-runtime.json',store.encode(runtime))
    # Both accepted sets predate this registry. Preserve their manifests and
    # actual public wires; never reuse the interrupted 12l run as a whole PASS.
    selected=[]
    for name,prefix in (('serve-startup-listeners-20261104','records/execution/business/'),
                        ('serve-supervisor-deadlines-20261104','execution/business/')):
        directory=ROOT/'docs/evidence'/name;manifest=pkg.read(directory/'manifest.json')
        pkg.require(pkg.sha(directory/'records.tar.gz')==manifest['archive_sha256'],'accepted archive')
        for item in ('result.json','manifest.json'):add('accepted/'+name+'/'+item,(directory/item).read_bytes())
        with tarfile.open(directory/'records.tar.gz') as archive:
            for member in archive.getmembers():
                if member.isfile() and member.name.startswith(prefix):
                    raw=archive.extractfile(member).read()
                    expected=next(r['sha256'] for r in manifest['records'] if r.get('artifact')==member.name)
                    pkg.require(sha(raw)==expected,'accepted public record')
                    add('accepted/'+name+'/'+member.name,raw);selected.append(member.name)
    add('review-source.py',Path(__file__).read_bytes())
    proof=dict(from_protocol=BEFORE,to_protocol=AFTER,template_sha256=TEMPLATE,
        old_runtime_count=222,new_runtime_count=223,unchanged_runtime_count=219,changed=delta,
        identical_schemas=7,http_dispatch_after_listener_publication_identical=True,
        public_capability_changed=fields,historical_runtime_files=len(runtime),
        historical_archive=HISTORY,historical_archive_sha256=HISTORY_SHA,
        old_binary_sha256=metadata['old']['binary_sha256'],new_binary_sha256=metadata['new']['binary_sha256'],
        reused_public_records=len(selected),new_models=0,new_http_requests=0,
        scope='Directed public readonly project-task history compatibility. Startup TCP may queue before HTTP readiness; no reverse/transitive/address/model approval.')
    add('review.json',store.encode(proof))
    for name,raw in data.items():
        path=out/name;path.parent.mkdir(parents=True,exist_ok=True)
        with path.open('xb') as stream:stream.write(raw)
    DEST.mkdir()
    with tarfile.open(DEST/'records.tar.gz','w:gz') as archive:
        for name,raw in sorted(data.items()):
            item=tarfile.TarInfo(name);item.mode=0o400;item.size=len(raw)
            archive.addfile(item,io.BytesIO(raw))
    pkg.save(DEST/'manifest.json',dict(records={n:sha(raw) for n,raw in sorted(data.items())},
        archive_sha256=pkg.sha(DEST/'records.tar.gz'),review=proof))
    print(json.dumps(dict(records=len(data),archive_sha256=pkg.sha(DEST/'records.tar.gz'))))


if __name__=='__main__':build()
