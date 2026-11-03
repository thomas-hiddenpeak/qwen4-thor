"""Fresh stdlib-only process: parent-death binding then pinned managed entry."""
import ctypes
import json
import os
from pathlib import Path
import signal
import sys


def identity(pid):
    fields=Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()
    return dict(pid=pid,start_ticks=int(fields[19]),
        boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip())


def main():
    folder=Path(sys.argv[1])
    launch=json.loads((folder/'launcher.json').read_text())
    owner=launch['owner']
    libc=ctypes.CDLL(None,use_errno=True)
    if libc.prctl(1,signal.SIGTERM,0,0,0)!=0:
        raise OSError(ctypes.get_errno(),'parent death binding')
    if os.getppid()!=owner['pid'] or identity(owner['pid'])!=owner:
        raise RuntimeError('launcher identity changed')
    command=launch['command']
    environment={k:v for k,v in os.environ.items() if k in ('PATH','HOME','LANG','LC_ALL')}
    os.execve(command[0],command,environment)


if __name__=='__main__':main()
