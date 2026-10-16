"""CPU-only service-manager recovery capability probe; no real clock IO."""
import array,ctypes,fcntl,json,os,signal,socket,subprocess,sys,time
from pathlib import Path
phase,mode,directory,unit=sys.argv[1:];out=Path(directory)
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'tools/evalscope'))
from gpu_clock_journal import ClockJournal

def read(): return json.loads((out/'fake-clock').read_text())
def write(field,value):
 values=read();values[0 if field=='min_freq' else 1]=value
 (out/'fake-clock').write_text(json.dumps(values)+'\n')

def controller():
 return ClockJournal(out/'lease.recovery-required',read,write,
                     {'boot':'fake-boot','device':'fake-device'},[1260,1575])

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

  sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'tools/evalscope'))
  import gpu_exclusive as guard
  # CPU capability probe: no NVIDIA query, model, or GPU workload.
  guard.inspect=lambda *args: {'conflicts': []}
  rc=guard.run_guard([sys.executable,__file__,'driver',mode,str(out),unit],out/'guard',out/'lease',
                     interval=0.01,grace=1,lease_fd=lease.fileno())
  assert rc==0,rc
elif phase=='driver':
 c=controller();c.prepare([1575,1575],[1260,1260]);c.apply()
 if mode=='kill':os.kill(os.getppid(),signal.SIGKILL)
elif phase=='cleanup':
 held=False
 with (out/'lease').open('a+') as lease:
  try:fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BlockingIOError:held=True
 before=read()
 if mode=='cleanup_kill':
  (out/'cleanup-kill.json').write_text(json.dumps({'lease_still_held':held,'before':before})+'\n')
  os.kill(os.getpid(),signal.SIGKILL)
 if mode=='restore_failure':
  c=controller()
  def fail(*args):raise OSError('injected restoration failure')
  c.write=fail
  try:c.restore()
  except OSError:pass
  else:raise AssertionError('failure injection missing')
  (out/'restore-failed.json').write_text(json.dumps({'lease_still_held':held,'before':before})+'\n')
  while not (out/'repair-authorized').exists():time.sleep(0.05)
 controller().restore()
 (out/'cleanup.json').write_text(json.dumps({'lease_still_held':held,'before':before,'after':read(),'service_result':os.environ.get('SERVICE_RESULT'),'exit_code':os.environ.get('EXIT_CODE'),'exit_status':os.environ.get('EXIT_STATUS')},indent=2)+'\n')
 assert held and before==[1260,1260]
else:raise ValueError(phase)
