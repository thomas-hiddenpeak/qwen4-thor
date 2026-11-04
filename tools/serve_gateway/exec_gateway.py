"""Acceptance child keeps parent-death handling across the production exec."""
import argparse
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.dont_write_bytecode=True
from tools.serve_service import supervisor as sup

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--parent',type=int,required=True)
    p.add_argument('--package',required=True)
    p.add_argument('--manifest-sha256',required=True)
    p.add_argument('--backend-run',required=True)
    args=p.parse_args()
    sup.arm_parent_death(sup.process_identity(args.parent))
    command=[sys.executable,'-I','-B',str(ROOT/'tools/serve_gateway/gateway.py'),
        'run','--package',args.package,'--manifest-sha256',args.manifest_sha256,
        '--backend-run',args.backend_run]
    os.execv(command[0],command)
