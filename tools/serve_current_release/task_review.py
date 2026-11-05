"""Seal exact historical readers and a directed current-release task contract."""
import ast
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
from tools.project_tasks import engine,protocol,store,test_tasks
from tools.serve_service import package as pkg

BASE=ROOT/'.q4t-work/serve-task-current-20261105'
DEST=ROOT/'docs/evidence/serve-task-current-review-20261105'
OLD=ROOT/'.q4t-work/serve-supervisor-deadlines-20261104/backend-package'
CURRENT=ROOT/'.q4t-work/serve-current-managed-20261105/backend-package'
BEFORE='15d7ccb103258ea5ec8ca3cfa66c9bfe1245eadb7d38ad6ee149ad97075ec30e'
AFTER='cc321239ced843d6d618d07b72ed81ed65958b2fdc29261fc2b042d9118c40d5'
TEMPLATE='c3cf9e34abf4f9e36c2d72165aa9c132d3e2a725b6c2586aaa3a8af9d7a81041'
HISTORY='docs/evidence/serve-task-startup-protocol-20261104/records.tar.gz'
HISTORY_SHA='c5aa379ab4e8b1a211a63bdd13a638bbacd0dc436c956ebf752c19a5659d413b'
CLIENT='docs/evidence/serve-client-transport-20261105/records.tar.gz'
CLIENT_SHA='1d4cd7f4f47848e3a35b7694085f963834bfbd18450348070eced43abc3885a5'
CHANGED=tuple(sorted(('CMakeLists.txt','include/q4t/model/full_attention.h',
    'include/q4t/mtp/mtp.h','include/q4t/server/request_control.h',
    'include/q4t/server/request_diagnostics.h','src/model/full_attention.cu',
    'src/mtp/mtp.cu','src/server/chat_generation.cpp','src/server/chat_preflight.cpp')))
sha=lambda raw:hashlib.sha256(raw).hexdigest()


def build():
    os.umask(0o077);BASE.mkdir(mode=0o700,exist_ok=True)
    out=BASE/'review';out.mkdir(mode=0o700)
    data={}
    def add(name,raw):
        pkg.require(name not in data,'duplicate review member');data[name]=raw
    binding=dict(test_tasks.BINDING,protocol_sha256=BEFORE,
        model='qwen3.8-flash-next',template_sha256=TEMPLATE)
    try:protocol.reviewed(binding,dict(binding,protocol_sha256=AFTER))
    except store.ContractError as error:
        pkg.require(error.code=='protocol_unsupported','original refusal')
        add('baseline-refusal.json',store.encode(dict(code=error.code,models=0,http=0)))
    else:raise RuntimeError('review must precede registry authorization')
    sources={};metadata={};caps={};raw_source={}
    for label,directory,expected in (('old',OLD,BEFORE),('new',CURRENT,AFTER)):
        value=pkg.read(directory/'q4t.release.json');metadata[label]=value
        pkg.require(pkg.sha(directory/'q4t')==value['binary_sha256'] and
            value['chat_protocol']['protocol_sha256']==expected and
            value['chat_protocol']['template']['reference_sha256']==TEMPLATE,'release identity')
        add(label+'/q4t.release.json',(directory/'q4t.release.json').read_bytes())
        manifest=Path(value['tested_source_manifest'])
        pkg.require(pkg.sha(manifest)==value['tested_source_manifest_sha256'],'accepted source manifest')
        sources[label]=pkg.read(manifest);add(label+'/runtime-source.json',manifest.read_bytes())
        for name,digest in sources[label].items():
            raw=subprocess.check_output(['git','show',value['source_commit']+':'+name],cwd=ROOT)
            pkg.require(sha(raw)==digest,'accepted Git source: '+name)
            if name in CHANGED:
                raw_source[label,name]=raw;add(label+'/source/'+name,raw)
        for name in ('config','message','preflight','reasoning','response','stop','thinking'):
            path=directory/'declarations/schemas'/(name+'.json')
            pkg.require(pkg.sha(path)==value['chat_protocol'][name+'_schema_sha256'],'schema identity')
            add(label+'/schemas/'+name+'.json',path.read_bytes())
        path=directory/'declarations/capabilities.json';caps[label]=pkg.read(path)
        add(label+'/capabilities.json',path.read_bytes())
    delta=sorted(n for n in sources['old'].keys()|sources['new'].keys()
                 if sources['old'].get(n)!=sources['new'].get(n))
    pkg.require(delta==list(CHANGED) and len(sources['old'])==len(sources['new'])==223,
                'unreviewed runtime delta')
    for name in CHANGED:
        add('reviewed-diff/'+name+'.diff',''.join(difflib.unified_diff(
            raw_source['old',name].decode().splitlines(True),
            raw_source['new',name].decode().splitlines(True),fromfile='old/'+name,tofile='new/'+name)).encode())
    for name in ('config','message','preflight','reasoning','response','stop','thinking'):
        pkg.require(data['old/schemas/'+name+'.json']==data['new/schemas/'+name+'.json'],'schema changed')
    fields=sorted(n for n in caps['old'].keys()|caps['new'].keys() if caps['old'].get(n)!=caps['new'].get(n))
    pkg.require(fields==['protocol_source_sha256'] and
        metadata['old']['chat_protocol']['template']==metadata['new']['chat_protocol']['template'],
        'public/template contract changed')
    histories=[]
    for label,path,digest,plan_name,map_name,names in (
        ('srv11k-f4705009',HISTORY,HISTORY_SHA,'plan.json','source_files',
            [n for n in engine.RUNTIME_FILES if n!='tools/examples/deadline_http.py']),
        ('srv08b-8f056762',CLIENT,CLIENT_SHA,'finish-plan-03.json','sources',engine.RUNTIME_FILES)):
        pkg.require(pkg.sha(ROOT/path)==digest,'historical archive identity')
        with tarfile.open(ROOT/path) as tar:
            saved=json.load(tar.extractfile(plan_name))[map_name]
            runtime={n:saved[n] for n in names}
            for name,checksum in runtime.items():
                matches=[m for m in tar.getmembers() if m.isfile() and
                    m.name.endswith('/source/'+name) and sha(tar.extractfile(m).read())==checksum]
                pkg.require(matches,'historical source unavailable: '+name)
                add('historical/'+label+'/source/'+name,tar.extractfile(matches[0]).read())
        histories.append(dict(release=label,evidence=path,archive_sha256=digest,
            budget_profile='q4t-project-budget-v1',runtime_sources=runtime))
    old_runtime,new_runtime=(row['runtime_sources'] for row in histories)
    changed_app=sorted(n for n in old_runtime.keys()|new_runtime.keys() if old_runtime.get(n)!=new_runtime.get(n))
    pkg.require(changed_app==['tools/examples/chat_client.py','tools/examples/deadline_http.py',
        'tools/project_tasks/engine.py'],'unreviewed application delta')
    def definitions(raw):
        return {n.name:ast.dump(n,include_attributes=False) for n in ast.parse(raw).body
            if isinstance(n,(ast.FunctionDef,ast.ClassDef)) and n.name!='ChatClient'}
    prefix=lambda label,name:data['historical/'+label+'/source/'+name]
    pkg.require(definitions(prefix(histories[0]['release'],'tools/examples/chat_client.py'))==
        definitions(prefix(histories[1]['release'],'tools/examples/chat_client.py')),
        'shared response/tool semantics changed')
    for label in ('serve-client-transport-20261105','serve-current-managed-20261105'):
        directory=ROOT/'docs/evidence'/label
        add('accepted/'+label+'/result.json',(directory/'result.json').read_bytes())
        add('accepted/'+label+'/manifest.json',(directory/'manifest.json').read_bytes())
    for name in ('SERVE_PREPARE_LOCK_RESULT_2026-11-04.md','MTP_PREFILL_COMPLETION_RESULT_2026-11-05.md'):
        add('accepted/'+name,(ROOT/'docs'/name).read_bytes())
    add('historical-runtimes.json',store.encode(histories))
    proof=dict(from_protocol=BEFORE,to_protocol=AFTER,template_sha256=TEMPLATE,
        runtime_files=223,unchanged_runtime_files=214,changed=delta,identical_schemas=7,
        public_capability_changed=fields,application_changed=changed_app,
        historical_source_counts=[len(r['runtime_sources']) for r in histories],
        new_models=0,new_http_requests=0,
        scope='Directed readonly project-task history compatibility. Existing tool results and wire history '
              'stay immutable; no reverse/transitive/model/address/key/CA approval and no bitwise output guarantee.')
    add('review.json',store.encode(proof));add('review-source.py',Path(__file__).read_bytes())
    for name,raw in data.items():
        target=out/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(raw)
    DEST.mkdir()
    with tarfile.open(DEST/'records.tar.gz','w:gz') as tar:
        for name,raw in sorted(data.items()):
            member=tarfile.TarInfo(name);member.size=len(raw);member.mode=0o400
            tar.addfile(member,io.BytesIO(raw))
    pkg.save(DEST/'manifest.json',dict(records={n:sha(raw) for n,raw in sorted(data.items())},
        archive_sha256=pkg.sha(DEST/'records.tar.gz'),review=proof))
    print(json.dumps(dict(records=len(data),archive_sha256=pkg.sha(DEST/'records.tar.gz'))))


if __name__=='__main__':build()
