"""Prepare new retained-control and private TLS payloads; no default mutation."""
import copy
import os
from pathlib import Path
import shutil
import subprocess
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_recovery import bundle, version_contract
from tools.serve_recovery.test_native_access import certificates
from tools.serve_service import capacity, package as p

WORK = ROOT/'.q4t-work/serve-file-search-20261112'
OUT = WORK/'managed-packages-01'
OLD = (ROOT/'build/service').resolve(strict=True)


def replace(source, target):
    target.chmod(0o600)
    shutil.copyfile(source, target)
    target.chmod(0o500 if target.parent.name == 'bin' else 0o400)


def main():
    p.require(not OUT.exists(), 'preserve existing payload preparation')
    p.require(not subprocess.check_output(['git','status','--porcelain'],cwd=ROOT),
              'commit reviewed source first')
    before = {n:p.sha(ROOT/n) for n in ('build/q4t','build/q4t.release.json',
        'build/service/manifest.json','build/service.release.json')}
    old = bundle.verify(OLD,p.sha(OLD/'manifest.json'))
    receipt = p.read(WORK/'candidate-01/receipt.json')
    primary = Path(receipt['package'])
    accepted = bundle.verify(primary,receipt['manifest_sha256'])
    p.require(old['recovery_profile_policy'] == accepted['recovery_profile_policy'],
              'profile policy differs')
    OUT.mkdir(mode=0o700)
    target=OUT/'retained'
    shutil.copytree(OLD,target,symlinks=True,
        ignore=lambda folder,_: {'engine'} if Path(folder)==OLD else set())
    shutil.copytree(primary/'engine',target/'engine',symlinks=True)
    changed=copy.deepcopy(old)
    changed['engine_manifest_sha256']=accepted['engine_manifest_sha256']
    changed['recovery_profiles']=accepted['recovery_profiles']
    for name in ('profile-primary.json','profile-light.json','native-build.json','bin/q4t-ingress'):
        replace(primary/name,target/name)
        changed['files'][name]=p.sha(target/name)
    p.save(target/'manifest.json',changed)
    digest=p.sha(target/'manifest.json')
    (target/'bin/q4t').chmod(0o700)
    (target/'bin/q4t').write_bytes(bundle.launcher(digest,changed['python']['path']))
    (target/'bin/q4t').chmod(0o500)
    p.require(bundle.verify(target,digest)==changed,'retained representation differs')
    difference={n for n,h in old['files'].items() if changed['files'][n]!=h}
    p.require(difference=={'profile-primary.json','profile-light.json',
                          'native-build.json','bin/q4t-ingress'},'old control source changed')
    root=dict(package=str(primary),manifest_sha256=receipt['manifest_sha256'])
    fallback=dict(package=str(target),manifest_sha256=digest)
    compatibility=version_contract.compatible(version_contract.payload(root),
                                               version_contract.payload(fallback))
    p.require(compatibility['automatic'],'automatic capability contract differs')
    p.save(OUT/'retained-review.json',dict(status='NEW_PAYLOAD_ACTUAL_ACCEPTANCE_PENDING',
        original=dict(package=str(OLD),manifest_sha256=p.sha(OLD/'manifest.json')),
        primary=root,fallback=fallback,changed_files=sorted(difference),
        actual_older_control_retained=True,new_engine_and_ingress=True,
        older_model_implementation=False,original_acceptance_not_relabelled=True,
        automatic_compatibility=compatibility,default_unchanged=True))
    # TLS is an optional native variant, retaining the engine, capacities,
    # authenticated client policies and recovery qualifications.
    certs=OUT/'certs';certificates(certs)
    raw=p.read(primary/'gateway-policy.json')
    raw.update(listen_port=19108,upstream_port=19109,upstream_operations_port=19110,
               listen_host='127.0.0.1',server_name='127.0.0.1')
    p.save(OUT/'tls-policy.json',raw)
    settings=p.read(primary/'engine/manifest.json')['settings']
    profile=dict(version=1,**{k:v for k,v in settings['profile'].items()
                             if k in capacity.BOUNDS or k=='mtp_enabled'})
    kwargs={key:primary/'engine'/name for key,name in [
        ('access_program_policy','access-program-policy.json'),
        ('file_input_policy','file-input-policy.json')] if (primary/'engine'/name).is_file()}
    tls=bundle.prepare(primary/'engine/q4t',primary/'engine/q4t.release.json',
        p.read(primary/'engine/manifest.json')['model_dir'],primary/'bin/q4t-guardian',
        primary/'bin/q4t-cuda-probe',None,OUT/'tls-policy.json',
        primary/'engine/client-policy.json',OUT/'tls',
        native_receipt=primary/'native-build.json',native_ingress=primary/'bin/q4t-ingress',
        device_control=primary/'bin/q4t-device-control',capacity_config=profile,
        certificate=certs/'server.pem',key=certs/'server.key',
        application_state={k:v for k,v in settings['application_state'].items() if k!='profile'},
        embedding_model_dir=Path(settings['native_embedding']['directory']),
        recovery_profiles=WORK/'profiles-01/qualified/registry.json',
        recovery_profile_policy=accepted['recovery_profile_policy'],**kwargs)
    p.save(OUT/'tls-receipt.json',tls)
    bundle.verify(tls['package'],tls['manifest_sha256'])
    p.require(all(p.sha(ROOT/n)==h for n,h in before.items()),'default changed')
    p.save(OUT/'review.json',dict(status='PREPARED_ACTUAL_MANAGED_ACCEPTANCE_PENDING',
        primary=root,retained=fallback,tls=tls,private_ca=str(certs/'ca.pem'),
        default_unchanged=True,gpu_starts=0,external_frontend_required=False,
        system_trust_modified=False,protected=before))
    print('retained',digest,'tls',tls['manifest_sha256'])


if __name__=='__main__':
    os.umask(0o077)
    main()
