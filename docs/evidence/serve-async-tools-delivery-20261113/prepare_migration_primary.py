"""Re-seal one reviewed migration-only change; retain exact GPU evidence."""
import copy
import os
from pathlib import Path
import shutil
import subprocess
import sys
sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from tools.serve_recovery import bundle,version_contract
from tools.serve_service import package as p
WORK=Path(__file__).resolve().parent
OLD=WORK/'candidate-02/bundle';OLD_SHA='9c11a13fc1a40a50a5ee72401541d4d9aaf04112aef1f71c2f10f847c6466fe4'
OUT=WORK/'candidate-03';TARGET=OUT/'bundle'
SOURCE='source/tools/serve_recovery/profile_migration.py'

def main():
    os.umask(0o077)
    p.require(not subprocess.check_output(['git','status','--porcelain'],cwd=ROOT),'commit reviewed sources')
    p.require(not OUT.exists(),'preserve prior patch')
    old=bundle.verify(OLD,OLD_SHA)
    p.require(p.read(WORK/'migration-cpu-01.json')['exit_code']==0 and
        p.read(WORK/'migration-audit-01.json')['exit_code']==0 and
        p.read(WORK/'migration-real-predecessor.json')['status']=='PASS_REAL_PREDECESSOR_CPU',
        'targeted migration checks absent')
    delta={n for n,h in old['files'].items() if n.startswith('source/') and p.sha(ROOT/n[7:])!=h}
    p.require(delta=={SOURCE},'unreviewed control changes')
    OUT.mkdir(mode=0o700);shutil.copytree(OLD,TARGET,symlinks=True)
    changed=copy.deepcopy(old);target=TARGET/SOURCE
    target.chmod(0o600);shutil.copyfile(ROOT/SOURCE[7:],target);target.chmod(0o400)
    changed['files'][SOURCE]=p.sha(target)
    p.save(TARGET/'manifest.json',changed);digest=p.sha(TARGET/'manifest.json')
    launcher=TARGET/'bin/q4t';launcher.chmod(0o700)
    launcher.write_bytes(bundle.launcher(digest,changed['python']['path']));launcher.chmod(0o500)
    p.require(bundle.verify(TARGET,digest)==changed,'new bundle invalid')
    p.require({n for n,h in old['files'].items() if h!=changed['files'][n]}=={SOURCE},'file delta changed')
    p.require(old['engine_manifest_sha256']==changed['engine_manifest_sha256'],'engine drift')
    for n in old['files']:
        if n!=SOURCE:p.require(p.sha(OLD/n)==p.sha(TARGET/n),'unchanged file drift')
    for n in ('q4t','q4t.release.json','manifest.json'):
        p.require(p.sha(OLD/'engine'/n)==p.sha(TARGET/'engine'/n),'engine identity drift')
    review=dict(status='PASS_MIGRATION_CONTROL_PATCH_CPU',original_package=str(OLD),
        original_manifest_sha256=OLD_SHA,package=str(TARGET),manifest_sha256=digest,
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        delta={SOURCE:dict(before=old['files'][SOURCE],after=changed['files'][SOURCE])},
        exact_engine_and_native_bytes=True,exact_primary_light_qualification=True,
        reused_gpu_evidence=str(WORK/'managed-version-02/decision.json'),
        cpu_evidence='docs/evidence/serve-async-migration-cpu-20261113',
        new_gpu_starts=0,scope='Only qualified migration capability comparison changes; GPU model, HTTP and supervision sources unchanged. STOPPED publication checks remain required.')
    p.save(OUT/'review.json',review)
    decision=copy.deepcopy(p.read(WORK/'managed-version-02/decision.json'))
    decision.update(status='PASS_ASYNC_MIGRATION_CONTROL_PATCH',acceptance='ACCEPT_ASYNC_MIGRATION_CONTROL_PATCH',
        manifest_sha256=digest,original_service_acceptance=dict(path=str(WORK/'managed-version-02/decision.json'),sha256=p.sha(WORK/'managed-version-02/decision.json')),
        control_patch_review=str(OUT/'review.json'),control_patch_review_sha256=p.sha(OUT/'review.json'),
        actual_business_reused_unchanged_engine_and_supervision=True,new_gpu_starts=0)
    p.save(OUT/'decision.json',decision)
    release=copy.deepcopy(p.read(WORK/'managed-version-02/service-release.json'))
    release.update(acceptance=decision['acceptance'],manifest_sha256=digest,package=str(TARGET),
        acceptance_record=str(OUT/'decision.json'),acceptance_sha256=p.sha(OUT/'decision.json'),
        control_patch_review=str(OUT/'review.json'),control_patch_review_sha256=p.sha(OUT/'review.json'))
    p.save(OUT/'service-release.json',release)
    selected=version_contract.payload(dict(package=str(TARGET),manifest_sha256=digest))
    version_contract.accepted_fallback({k:dict(path=str(OUT/f),sha256=p.sha(OUT/f)) for k,f in
        [('decision','decision.json'),('release','service-release.json')]},selected)
    bundle.verify(OLD,OLD_SHA)
    print(digest)
if __name__=='__main__':main()
