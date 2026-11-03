"""Pinned nginx exec, with parent-death handling and inherited package claim."""
import argparse
import fcntl
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True;sys.path.insert(0,str(ROOT))
from tools.serve_gateway import managed as m


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',type=Path,required=True)
    args=parser.parse_args()
    control=m.read(args.run/'controller.json')
    m.sup.arm_parent_death(control['owner'])
    package=Path(control['package'])
    manifest,_=m.verify(package,control['manifest_sha256'])
    fd=int(os.environ.pop('Q4T_GATEWAY_CLAIM_FD'))
    info=os.fstat(fd)
    path=package/'gateway/runtime/managed.lock'
    m.g.require([info.st_dev,info.st_ino]==control['claim_identity']==
        [path.stat().st_dev,path.stat().st_ino],'package claim identity changed')
    with path.open('r+') as probe:
        try:fcntl.flock(probe,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:pass
        else:raise RuntimeError('inherited package claim absent')
    os.set_inheritable(fd,True)
    command=[manifest['python']['path'],'-I','-B',str(package/'source/tools/serve_gateway/gateway.py'),
        'run','--package',str(package/'gateway'),
        '--manifest-sha256',manifest['gateway']['manifest_sha256'],
        '--backend-run',control['backend_run']]
    os.execv(command[0],command)


if __name__=='__main__':main()
