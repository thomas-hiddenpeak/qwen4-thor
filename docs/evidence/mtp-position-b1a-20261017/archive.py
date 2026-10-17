import hashlib,json,shutil
from pathlib import Path
root=Path('/home/rm01/models/dev/qwen4-thor')
source=Path(__file__).resolve().parent
target=root/'docs/evidence/mtp-position-b1a-20261017'
target.mkdir(parents=True,exist_ok=True)
manifest=[]
for path in sorted(source.rglob('*')):
    if path.is_file():
        manifest.append({'path':str(path.relative_to(root)),'bytes':path.stat().st_size,'sha256':hashlib.file_digest(path.open('rb'),'sha256').hexdigest()})
(target/'raw-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
for name in ['delivery-identity.json','build-delivery.log','plan.json','environment.json','contracts-identity-executed.json','build-contracts-post-http.log','identity-before-http.json','contracts-identity.json','contracts-identity-final.json','contracts.required','summary.json','performance-table.md','direct-exit.json','build.log','build-contracts.log','build-contracts-final.log','contracts.log','boundaries.log','run_phase.py','run_contracts.py','boundaries.py','analyze.py','archive.py']:
    path=source/name
    if path.exists():shutil.copy2(path,target/name)
for group in ['on','off','quality-on','quality-off','boundaries','lease']:
    (target/group).mkdir(exist_ok=True)
    for name in ['results.json','exit.json','binary.sha256','run-mode.json','server-command.json','capacity.json','plan.json','command.json']:
        path=source/group/name
        if path.exists():shutil.copy2(path,target/group/name)
    path=source/(group+'-exit.json')
    if path.exists():shutil.copy2(path,target/path.name)
