"""Offline client wire review and a source-bound, secret-screened evidence archive."""
import argparse
from dataclasses import asdict
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.serve_client_transport import stage as s
from tools.serve_client_transport import finish
from tools.examples import chat_client as chat
from tools.serve_service import package as pkg

DEST = ROOT/'docs/evidence/serve-client-transport-20261105'
sha = lambda raw: hashlib.sha256(raw).hexdigest()
finish.configure()


def audit():
    plan = s.check()
    gate = pkg.read(s.BASE/s.CPU_LABEL/'result.json')
    pkg.require(gate['passed'] and gate['tests_run']==83 and not gate['skipped'], 'CPU gate')
    node = (s.BASE/s.CPU_LABEL/'node.log').read_text().splitlines()
    pkg.require({'# tests 40','# pass 40','# fail 0','# cancelled 0','# skipped 0'}<=set(node),'Node gate')
    result = pkg.read(s.OUT/'result.json')
    pkg.require(result['status']=='PASS_CLIENT_TRANSPORT_SHORT' and result['cleanup_confirmed'] and
                result['seconds']<=s.LIMITS['seconds'],'short acceptance')
    s.verify_counts(result['counts'],result['successes'],result['real_tools'])
    guard = pkg.read(s.OUT/'lease/exit.json')
    pkg.require(guard['status']=='COMPLETED','GPU guard terminal')
    network = pkg.read(s.OUT/'network/result.json')
    pkg.require(network['passed'] and network['cases']==2*len(s.probes.CASES),'network count')
    names = {language+'-'+case for language in ('python','node') for case in s.probes.CASES}
    pkg.require({r['name'] for r in network['rows']}==names,'network coverage')
    for row in network['rows']:
        pkg.require(row==pkg.read(s.OUT/'network'/(row['name']+'.json')) and row['accepts']==1 and
                    row['thread_stopped'] and row['listener_released'],'network raw/cleanup mismatch')
    responses = []
    for language in ('python','node'):
        caps = pkg.read(s.OUT/(language+'-capabilities.json'))
        pkg.require(caps['protocol_source_sha256']==plan['protocol'],'capability identity')
        for suffix in ('ordinary','read','answer'):
            label = language+'-'+suffix
            request = pkg.read(s.OUT/(label+'-request.json'))
            raw = (s.OUT/(label+'-wire.bin')).read_bytes()
            parsed = (chat.read_sse if request['stream'] else chat.read_json)(io.BytesIO(raw))
            saved = pkg.read(s.OUT/(label+'-response.json'))
            pkg.require(asdict(parsed)=={k:v for k,v in saved.items() if k!='seconds'},'wire replay: '+label)
            pkg.require(parsed.id==request['request_id'] and parsed.model==request['model'],'response identity')
            if suffix=='ordinary':
                pkg.require(parsed.message['content'].strip()=='710003','ordinary answer')
            if suffix=='answer':
                pkg.require(parsed.finish_reason=='stop' and 'Qwen4ExpForConditionalGeneration' in
                    parsed.message['content'] and '[docs/MODEL.md:L1-L24]' in parsed.message['content'],'tool answer/citation')
            responses.append(dict(label=label,stream=request['stream'],wire_sha256=sha(raw),
                                  request_id=parsed.id,finish_reason=parsed.finish_reason))
        tool = pkg.read(s.OUT/(language+'-tool-started.json'))
        value = json.loads(pkg.read(s.OUT/(language+'-tool-result.json'))['content'])
        pkg.require(tool['name']=='read_project_record' and tool['arguments']==
            dict(path='docs/MODEL.md',start_line=1,line_count=24) and value['ok'],'real tool identity')
        pkg.require(value['source']['text']=='\n'.join(f'{n}: {line}' for n,line in
                    enumerate((ROOT/'docs/MODEL.md').read_text().splitlines()[:24],1)),
                    'actual project source differs')
    pkg.require(len({r['request_id'] for r in responses})==6,'duplicate model request')
    owners = []
    from tools.serve_cuda_quota_integration.verify import custody
    for folder in ('scoped-model','nginx'):
        old = pkg.read(s.OUT/folder/'identity.json')
        try:
            current = custody.proc(old['pid'])
        except FileNotFoundError:
            current = None
        pkg.require(current is None or current['start_ticks']!=old['start_ticks'],'owned process remains')
        owners.append(dict(role=folder,pid=old['pid'],start_ticks=old['start_ticks'],gone=True))
    lock = s.shared.a.guard.common_lock()
    st = lock.stat()
    pkg.require((st.st_dev,st.st_ino)==(66305,3294067) and s.shared.a.lock_available(lock),'original lock not released')
    for port in (s.PORT,s.TLS_PORT):
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            sock.bind(('127.0.0.1',port))
    return dict(status='PASS_CLIENT_TRANSPORT',passed=True,cpu_python=83,cpu_node=40,
        network_cases=network['cases'],model_starts=1,successful_generations=6,real_tools=2,
        responses=responses,seconds=result['seconds'],guard='COMPLETED',owners=owners,
        lock=dict(dev=st.st_dev,inode=st.st_ino,available=True),default_pair_unchanged=plan['default'],
        protocol=plan['protocol'],source_commit=plan['source_commit'],
        current_task_migration_accepted=False,managed_package_accepted=False,long_run=False,
        attempts=2,network_attempts=result['counts']['network'],reused_network_observations=8,
        original_failure='Local TLS fixture closed before consuming the complete POST request; no model started.',
        note='Native and fixed JS SDK client acceptance only; no new managed lifecycle or durable migration qualification.')


def collect():
    plan = pkg.read(s.PLAN)
    files = {}
    for directory in (s.BASE/s.CPU_LABEL,s.BASE/'pre-resume',s.OUT,
                      s.BASE/'execution',s.BASE/'initial-source'):
        for path in sorted(directory.rglob('*')):
            if path.is_file() and 'private' not in path.relative_to(directory).parts:
                files[str(path.relative_to(s.BASE))] = path.read_bytes()
    for name in ('plan.json','finish-plan.json','sdk.json','pre-resume.json',
                 'fixture-baseline.json','fixture-fixed.json'):
        files[name] = (s.BASE/name).read_bytes()
    old = ROOT/'.q4t-work/serve-client-transport-20261104/baseline'
    for path in sorted(old.rglob('*')):
        if path.is_file():
            files['baseline/'+str(path.relative_to(old))] = path.read_bytes()
    for name,digest in {**plan['sources'],**plan['runtime']}.items():
        raw = (ROOT/name).read_bytes()
        pkg.require(sha(raw)==digest,'source archive drift')
        files['current-source/'+name] = raw
    for label in ('frontend','policy'):
        private = pkg.read(plan[label])
        secrets = [c['api_key'] for c in private['clients']] if label=='frontend' else [private['gateway_token']]
        for secret in secrets:
            pkg.require(not any(secret.encode() in raw for raw in files.values()),'credential in evidence')
    return files


def archive():
    result = audit()
    pkg.require(not DEST.exists(),'preserve original archive')
    files = collect()
    files['acceptance.json'] = (json.dumps(result,ensure_ascii=False,indent=2)+'\n').encode()
    manifest = {name:dict(sha256=sha(raw),bytes=len(raw)) for name,raw in files.items()}
    DEST.mkdir()
    with tarfile.open(DEST/'records.tar.gz','w:gz') as tar:
        for name,raw in sorted(files.items()):
            info = tarfile.TarInfo(name);info.size=len(raw);info.mode=0o600;info.mtime=0
            tar.addfile(info,io.BytesIO(raw))
    pkg.save(DEST/'manifest.json',manifest)
    pkg.save(DEST/'result.json',result)
    receipt = dict(passed=True,files=len(files),archive_sha256=pkg.sha(DEST/'records.tar.gz'),
                   manifest_sha256=pkg.sha(DEST/'manifest.json'),bytes=(DEST/'records.tar.gz').stat().st_size)
    pkg.save(s.BASE/'archive.json',receipt)
    return receipt


def verify():
    manifest = pkg.read(DEST/'manifest.json')
    expected = collect()
    expected['acceptance.json'] = (DEST/'result.json').read_bytes()
    with tarfile.open(DEST/'records.tar.gz','r:gz') as tar:
        members = tar.getmembers()
        pkg.require(len(members)==len(manifest) and {m.name for m in members}==set(manifest),'archive membership')
        for member in members:
            pkg.require(member.isfile(),'unexpected archive member')
            raw = tar.extractfile(member).read()
            pkg.require(len(raw)==manifest[member.name]['bytes'] and sha(raw)==manifest[member.name]['sha256'] and
                        raw==expected[member.name],'archive/raw/source mismatch')
    receipt = dict(passed=True,files=len(manifest),archive_sha256=pkg.sha(DEST/'records.tar.gz'))
    pkg.save(s.BASE/'verified.json',receipt)
    return receipt


if __name__=='__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('archive','verify'))
    args = parser.parse_args()
    print(json.dumps(globals()[args.action]()))
