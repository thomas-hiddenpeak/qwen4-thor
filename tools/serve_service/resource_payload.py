"""Bounded CPU-only payload for actual memcg/pids/parent-death acceptance."""
import errno
import os
from pathlib import Path
import signal
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
from tools.serve_service import resources as r,package as pkg,supervisor as sup


def main():
    output,case=Path(sys.argv[1]),sys.argv[2]
    sup.arm_parent_death(sup.process_identity(os.getppid()))
    record=pkg.read(output/'resources.json')
    r.attach(output,record['policy'])
    pkg.save(output/'payload-ready.json',dict(owner=sup.process_identity(os.getpid()),case=case,
        membership=r.membership(os.getpid())))
    until=time.monotonic()+10
    while not (output/'go.json').exists():
        pkg.require(time.monotonic()<until,'CPU barrier deadline');time.sleep(.01)
    if case=='oom':
        parent=sup.process_identity(os.getpid())
        sentinel=os.fork()
        if sentinel==0:
            sup.arm_parent_death(parent)
            while True:signal.pause()
        pkg.save(output/'sentinel.json',sup.process_identity(sentinel))
        chunks=[]
        for _ in range(32):
            chunk=bytearray(4*1024**2)
            for index in range(0,len(chunk),4096):chunk[index]=1
            chunks.append(chunk)
        raise RuntimeError('bounded memcg OOM did not occur')
    elif case=='tasks':
        children=[]
        parent=sup.process_identity(os.getpid())
        for _ in range(16):
            try:pid=os.fork()
            except OSError as error:
                if error.errno!=errno.EAGAIN:raise
                pkg.save(output/'pids-rejected.json',dict(errno=error.errno,children=children));break
            if pid==0:
                sup.arm_parent_death(parent)
                while True:signal.pause()
            children.append(sup.process_identity(pid))
        else:raise RuntimeError('pids limit did not reject bounded fork')
    elif case in ('normal','high','death'):
        chunk=bytearray((40 if case=='high' else 8)*1024**2)
        for index in range(0,len(chunk),4096):chunk[index]=7
        pkg.save(output/'payload-held.json',dict(bytes=len(chunk),checksum=chunk[0]+chunk[-4096]))
    elif case=='drift':
        r.write(Path(record['root'])/'payload/pids.max',record['policy']['tasks_max']+1)
        pkg.save(output/'limit-drift.json',dict(changed='pids.max'))
    else:raise ValueError('unknown CPU case')
    while True:signal.pause()


if __name__=='__main__':main()
