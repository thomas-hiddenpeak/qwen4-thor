"""Owned static nginx exporter; inherited claim and parent-death signal."""
import argparse
import fcntl
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
from tools.serve_host import observer as o


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--state',type=Path,required=True)
    state=parser.parse_args().state.resolve(strict=True)
    with o.Directory(state) as directory:control=directory.json('observer.json')
    o.sup.arm_parent_death(control['owner'])
    policy=o.verify(state,control['policy_sha256'])
    fd=int(os.environ.pop('Q4T_HOST_CLAIM_FD'));info=os.fstat(fd);named=(state/'observer.lock').stat()
    o.c.require([info.st_dev,info.st_ino]==control['claim_identity']==[named.st_dev,named.st_ino],'binding_invalid')
    with (state/'observer.lock').open('r+') as probe:
        try:fcntl.flock(probe,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:pass
        else:raise o.c.Invalid('binding_invalid')
    os.set_inheritable(fd,True)
    argv=[str(state/'nginx'),'-e','stderr','-p',str(state)+'/', '-c',str(state/'nginx.conf'),'-g','daemon off;']
    os.execv(argv[0],argv)


if __name__=='__main__':main()
