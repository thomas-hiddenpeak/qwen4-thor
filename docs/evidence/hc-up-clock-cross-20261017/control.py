"""Frozen eight-request GPC crossover; independent service cleanup."""
import array
import ctypes
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

out = Path(__file__).resolve().parent
sys.path.insert(0,str(out.parents[1] / 'tools/evalscope'))
import gpu_exclusive as guard
from gpu_clock_io import GpcIO, snapshot
from gpu_clock_journal import ClockJournal, persist

phase,unit = sys.argv[1:]
config = json.loads((out/'config.json').read_text())
lock_path = guard.common_lock()
marker = lock_path.with_name(lock_path.name+'.recovery-required')
io = GpcIO(config['identity'],config['range'][1],marker)
controller = ClockJournal(marker,io.read,io.write,config['identity'],io.available)

def save(name,value):persist(out/name,value,create=True)

if phase == 'main':
    with lock_path.open('a+') as lease:
        fcntl.flock(lease,fcntl.LOCK_EX | fcntl.LOCK_NB)
        if os.path.lexists(marker):raise RuntimeError('existing recovery marker')
        address=os.environ['NOTIFY_SOCKET']
        address='\0'+address[1:] if address.startswith('@') else address
        with socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendmsg([b'FDSTORE=1\nFDNAME=gpu-lease\nFDPOLL=0'],[
                (socket.SOL_SOCKET,socket.SCM_RIGHTS,array.array('i',[lease.fileno()]))])
        lib=ctypes.CDLL('libsystemd.so.0')
        lib.sd_notify_barrier.argtypes=[ctypes.c_int,ctypes.c_uint64]
        lib.sd_notify_barrier.restype=ctypes.c_int
        result=lib.sd_notify_barrier(0,5000000)
        assert result>0,result
        count=subprocess.check_output(['systemctl','show',unit,
            '--property=NFileDescriptorStore','--value'],text=True).strip()
        assert count=='1',count
        # Durable before driver can create a journal or write hardware.
        save('armed.json',{'unit':unit,'fdstore':count,'marker':str(marker)})
        rc=guard.run_guard([sys.executable,__file__,'driver',unit],out/'guard',
                           lock_path,interval=1.0,grace=30,lease_fd=lease.fileno())
        raise SystemExit(rc)
elif phase == 'driver':
    import runpy
    runpy.run_path(str(out/'run.py'),run_name='__main__')
elif phase == 'cleanup':
    if not (out/'armed.json').exists():
        save('not-armed.json',{'no_recovery_attempt':True})
        raise SystemExit(0)
    try:
        with lock_path.open('r+') as probe:
            try:fcntl.flock(probe,fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:pass
            else:raise RuntimeError('manager no longer holds lease')
        if marker.exists():
            save('journal-before-restore.json',json.loads(marker.read_text()))
            controller.restore()
        final=snapshot()
        assert final['identity']==config['identity']
        assert final['range']==config['range']
        save('restored.json',{'snapshot':final,'marker_absent':not os.path.lexists(marker),
                             'service_result':os.environ.get('SERVICE_RESULT')})
    except BaseException as error:
        save('recovery-failed.json',{'error':repr(error)})
        # Hold the manager's lease until explicit intervention. No retry loop.
        while True:time.sleep(1)
else:raise ValueError(phase)
