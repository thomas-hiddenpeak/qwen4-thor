"""Reseal only the reviewed recovery migration source; engine/qualification intact."""
import copy,os,shutil,subprocess,sys
from pathlib import Path
sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from tools.serve_recovery import bundle,version_contract
from tools.serve_service import package as p
W=Path(__file__).resolve().parent;OUT=W/'package-02';OLD=W/'package-01/bundle'
NAME='tools/serve_recovery/profile_migration.py'
os.umask(0o077)
p.require(not OUT.exists() and not subprocess.check_output(['git','status','--porcelain'],cwd=ROOT),'commit review before resealing')
old=bundle.verify(OLD,p.sha(OLD/'manifest.json'));review=p.read(W/'migration-actual-review.json')
p.require(review['source_sha256']==p.sha(ROOT/NAME),'review changed')
p.require({n for n in bundle.SOURCES if p.sha(ROOT/n)!=p.sha(OLD/'source'/n)}=={NAME},'unreviewed control change')
OUT.mkdir(mode=0o700);target=OUT/'bundle';shutil.copytree(OLD,target,symlinks=True)
path=target/'source'/NAME;path.chmod(0o600);shutil.copyfile(ROOT/NAME,path);path.chmod(0o400)
manifest=copy.deepcopy(old);manifest['files']['source/'+NAME]=p.sha(path)
p.save(target/'manifest.json',manifest);digest=p.sha(target/'manifest.json')
launch=target/'bin/q4t';launch.chmod(0o700);launch.write_bytes(bundle.launcher(digest,manifest['python']['path']));launch.chmod(0o500)
p.require(bundle.verify(target,digest)==manifest,'resealed source invalid')
p.require({n for n,h in old['files'].items() if manifest['files'][n]!=h}=={'source/'+NAME},'unexpected payload changes')
p.require(p.sha(target/'engine/manifest.json')==p.sha(OLD/'engine/manifest.json') and manifest['recovery_profiles']==old['recovery_profiles'],'engine or qualification changed')
root=dict(package=str(target),manifest_sha256=digest)
fallback_path=W/'retained-01/bundle';fallback=dict(package=str(fallback_path),manifest_sha256=p.sha(fallback_path/'manifest.json'))
permit=version_contract.compatible(version_contract.payload(root),version_contract.payload(fallback))
p.require(permit['automatic'],'new package fallback not compatible')
p.save(OUT/'receipt.json',root)
prior=p.read(W/'package-01/prepare-review.json')
prior.update(package_sha256=digest,control_source_delta=[NAME],migration_review=review,
 source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
 predecessor_package=dict(package=str(OLD),manifest_sha256=p.sha(OLD/'manifest.json')),
 exact_engine_helpers_and_qualification_unchanged=True,version_composition_pending=True)
p.save(OUT/'prepare-review.json',prior)
print(digest)
