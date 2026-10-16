"""CPU-only service-manager recovery capability probe; no real clock IO."""
import array,ctypes,fcntl,json,os,signal,socket,subprocess,sys
from pathlib import Path
phase,mode,directory,unit=sys.argv[1:];out=Path(directory)
if phase=='main':
 with (out/'lease').open('a+') as lease:
  fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
  address=os.environ['NOTIFY_SOCKET'];address='\0'+address[1:] if address.startswith('@') else address
  with socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM) as sock:
   sock.connect(address)
   sock.sendmsg([b'FDSTORE=1\nFDNAME=clock-probe-lease\nFDPOLL=0'],[(socket.SOL_SOCKET,socket.SCM_RIGHTS,array.array('i',[lease.fileno()]))])
  lib=ctypes.CDLL('libsystemd.so.0');lib.sd_notify_barrier.argtypes=[ctypes.c_int,ctypes.c_uint64];lib.sd_notify_barrier.restype=ctypes.c_int
  rc=lib.sd_notify_barrier(0,5000000);assert rc>0,rc
  count=subprocess.check_output(['systemctl','show',unit,'--property=NFileDescriptorStore','--value'],text=True).strip();assert count=='1',count
  (out/'armed.json').write_text(json.dumps({'fdstore_count':count,'barrier_result':rc,'mode':mode})+'\n')
  (out/'fake-clock').write_text('lower\n')
  if mode=='kill':os.kill(os.getpid(),signal.SIGKILL)
  elif mode!='normal':raise ValueError(mode)
elif phase=='cleanup':
 held=False
 with (out/'lease').open('a+') as lease:
  try:fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BlockingIOError:held=True
 before=(out/'fake-clock').read_text()
 (out/'fake-clock').write_text('original\n')
 (out/'cleanup.json').write_text(json.dumps({'lease_still_held':held,'before':before,'after':(out/'fake-clock').read_text(),'service_result':os.environ.get('SERVICE_RESULT'),'exit_code':os.environ.get('EXIT_CODE'),'exit_status':os.environ.get('EXIT_STATUS')},indent=2)+'\n')
 assert held and before=='lower\n'
else:raise ValueError(phase)
