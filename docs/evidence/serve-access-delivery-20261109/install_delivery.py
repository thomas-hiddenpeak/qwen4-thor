"""Install accepted N04 under global ownership; never restore legacy client state."""
from contextlib import ExitStack
import fcntl
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import time
import uuid

sys.dont_write_bytecode=True
ROOT=Path.cwd().resolve();sys.path.insert(0,str(ROOT))
from tools.serve_service.package import read,save,sha,require
from tools.serve_recovery import bundle,ledger,deployment

BASE=ROOT/'.q4t-work/serve-access-20261109';RELEASE=BASE/'release-01'
OUT=BASE/'delivery-01';STATE=ROOT/'build/service-native-state'


def replace(source,target):
    temporary=target.with_name(target.name+'.access-new')
    require(not temporary.exists(),'pending default replacement')
    with source.open('rb') as src,temporary.open('xb') as dst:
        shutil.copyfileobj(src,dst);dst.flush();os.fsync(dst.fileno())
    temporary.chmod(0o755 if target.name=='q4t' else 0o600)
    require(sha(source)==sha(temporary),'replacement bytes')
    os.replace(temporary,target)
    fd=os.open(target.parent,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)


def point(package):
    temporary=ROOT/'build/service.access-new'
    require(not os.path.lexists(temporary),'pending service replacement')
    temporary.symlink_to(package);os.replace(temporary,ROOT/'build/service')
    fd=os.open(ROOT/'build',os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)


def cli(package,label,operation,*args,wanted=0):
    command=[str(package/'bin/q4t'),'service',operation]
    if operation!='--help':command+=['--state',str(STATE)]
    command+=list(map(str,args))
    result=subprocess.run(command,stdin=subprocess.DEVNULL,capture_output=True,timeout=20)
    (OUT/(label+'.stdout')).write_bytes(result.stdout);(OUT/(label+'.stderr')).write_bytes(result.stderr)
    save(OUT/(label+'.json'),dict(argv=command,exit_code=result.returncode,model_starts=0,chat_requests=0))
    require(result.returncode==wanted,'installed command failed '+label)
    return json.loads(result.stdout) if operation not in ('--help','metrics') else result.stdout.decode()


def main():
    require(not OUT.exists(),'delivery exists');OUT.mkdir(mode=0o700)
    receipt=read(RELEASE/'receipt.json');package=Path(receipt['package'])
    backup=Path(read(RELEASE/'rollback-receipt.json')['package']);digest=receipt['manifest_sha256']
    manifest=bundle.verify(package,digest);bundle.verify(backup,digest)
    reviewed=read(RELEASE/'control-review.json')
    require(all(sha(ROOT/n)==h for n,h in reviewed['files'].items()),'control drift')
    passed=read(BASE/'packaged-01/result.json')
    require(passed['status']=='PASS_ACCEPTED_N04_PACKAGED_CLI' and passed['manifest_sha256']==digest,'actual CLI gate')
    decision=dict(status='PASS_N04_DEFAULT_DELIVERY_PREREQUISITES',
        acceptance='ACCEPT_TARGETED_NATIVE_ACCESS_QUOTA_CONTINUITY',manifest_sha256=digest,
        business=dict(path=str(ROOT/'docs/evidence/serve-access-business-20261109/decision.json'),
            sha256=sha(ROOT/'docs/evidence/serve-access-business-20261109/decision.json')),
        packaged=dict(path=str(BASE/'packaged-01/result.json'),sha256=sha(BASE/'packaged-01/result.json')),
        accepted_engine_metadata_sha256=sha(package/'engine/q4t.release.json'),
        model_starts_for_install=0,chat_requests_for_install=0,performance_acceptance=False,
        long_stability_accepted=False,remaining_master_tasks_open=True)
    save(OUT/'decision.json',decision)
    before_files=OUT/'pre-migration';before_files.mkdir(mode=0o700)
    rollback=OUT/'rollback';rollback.mkdir(mode=0o700)
    ids=dict(migrate=str(uuid.uuid4()),select=str(uuid.uuid4()),emergency_rollback=str(uuid.uuid4()))
    save(OUT/'operation-ids.json',ids)
    with ExitStack() as stack:
        lock_path=Path(manifest['lock_path'])
        fd=os.open(lock_path,os.O_RDWR|os.O_NOFOLLOW|os.O_CLOEXEC);stack.callback(os.close,fd)
        held=os.fstat(fd);named=lock_path.stat(follow_symlinks=False)
        require(stat.S_ISREG(held.st_mode) and held.st_uid==os.geteuid() and held.st_nlink==1 and
            (held.st_dev,held.st_ino)==(named.st_dev,named.st_ino),'global ownership lock identity')
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        require(all(sha(r['path'])==r['sha256'] for r in reviewed['defaults']),'default changed before install')
        old=(ROOT/'build/service').resolve(strict=True);old_release=read(ROOT/'build/service.release.json')
        bundle.verify(old,old_release['manifest_sha256'])
        before=read(STATE/'ledger.json');value=before['state'];ledger.validate(value,value['service_id'])
        require(before['sha256']==ledger.digest(value) and value['desired']=='STOPPED' and
            not any(value['active'].values()) and not (STATE/'ledger.pending').exists(),'not durably stopped')
        for name,source in [('q4t',ROOT/'build/q4t'),('q4t.release.json',ROOT/'build/q4t.release.json'),
                ('service.release.json',ROOT/'build/service.release.json'),('service.json',STATE/'service.json')]:
            shutil.copyfile(source,before_files/name);(before_files/name).chmod(0o500 if name=='q4t' else 0o400)
            require(sha(source)==sha(before_files/name),'preserved bytes')
        save(before_files/'receipt.json',dict(package=str(old),manifest_sha256=old_release['manifest_sha256'],
            files={p.name:sha(p) for p in before_files.iterdir() if p.is_file()},
            ledger_sha256=sha(STATE/'ledger.json'),purpose='Historical pre-migration evidence only. '
                'After the durable managed-state fence, do NOT restore this legacy descriptor/engine/service pair.'))
        for name in ('gateway-policy.json','engine/client-policy.json'):
            require(sha(package/name)==sha(old/name),'published seed key/limits changed')
        metadata=dict(old_release)
        metadata.pop('rollback_receipt',None);metadata.pop('rollback_sha256',None)
        metadata.update(package=str(backup),manifest_sha256=digest,acceptance=decision['acceptance'],
            acceptance_record=str(OUT/'decision.json'),acceptance_sha256=sha(OUT/'decision.json'),
            previous_acceptance_record=old_release['acceptance_record'],
            engine_binary_sha256=sha(package/'engine/q4t'),engine_release_sha256=sha(package/'engine/q4t.release.json'),
            ingress_sha256=sha(package/'bin/q4t-ingress'),guardian_sha256=sha(package/'bin/q4t-guardian'),
            control_review=str(RELEASE/'control-review.json'),engine_default_replaced=True,
            engine_metadata_unchanged=False,state_ledger_preserved=False,core_state_values_preserved=True,
            ledger_envelope_upgraded=True,native_client_policy_targeted_accepted=True,
            native_key_rotation_and_revocation=True,managed_quota_handoff=True,
            compatible_stopped_deployment=True,gpu_starts_for_install=0,automatically_started=False,
            scope='Accepted native credential/policy update, stable client identities, exact normal quota '
                'handoff and conservative unknown recovery; compatible STOPPED upgrade/rollback preserves '
                'current state. Existing application/observation/cache contracts retained. All remaining tasks continue.',
            limitations=['Remaining OpenAI inference/application tasks stay active in the master plan.',
                'Remaining HTTP ingress and general deployment configuration N05/N07 remain active.',
                'Automatic profile and older-code fallback R05/R06 remain active; this rollback is a compatible v8 copy.',
                'Physical RAM protection, NVML-free cold start and whole-host recovery remain active.',
                'User-run long stability remains pending; certificates/DNS/CIDRs are deployment-selected.'])
        save(rollback/'q4t.release.json',read(backup/'engine/q4t.release.json'))
        save(rollback/'service.release.json',metadata)
        save(rollback/'receipt.json',dict(package=str(backup),manifest_sha256=digest,
            service_id=value['service_id'],engine_unchanged=dict(binary_sha256=metadata['engine_binary_sha256'],
                release_sha256=metadata['engine_release_sha256']),
            files={name:sha(rollback/name) for name in ('q4t.release.json','service.release.json')},
            compatible_managed_state=True,legacy_v6_restore_allowed=False,
            procedure='With global GPU ownership and a durable STOPPED service, run this retained v8 '
                'package service rollback --state STATE --expected-manifest-sha256 CURRENT --request-id UUID. '
                'Do not copy service.json or any mutable client/core journal. Repoint build/service to this '
                'package and restore only these matching release records; engine bytes remain the accepted v8 engine.'))
        current=dict(metadata,package=str(package),rollback_receipt=str(rollback/'receipt.json'),
            rollback_sha256=sha(rollback/'receipt.json'))
        save(OUT/'service.release.json',current)
        try:
            cli(backup,'migrate','deploy','--expected-manifest-sha256',old_release['manifest_sha256'],
                '--request-id',ids['migrate'])
            cli(package,'select','deploy','--expected-manifest-sha256',digest,'--request-id',ids['select'])
            replace(package/'engine/q4t',ROOT/'build/q4t')
            replace(package/'engine/q4t.release.json',ROOT/'build/q4t.release.json')
            point(package);replace(OUT/'service.release.json',ROOT/'build/service.release.json')
            core=read(STATE/'ledger.json')
            require(core['state']==value and core['service_contract']=='q4t-service-state-v2','core state changed')
            bundle.verify((ROOT/'build/service').resolve(),digest)
            cli(package,'installed-help','--help')
            observed=cli(package,'installed-observe','observe')
            require(observed['state']=='STOPPED' and not observed['business_ready'],'installed service not stopped')
            cli(package,'installed-metrics','metrics')
            selected=cli(package,'installed-deployment','deployment-status')
            require(selected['current']['package']==str(package),'installed descriptor mismatch')
            for family in ('access','limits','usage'):cli(package,'installed-'+family,family+'-status')
            pins=cli(package,'installed-retention','retention-plan')
            require(not pins['remove'] and not pins['destructive'],'unexpected cleanup proposal')
            require(read(STATE/'ledger.json')['state']==value,'health checks changed core budgets')
        except BaseException:
            # A partially committed migration must resume the saved UUID;
            # never undo its one-way fence or restore revoked/static keys.
            desc=read(STATE/'service.json')
            if desc.get('version')==2 and desc.get('package')==str(package):
                try:
                    cli(backup,'failure-rollback','rollback','--expected-manifest-sha256',digest,
                        '--request-id',ids['emergency_rollback'])
                    replace(backup/'engine/q4t',ROOT/'build/q4t')
                    replace(backup/'engine/q4t.release.json',ROOT/'build/q4t.release.json')
                    point(backup);replace(rollback/'service.release.json',ROOT/'build/service.release.json')
                    save(OUT/'rollback-applied.json',dict(compatible_backup=True,mutable_state_preserved=True))
                except BaseException:save(OUT/'resume-required.json',dict(operation_ids=ids,legacy_restore_forbidden=True))
            else:save(OUT/'resume-required.json',dict(operation_ids=ids,legacy_restore_forbidden=True))
            raise
        result=dict(status='PASS_N04_NATIVE_CLIENT_POLICY_DEFAULT_DELIVERY',package=str(package),
            manifest_sha256=digest,engine_binary_sha256=sha(ROOT/'build/q4t'),
            engine_release_sha256=sha(ROOT/'build/q4t.release.json'),
            service_release_sha256=sha(ROOT/'build/service.release.json'),service_id=value['service_id'],
            ledger_sha256=sha(STATE/'ledger.json'),core_state_sha256=ledger.digest(value),
            ledger_envelope_upgraded=True,core_state_values_preserved=True,
            rollback_receipt=str(rollback/'receipt.json'),rollback_sha256=sha(rollback/'receipt.json'),
            compatible_rollback_package=str(backup),legacy_static_policy_rollback_forbidden=True,
            initial_keys_and_limits_preserved=True,desired='STOPPED',automatically_started=False,
            model_starts=0,chat_requests=0,remaining_master_tasks_open=True)
        save(OUT/'result.json',result);print(json.dumps(result))


if __name__=='__main__':os.umask(0o077);main()
