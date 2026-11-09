"""CPU-only real accepted CLI migration/rotation/deploy/rollback, no listener."""
import copy
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
import uuid

sys.dont_write_bytecode=True
ROOT=Path.cwd().resolve();sys.path.insert(0,str(ROOT))
from tools.serve_service.package import read,save,sha,require
from tools.serve_recovery import bundle,ledger

BASE=ROOT/'.q4t-work/serve-access-20261109';RELEASE=BASE/'release-01';OUT=BASE/'packaged-01'


def main():
    require(not OUT.exists(),'packaged stage exists');OUT.mkdir(mode=0o700)
    package=Path(read(RELEASE/'receipt.json')['package']);backup=Path(read(RELEASE/'rollback-receipt.json')['package'])
    digest=sha(package/'manifest.json');old=(ROOT/'build/service').resolve(strict=True)
    manifest=bundle.verify(package,digest);bundle.verify(backup,digest)
    protected=read(RELEASE/'control-review.json')['defaults']
    state=OUT/'legacy-state';fresh=OUT/'fresh-state';calls=[]
    keys=read(old/'gateway-policy.json')['clients']
    secret_values=[c['api_key'] for c in keys]
    def cli(selected,label,operation,*args,scope=state,wanted=0,json_output=True):
        command=[str(selected/'bin/q4t'),'service',operation]
        if operation!='--help':command+=['--state',str(scope)]
        command+=list(map(str,args));started=time.monotonic()
        result=subprocess.run(command,stdin=subprocess.DEVNULL,capture_output=True,timeout=20)
        require(not any(key.encode() in result.stdout+result.stderr for key in secret_values),'credential output')
        (OUT/(label+'.stdout')).write_bytes(result.stdout);(OUT/(label+'.stderr')).write_bytes(result.stderr)
        value=dict(argv=command,exit_code=result.returncode,seconds=time.monotonic()-started)
        save(OUT/(label+'.json'),value);calls.append(value)
        require(result.returncode==wanted,'CLI '+label)
        return json.loads(result.stdout) if json_output else result.stdout.decode()
    help_text=cli(package,'help','--help',json_output=False)
    require(all(name in help_text for name in ('init-resume','access-apply','limits-apply','usage-status','deploy','rollback')),
            'missing native CLI commands')
    initialized=cli(package,'fresh-init','init',scope=fresh)
    resumed=cli(package,'fresh-resume','init-resume',scope=fresh)
    require(initialized['service_id']==resumed['service_id'],'init resume identity')
    cli(old,'legacy-init','init')
    before=read(state/'ledger.json')['state'];service_id=before['service_id']
    cli(backup,'legacy-deploy','deploy','--expected-manifest-sha256',sha(old/'manifest.json'),
        '--request-id',uuid.uuid4())
    core=read(state/'ledger.json')
    require(core['service_contract']=='q4t-service-state-v2' and core['state']==before,'legacy core values reset')
    changed='n04-packaged-'+secrets.token_hex(24);secret_values.append(changed)
    rows=[dict(id=row['id'],api_keys=[changed if row['id']=='alpha' else row['api_key']]) for row in keys]
    save(OUT/'keys-private.json',dict(version=1,clients=rows))
    cli(backup,'keys-propose','access-propose','--keys',OUT/'keys-private.json','--proposal',OUT/'keys-proposal.json')
    applied=cli(backup,'keys-apply','access-apply','--proposal',OUT/'keys-proposal.json','--wait-seconds',0)
    require(applied['durable'] and not applied['live_applied'],'stopped key mutation mislabeled live')
    replay=cli(backup,'keys-replay','access-apply','--proposal',OUT/'keys-proposal.json','--wait-seconds',0)
    require(replay['revision']==applied['revision'],'key replay changed version')
    limits=copy.deepcopy(read(old/'engine/client-policy.json')['clients'])
    limits[0]['requests_per_minute']=max(1,limits[0]['requests_per_minute']-1)
    save(OUT/'limits-private.json',dict(version=1,clients=limits))
    cli(backup,'limits-propose','limits-propose','--limits',OUT/'limits-private.json','--proposal',OUT/'limits-proposal.json')
    applied=cli(backup,'limits-apply','limits-apply','--proposal',OUT/'limits-proposal.json','--wait-seconds',0)
    require(applied['durable'] and not applied['live_applied'],'stopped limits mutation mislabeled live')
    policies={name:sha(state/name/'policy.json') for name in ('client-access','client-limits','client-usage')}
    request_id=str(uuid.uuid4())
    selected=cli(package,'deploy','deploy','--expected-manifest-sha256',digest,'--request-id',request_id)
    require(selected['current']['package']==str(package),'wrong selected package')
    replay=cli(package,'deploy-replay','deploy','--expected-manifest-sha256',digest,'--request-id',request_id)
    require(replay['replayed'],'deployment request not idempotent')
    rolled=cli(backup,'rollback','rollback','--expected-manifest-sha256',digest,'--request-id',uuid.uuid4())
    require(rolled['current']['package']==str(backup),'wrong rollback selection')
    require(all(sha(state/n/'policy.json')==h for n,h in policies.items()),'rollback restored old client state')
    keys_state=read(state/'client-access/policy.json')['state']['policy']
    alpha=next(c for c in keys_state['clients'] if c['id']=='alpha')
    import hashlib
    require(alpha['key_sha256']==[hashlib.sha256(changed.encode()).hexdigest()],'revoked key returned')
    for operation in ('deployment-status','access-status','limits-status','usage-status'):
        cli(backup,operation,operation)
    observed=cli(backup,'observe','observe');require(observed['state']=='STOPPED' and not observed['business_ready'],'observation')
    metrics=cli(backup,'metrics','metrics',json_output=False)
    require('q4t_service_business_ready 0' in metrics,'stopped metrics')
    retention=cli(backup,'retention','retention-plan')
    require(not retention['remove'] and not retention['destructive'],'unexpected deletion')
    current=read(state/'ledger.json')['state']
    require(current==before and current['service_id']==service_id,'core budgets changed')
    require(all(sha(r['path'])==r['sha256'] for r in protected),'production default changed')
    decision=dict(status='PASS_ACCEPTED_N04_PACKAGED_CLI',manifest_sha256=digest,source_package=str(package),
        compatible_rollback=str(backup),actual_cli_calls=len(calls),native_init_resume=True,
        legacy_migration_core_values_preserved=True,credential_revocation_persistent=True,
        limits_and_usage_preserved=True,deploy_and_rollback=True,stopped_observation=True,
        default_changed=False,model_starts=0,chat_requests=0,gpu_operations=0,
        production_runtime_unmodified=True,service_id=service_id)
    save(OUT/'result.json',decision);print(json.dumps(decision))


if __name__=='__main__':os.umask(0o077);main()
