import hashlib, json, subprocess, sys
from pathlib import Path
ROOT=Path('/home/rm01/models/dev/qwen4-thor')
FROZEN=ROOT/'docs/evidence/no-mtp-calibration-design-20261018/identity.json'
def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''): h.update(chunk)
    return h.hexdigest()
def check():
    frozen=json.loads(FROZEN.read_text()); errors=[]; hashes={}
    for name,expected in frozen['hashes'].items():
        try: actual=sha(ROOT/name)
        except OSError as e: actual=str(e)
        hashes[name]=actual
        if actual!=expected: errors.append(name)
    observed={'kernel':subprocess.check_output(['uname','-a'],text=True).strip(),
      'l4t':Path('/etc/nv_tegra_release').read_text(),
      'driver':Path('/proc/driver/nvidia/version').read_text(),
      'power_mode':subprocess.check_output(['/usr/sbin/nvpmodel','-q'],text=True).strip(),
      'frequency_policies':{p:Path(p).read_text().strip() for p in frozen['frequency_policies']},
      'python_environment':json.loads(subprocess.check_output([str(ROOT/'tools/evalscope/.venv/bin/python'),'-c',
       'import sys,json; from importlib.metadata import distributions; print(json.dumps({"python":sys.version,"packages":dict(sorted((d.metadata["Name"],d.version) for d in distributions()))}))'],text=True))}
    for k,v in observed.items():
        if frozen[k]!=v: errors.append(k)
    model=Path('/home/rm01/models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream')
    inventory={str(p.relative_to(model)):{'size':p.stat().st_size,'mtime_ns':p.stat().st_mtime_ns} for p in sorted(model.rglob('*')) if p.is_file()}
    thermal={}
    for p in sorted(Path('/sys/class/thermal').glob('thermal_zone*/temp')):
        try: thermal[str(p)]=p.read_text().strip()
        except OSError: pass
    return {'status':'PASS' if not errors else 'IDENTITY_MISMATCH','mismatches':errors,'hashes':hashes,'observed':observed,'model_metadata':inventory,'thermal_snapshot':thermal,'weight_hash_note':'No full weight rehash; metadata inventory for same-phase change detection.'}
if __name__=='__main__':
    result=check(); Path(sys.argv[1]).write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({'status':result['status'],'mismatches':result['mismatches'],'hashed_files':len(result['hashes']),'model_files':len(result['model_metadata'])}))
    raise SystemExit(0 if result['status']=='PASS' else 1)
