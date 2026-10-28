"""One fixed 64 GiB direct-IO preparation, never an adaptive warmup."""
import hashlib
import json
import mmap
import os
from pathlib import Path
import time

CHUNK=8*1024**2
TOTAL=64*1024**3


def prepare(directory,task_deadline):
    directory=Path(directory)
    fs=os.statvfs(directory)
    if fs.f_bavail*fs.f_frsize<100*1024**3:
        raise RuntimeError('IO preparation requires 100 GiB free')
    path=directory/'io-preparation.data'
    started=time.time();deadline=min(started+900,task_deadline)
    fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_RDWR|os.O_DIRECT|os.O_CLOEXEC,0o600)
    stat=os.fstat(fd);rows=[];written=0;failure=None
    buffer=mmap.mmap(-1,CHUNK)
    payload=hashlib.shake_256(b'q4t-prefix-seed-fixed-IO-20261028').digest(CHUNK)
    digest=hashlib.sha256(payload).hexdigest();buffer[:]=payload;del payload
    begin=time.monotonic();last=begin
    try:
        with (directory/'io-preparation.jsonl').open('x') as log:
            while written<TOTAL:
                if time.time()>=deadline:raise TimeoutError('fixed IO preparation deadline')
                n=os.write(fd,buffer)
                if n!=CHUNK:raise OSError('short direct IO write')
                written+=n
                if written%(4*1024**3)==0:
                    now=time.monotonic();row=dict(bytes=written,segment_seconds=now-last,elapsed_seconds=now-begin)
                    rows.append(row);log.write(json.dumps(row)+'\n');log.flush();last=now
            os.fdatasync(fd)
            if time.time()>=deadline:raise TimeoutError('fixed IO sync deadline')
    except BaseException as error:
        failure=repr(error);raise
    finally:
        os.close(fd);buffer.close()
        record=dict(path=str(path),device=stat.st_dev,inode=stat.st_ino,started=started,finished=time.time(),
            bytes=written,target=TOTAL,staging_bytes=CHUNK,buffer_sha256=digest,segments=rows,failure=failure,
            counts_in_transaction=False,counts_in_stage_wall=True,steady_state_guaranteed=False)
        (directory/'io-preparation.json').write_text(json.dumps(record,indent=2)+'\n')
    return record


def release(record,directory):
    started=time.monotonic();path=Path(record['path']);stat=path.stat()
    if (stat.st_dev,stat.st_ino)!=(record['device'],record['inode']):
        raise RuntimeError('IO preparation inode changed; leave untouched')
    path.unlink()
    (Path(directory)/'io-release.json').write_text(json.dumps(dict(
        passed=True,bytes=record['bytes'],seconds=time.monotonic()-started),indent=2)+'\n')
