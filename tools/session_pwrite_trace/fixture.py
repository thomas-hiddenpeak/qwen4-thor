"""Second and final bounded direct-I/O trace fixture, including fd reuse."""
import fcntl
import json
import mmap
import os
from pathlib import Path
import struct
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]


def main():
    out = Path(sys.argv[1]).resolve()
    out.mkdir()
    budget = json.loads((out.parent/'budget.json').read_text())
    deadline = budget['tool_deadline_unix']
    result = dict(writes=0, calls=[], files=[], status='FAILED')
    proc = None
    try:
        with (out/'collector.log').open('x') as log:
            proc = subprocess.Popen(['sudo','-n','python3','-B',
                str(ROOT/'tools/session_pwrite_trace/collect.py'),
                str(out.parent/'trace.bpf.o'), str(out), str(deadline)],
                stdout=log, stderr=subprocess.STDOUT)
            while not (out/'ready.json').exists():
                if proc.poll() is not None: raise RuntimeError('collector load')
                if time.time() >= deadline: raise TimeoutError('tool deadline')
                time.sleep(.01)
            (out/'target.json').write_text(json.dumps({'pid':os.getpid()})+'\n')
            while not (out/'target-ready.json').exists():
                if proc.poll() is not None: raise RuntimeError('collector bind')
                time.sleep(.01)
            buf = mmap.mmap(-1, 8*1024**2)
            buf[:] = b'\x5a'*len(buf)
            try:
                for file_index in range(2):
                    fd = os.open(out, os.O_TMPFILE | os.O_DIRECT | os.O_RDWR, 0o600)
                    try:
                        inode = os.fstat(fd).st_ino
                        for size,offset in [(4096,0),(131072,4096),
                                           (len(buf),135168),
                                           (len(buf),135168+len(buf))]:
                            view = memoryview(buf)[:size]
                            begin = time.monotonic_ns()
                            try: n = os.pwrite(fd, view, offset)
                            finally: view.release()
                            end = time.monotonic_ns()
                            result['writes'] += n
                            result['calls'].append(dict(pid=os.getpid(),fd=fd,
                                inode=inode,offset=offset,bytes=size,returned=n,
                                start=begin,end=end,file_index=file_index))
                            if n != size: raise RuntimeError('short write')
                        layout = bytearray(struct.pack('<QQIIII',0,2**64-1,0,0,64,0)+bytes(56*64))
                        fcntl.ioctl(fd,0xc020660b,layout,True)
                        mapped = struct.unpack_from('<I',layout,20)[0]
                        extents = [struct.unpack_from('<QQQ',layout,32+56*i)
                                   for i in range(mapped)]
                        result['files'].append(dict(fd=fd,inode=inode,
                            size=os.fstat(fd).st_size,extents=extents))
                        for item in result['calls']:
                            if item['file_index'] != file_index: continue
                            size = item['bytes']; view = memoryview(buf)[:size]
                            try:
                                view[:] = b'\x00'*size
                                if os.preadv(fd,[view],item['offset']) != size:
                                    raise RuntimeError('short read')
                                if view.tobytes() != b'\x5a'*size:
                                    raise RuntimeError('readback mismatch')
                            finally: view.release()
                    finally: os.close(fd)
            finally: buf.close()
            result['status'] = 'IO_PASSED_TRACE_REQUIRES_REVIEW'
    except BaseException as exc:
        result['error'] = repr(exc)
    finally:
        (out/'stop').touch()
        if proc:
            try: proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.terminate()
                proc.wait(timeout=10)
            result['collector_returncode'] = proc.returncode
        result['finished'] = time.time()
        (out/'fixture.json').write_text(json.dumps(result,indent=2)+'\n')
        print(json.dumps(result))


if __name__ == '__main__':
    main()
