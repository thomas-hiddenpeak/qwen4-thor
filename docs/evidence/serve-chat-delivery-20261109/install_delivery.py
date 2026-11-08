"""Install the accepted application bundle while preserving stopped state and budgets."""
from contextlib import ExitStack
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

sys.dont_write_bytecode = True
ROOT = Path.cwd().resolve()
sys.path.insert(0, str(ROOT))
from tools.serve_service.package import read, save, sha, require
from tools.serve_recovery import bundle

BASE = ROOT/'.q4t-work/serve-chat-storage-20261109'
RELEASE = BASE/'release-01'
OUT = BASE/'delivery-01'
STATE = ROOT/'build/service-native-state'


def replace_file(source, target):
    temp = target.with_name(target.name+'.application-new')
    require(not temp.exists(), 'pending atomic replacement')
    with source.open('rb') as incoming, temp.open('xb') as outgoing:
        shutil.copyfileobj(incoming, outgoing)
        outgoing.flush(); os.fsync(outgoing.fileno())
    temp.chmod(0o755 if source.name == 'q4t' else 0o600)
    require(sha(temp) == sha(source), 'replacement copy identity')
    os.replace(temp,target)
    fd=os.open(target.parent,os.O_RDONLY|os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)


def point(package):
    temp=ROOT/'build/service.application-new'
    require(not temp.exists() and not temp.is_symlink(), 'pending symlink')
    temp.symlink_to(package)
    os.replace(temp,ROOT/'build/service')
    fd=os.open(ROOT/'build',os.O_RDONLY|os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)


def main():
    receipt=read(RELEASE/'receipt.json')
    package=Path(receipt['package'])
    manifest=bundle.verify(package,receipt['manifest_sha256'])
    decision=read(RELEASE/'decision.json')
    require(decision['status']=='PASS_TARGETED_STORED_CHAT_DELIVERY','acceptance')
    require(not OUT.exists(),'delivery already exists')
    OUT.mkdir(mode=0o700)
    rollback=OUT/'rollback';rollback.mkdir(mode=0o700)
    files={'q4t':ROOT/'build/q4t','q4t.release.json':ROOT/'build/q4t.release.json',
           'service.release.json':ROOT/'build/service.release.json',
           'service.json':STATE/'service.json'}
    with ExitStack() as stack:
        for path in (Path(manifest['lock_path']),STATE/'guardian.lock',STATE/'writer.lock'):
            fd=os.open(path,os.O_RDWR|os.O_NOFOLLOW)
            stack.callback(os.close,fd)
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        previous=(ROOT/'build/service').resolve(strict=True)
        old_release=read(files['service.release.json'])
        require(sha(files['q4t'])=='c704eebf3d7f8f03fb566c5ab340277427a9304a09416bed0387676ed028f28f'
                and sha(files['q4t.release.json'])=='3216d5afdcea78b1f33e37a055c1acfea89eef88b22da2a2c4c75ef19c49c000'
                and old_release['package']==str(previous),'default drift')
        bundle.verify(previous,old_release['manifest_sha256'])
        require(sha(package/'gateway-policy.json')==sha(previous/'gateway-policy.json') and
                sha(package/'engine/client-policy.json')==sha(previous/'engine/client-policy.json'),
                'policy/key/quota changed')
        ledger=read(STATE/'ledger.json')['state']
        ledger_hash=sha(STATE/'ledger.json')
        require(ledger['desired']=='STOPPED' and not any(ledger['active'].values()),
                'service is not durably stopped')
        for name,path in files.items():
            shutil.copyfile(path,rollback/name);(rollback/name).chmod(0o500 if name=='q4t' else 0o400)
            require(sha(path)==sha(rollback/name),'rollback copy')
        rollback_record=dict(package=str(previous),manifest_sha256=old_release['manifest_sha256'],
            service_id=ledger['service_id'],ledger_sha256=ledger_hash,
            files={name:sha(rollback/name) for name in files},
            procedure='Acquire GPU/Guardian/writer locks while durably STOPPED; restore these '
                'four files to original paths and repoint build/service. Preserve ledger and budgets.')
        save(rollback/'receipt.json',rollback_record)
        config=read(files['service.json'])
        config.update(package=str(package),manifest_sha256=receipt['manifest_sha256'])
        require({k:v for k,v in config.items() if k not in ('package','manifest_sha256')} ==
                {k:v for k,v in read(rollback/'service.json').items() if k not in ('package','manifest_sha256')},
                'service identity changed')
        save(OUT/'service.json',config)
        new=dict(old_release)
        new.update(acceptance=decision['acceptance'],acceptance_record=str(RELEASE/'decision.json'),
            acceptance_sha256=sha(RELEASE/'decision.json'),package=str(package),
            manifest_sha256=receipt['manifest_sha256'],
            engine_binary_sha256=sha(package/'engine/q4t'),
            engine_release_sha256=sha(package/'engine/q4t.release.json'),
            ingress_sha256=sha(package/'bin/q4t-ingress'),
            guardian_sha256=sha(package/'bin/q4t-guardian'),
            previous_acceptance_record=old_release['acceptance_record'],
            scope='Accepted stored Chat CRUD/history and real JSON/SSE/logprobs/tools; '
                'public unknown-commit semantics and same-model recovery. Prior application APIs retained. '
                'Native routes/TLS, optional nginx and v3 declarations have targeted evidence. '
                'Guardian/restart policy unchanged; remaining master tasks continue.',
            control_review=str(RELEASE/'control-review.json'),
            rollback_receipt=str(rollback/'receipt.json'),rollback_sha256=sha(rollback/'receipt.json'),
            stored_chat_targeted_accepted=True,application_targeted_accepted=True,application_declarations='q4t-release-declarations-v3',
            application_state_outside_generations=True,
            native_targeted_cpu_accepted=True,native_gpu_restart_repeated=False,
            engine_default_replaced=True,gpu_starts_for_install=0,
            state_ledger_preserved=True,automatically_started=False)
        save(OUT/'service.release.json',new)
        try:
            replace_file(package/'engine/q4t',files['q4t'])
            replace_file(package/'engine/q4t.release.json',files['q4t.release.json'])
            point(package)
            replace_file(OUT/'service.json',files['service.json'])
            replace_file(OUT/'service.release.json',files['service.release.json'])
            require(sha(STATE/'ledger.json')==ledger_hash,'ledger changed')
            bundle.verify((ROOT/'build/service').resolve(),receipt['manifest_sha256'])
            for name,cmd,wanted in (
                ('help',[str(ROOT/'build/service/bin/q4t'),'service','--help'],0),
                ('status',[str(ROOT/'build/service/bin/q4t'),'service','status','--state',str(STATE)],1)):
                r=subprocess.run(cmd,capture_output=True,text=True,timeout=15)
                (OUT/(name+'.stdout')).write_text(r.stdout)
                (OUT/(name+'.stderr')).write_text(r.stderr)
                save(OUT/(name+'.json'),dict(argv=cmd,returncode=r.returncode,model_starts=0,chat_requests=0))
                require(r.returncode==wanted,'installed help/status exit')
                if name=='status':
                    status=json.loads(r.stdout)
                    require(status['state']=='UNKNOWN' and not status['live_control']
                            and not status['business_ready'],'unexpected running service')
            require(sha(STATE/'ledger.json')==ledger_hash,'health changed ledger')
        except BaseException:
            for name,path in files.items():replace_file(rollback/name,path)
            point(previous)
            save(OUT/'rollback-applied.json',dict(applied=True,ledger_preserved=sha(STATE/'ledger.json')==ledger_hash))
            raise
        result=dict(status='PASS_STORED_CHAT_DEFAULT_INSTALL',package=str(package),
            manifest_sha256=receipt['manifest_sha256'],engine_binary_sha256=sha(files['q4t']),
            engine_release_sha256=sha(files['q4t.release.json']),
            service_release_sha256=sha(files['service.release.json']),service_id=ledger['service_id'],
            ledger_sha256=ledger_hash,ledger_preserved=True,desired='STOPPED',
            rollback_receipt=str(rollback/'receipt.json'),rollback_sha256=sha(rollback/'receipt.json'),
            model_starts=0,chat_requests=0,policy_keys_quota_unchanged=True)
        save(OUT/'result.json',result)
        print(json.dumps(result))


if __name__=='__main__': main()
