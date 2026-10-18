from pathlib import Path
import hashlib,json,shutil,os,subprocess,datetime
R=Path('/home/rm01/models/dev/qwen4-thor'); O=R/'.q4t-work/b1a-deploy-20261018'
A='8685d8bf4068b81df0f5162d6c55924d93eae793d17664a1b465ca7587d6c6ee';B='44abd8b1a7edaca41620b87b4cf894427f8397c1e3529ef56512940ef1b44e03';C='ca2e60a10bb4c6c3d653a73437283e543797d119b5fbd7e9cdc82835af549e06'
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def save(p,x):p.write_text(json.dumps(x,indent=2)+'\n')
binary=R/'build/q4t';side=R/'build/q4t.release.json';src=R/'.q4t-work/b1a-20261017/q4t-candidate';cache=src.parent/'CMakeCache.txt'
assert sha(binary)==A and sha(src)==B and sha(cache)==C
assert not subprocess.check_output(['git','diff','1c6e792','HEAD','--','src','include','CMakeLists.txt','cmake'],cwd=R)
identity=json.loads((src.parent/'identity-before-http.json').read_text())
for name in ['config.json','model.safetensors.index.json']:
 p=Path('/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream')/name
 assert sha(p)==identity[str(p)]
backup=O/'previous';backup.mkdir(exist_ok=False)
shutil.copy2(binary,backup/'q4t');shutil.copy2(side,backup/'q4t.release.json')
package=R/'.q4t-work/releases/b1a-20261018';package.mkdir(exist_ok=False)
shutil.copy2(src,package/'q4t');shutil.copy2(cache,package/'CMakeCache.txt')
shutil.copy2(R/'docs/evidence/b1a-ab60-20261018/B-on/server-command.json',package/'validated-server-command.json')
manifest={'kind':'default executable deployment; not supervised text-v1 service qualification','binary_sha256':B,'build_cache_sha256':C,'source_commit':'1c6e7927d393cbed1a490c23344d2995c941335a','accepted_main':subprocess.check_output(['git','rev-parse','HEAD'],cwd=R,text=True).strip(),'acceptance':'ENGINEERING_ACCEPTED_WITH_TIMING_UNCERTAINTY','new_requests':0,'files':{p.name:sha(p) for p in package.iterdir() if p.is_file()},'evidence':'docs/MTP_POSITION_B1A_FINAL_REVIEW_2026-10-18.md'}
save(package/'manifest.json',manifest)
meta={'version':'mtp-position-b1a-20261018','binary_sha256':B,'package':str(package),'package_manifest_sha256':sha(package/'manifest.json'),'build_cache':str(package/'CMakeCache.txt'),'build_cache_sha256':C,'acceptance_decision':str(R/'docs/MTP_POSITION_B1A_MERGE_2026-10-18.md'),'acceptance':manifest['acceptance'],'scope':'default executable; validated single-stream MTP on/off; no new service lifecycle qualification','previous_binary':str(backup/'q4t'),'previous_release':str(backup/'q4t.release.json'),'previous_release_was_stale':True}
# Construct both files before replacement; restore old pair on any failure.
shutil.copy2(package/'q4t',R/'build/q4t.b1a-new');save(R/'build/q4t.release.b1a-new.json',meta)
try:
 os.replace(R/'build/q4t.b1a-new',binary)
 os.replace(R/'build/q4t.release.b1a-new.json',side)
 assert sha(binary)==B
 actual=json.loads(side.read_text());assert actual['binary_sha256']==sha(binary)
 assert actual['build_cache_sha256']==sha(Path(actual['build_cache']))
except BaseException:
 shutil.copy2(backup/'q4t',binary);shutil.copy2(backup/'q4t.release.json',side);raise
save(O/'deployment.json',{'status':'DEPLOYED_DEFAULT_EXECUTABLE','utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'source_sha256':B,'installed_sha256':sha(binary),'previous_sha256':sha(backup/'q4t'),'release_metadata':meta,'package_manifest':manifest,'service_started':False,'reason':'No q4t service was running; preserve stopped state','new_http_gpu_requests':0})
print(json.dumps({'status':'DEPLOYED_DEFAULT_EXECUTABLE','binary_sha256':sha(binary),'service_started':False,'new_requests':0}))
