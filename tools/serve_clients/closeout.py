"""Seal client default delivery; rollback accepts only the pinned old/new pair."""
import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tools.serve_clients import delivery as d, report

OUTPUT=d.BASE/'delivery-absolute'
DEST=ROOT/'docs/evidence/serve-clients-delivery-20261104'


def postflight():
    report.verify()
    d.h.verify_seal(d.PACKAGE)
    d.h.verify_seal(OUTPUT)
    plan=d.s.pkg.read(d.PACKAGE/'plan.json')
    d.h.require(d.s.pkg.read(OUTPUT/'finalized.json')['status']=='DELIVERED' and
        d.s.pkg.read(OUTPUT/'lease/exit.json')['status']=='COMPLETED' and
        d.s.pkg.read(d.BASE/'delivery/lease/exit.json')['status']=='COMMAND_FAILED', 'delivery history')
    binding=d.s.pkg.read(OUTPUT/'release/binding.json')
    actual=d.a.state_pair(ROOT/'build',d.PACKAGE/'previous',OUTPUT/'pair',binding['old'],binding['new'])
    d.h.require(actual==binding['new'],'default is not the delivered pair')
    d.h.verify_pair(ROOT/'build/q4t',ROOT/'build/q4t.release.json',plan['candidate_sha256'])
    d.h.verify_pair(d.PACKAGE/'previous/q4t',d.PACKAGE/'previous/q4t.release.json',plan['previous_binary_sha256'])
    d.h.require(not list((ROOT/'build').glob('*recovery*.json')),'pending recovery marker')
    for name,digest in plan['day_plans'].items():
        d.h.require(d.cpu.sha(name)==digest and not (Path(name).parent/'execution').exists(),'operator day drift')
    folder=d.BASE/'delivery-postflight';folder.mkdir()
    d.s.a.require_idle(folder/'gpu.json')
    lock=d.s.a.guard.common_lock();info=lock.stat()
    d.h.require((info.st_dev,info.st_ino)==(66305,3294067) and d.s.a.lock_available(lock),'original lock unavailable')
    unit='soar-flash-fast-v3-fixture-retry-qualified-v1-20261103.service'
    mask=Path.home()/'.config/systemd/user'/unit
    d.h.require(mask.is_symlink() and os.readlink(mask)=='/dev/null','SOAR mask removed')
    query=subprocess.run(['systemctl','--user','show',unit,'-p','MainPID','-p','LoadState','-p','ActiveState'],
                          capture_output=True,text=True,timeout=10,check=True)
    d.h.require(all(x in query.stdout for x in ('MainPID=0','LoadState=masked','ActiveState=inactive')),'SOAR changed')
    (folder/'soar-unit.txt').write_text(query.stdout)
    containers=subprocess.run(['sudo','-n','docker','ps','--format','{{.Names}}'],
                              capture_output=True,text=True,timeout=10,check=True)
    d.h.require(not containers.stdout.strip(),'unexpected container, do not interfere')
    (folder/'containers.txt').write_text(containers.stdout)
    owned=[]
    for old in d.s.pkg.read(d.BASE/'postflight/result.json')['owned']:
        path=Path(f"/proc/{old['pid']}/stat")
        same=path.exists() and int(path.read_text().rsplit(')',1)[1].split()[19])==old['start_ticks']
        owned.append(dict(old,exited=not same))
    d.h.require(all(row['exited'] for row in owned),'owned process returned')
    result=dict(status='DELIVERED_BOUNDED_CLIENT_ISOLATION',default=d.h.identity(ROOT/'build/q4t'),
        sidecar=d.h.identity(ROOT/'build/q4t.release.json'),protocol_sha256=plan['protocol_sha256'],
        runtime_sources=len(d.s.pkg.read(d.PACKAGE/'candidate/runtime-source.json')),
        previous_binary_sha256=plan['previous_binary_sha256'],rollback_pair_verified=True,
        new_model_starts=0,new_http_requests=0,long_run=False,original_delivery_refusal_preserved=True,
        lock_available=True,lock_device=info.st_dev,lock_inode=info.st_ino,owned=owned,
        days_unchanged=True,day_execution_absent=True,soar_masked_inactive=True,containers_empty=True)
    d.cpu.save(folder/'result.json',result)
    return result


def archive():
    result=postflight()
    data,local={},{}
    for name in ('delivery-package','delivery','delivery-absolute','delivery-postflight'):
        for path in sorted((d.BASE/name).rglob('*')):
            if not path.is_file() or path.is_symlink():
                continue
            with path.open('rb') as stream:
                elf=stream.read(4)==b'\x7fELF'
            if elf:
                local[str(path)]=d.h.identity(path)
            else:
                data[str(path.relative_to(d.BASE))]=path.read_bytes()
    data['delivery-launch.json']=(d.BASE/'delivery-launch.json').read_bytes()
    for path in (Path(__file__),Path(d.__file__)):
        data['source/'+path.name]=path.read_bytes()
    policy=d.s.pkg.read(d.BASE/'short-private/clients.json')
    front=d.s.pkg.read(d.BASE/'short-private/gateway.json')
    secrets=[policy['gateway_token'],*(c['api_key'] for c in front['clients'])]
    d.h.require(all(value.encode() not in raw for raw in data.values() for value in secrets),'credential in delivery archive')
    files={name:dict(bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest()) for name,raw in sorted(data.items())}
    buffer=io.BytesIO()
    with tarfile.open(fileobj=buffer,mode='w') as target:
        for name,raw in sorted(data.items()):
            item=tarfile.TarInfo(name);item.size,item.mode,item.mtime=len(raw),0o600,0
            target.addfile(item,io.BytesIO(raw))
    packed=gzip.compress(buffer.getvalue(),mtime=0)
    DEST.mkdir(parents=True)
    (DEST/'records.tar.gz').write_bytes(packed)
    d.cpu.save(DEST/'manifest.json',dict(files=files,local_identities=local,
        acceptance=d.h.identity(report.DEST/'result.json'),
        archive=dict(bytes=len(packed),sha256=hashlib.sha256(packed).hexdigest())))
    d.cpu.save(DEST/'result.json',result)
    return verify()


def verify():
    manifest=d.s.pkg.read(DEST/'manifest.json')
    raw=(DEST/'records.tar.gz').read_bytes()
    d.h.require(len(raw)==manifest['archive']['bytes'] and
                hashlib.sha256(raw).hexdigest()==manifest['archive']['sha256'],'delivery archive identity')
    data={}
    with tarfile.open(fileobj=io.BytesIO(raw),mode='r:gz') as source:
        for item in source:
            d.h.require(item.isfile() and item.name in manifest['files'] and item.name not in data,'archive member')
            body=source.extractfile(item).read();identity=manifest['files'][item.name]
            d.h.require(len(body)==identity['bytes'] and hashlib.sha256(body).hexdigest()==identity['sha256'],'record identity')
            data[item.name]=body
    d.h.require(data.keys()==manifest['files'].keys(),'missing delivery records')
    read=lambda name:json.loads(data[name])
    result=read('delivery-postflight/result.json')
    d.h.require(result==d.s.pkg.read(DEST/'result.json') and result['rollback_pair_verified'] and
        read('delivery-absolute/finalized.json')['status']=='DELIVERED' and
        read('delivery-absolute/lease/exit.json')['status']=='COMPLETED' and
        read('delivery/lease/exit.json')['status']=='COMMAND_FAILED','delivery acceptance')
    binding=read('delivery-absolute/release/binding.json')
    d.h.require(binding['new']['q4t']==result['default']['sha256'] and
        binding['new']['q4t.release.json']==result['sidecar']['sha256'] and
        read('delivery-absolute/health/result.json')['new_model_starts']==0,'installed identity or scope')
    return dict(passed=True,records=len(data),local_identities=len(manifest['local_identities']),
                compressed_bytes=len(raw),new_model_or_http=0)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('archive','verify','rollback'))
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    if args.action=='rollback':
        # Deliberately use immutable package/pair checks, not current workspace
        # documentation hashes. Unknown defaults are still rejected by restore.
        d.h.require(args.output is not None,'fresh absolute rollback lease output required')
        d.OUTPUT=OUTPUT
        d.rollback(args.output.resolve())
    else:
        print(json.dumps(globals()[args.action]()))
