"""Seal evidence only after gpu_exclusive has drained its owned descendants."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
ROOT = Path(__file__).resolve().parents[3]


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('command',nargs=argparse.REMAINDER)
    args=p.parse_args()
    out=args.output.resolve()
    if not out.is_relative_to(ROOT/'.q4t-work'):
        raise ValueError('output must be under .q4t-work')
    command=args.command[1:] if args.command[:1]==['--'] else args.command
    if not command:raise ValueError('missing stage command')
    out.mkdir(exist_ok=False)
    started=time.time()
    guard=[sys.executable,'-B',str(ROOT/'tools/evalscope/gpu_exclusive.py'),
           '--output',str(out/'lease'),'--',*command]
    with (out/'driver.log').open('w') as log:
        result=subprocess.run(guard,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT)
    # gpu_exclusive returns only after verifying owned descendant cleanup.
    lease=json.loads((out/'lease/exit.json').read_text())
    state=dict(guard_exit=result.returncode,guard_status=lease['status'],
               started=started,finished=time.time(),command=command,
               seal_scope='after guard completion; preserves interrupted child manifests')
    (out/'seal.json').write_text(json.dumps(state,indent=2)+'\n')
    manifest={}
    for path in out.rglob('*'):
        if path.is_file():
            with path.open('rb') as f:manifest[str(path.relative_to(out))]=hashlib.file_digest(f,'sha256').hexdigest()
    (out/'sealed-files.json').write_text(json.dumps(manifest,indent=2)+'\n')
    print(json.dumps(state),flush=True)
    return result.returncode


if __name__=='__main__':sys.exit(main())
