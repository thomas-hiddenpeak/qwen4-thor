import sys,json,fcntl
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'tools/evalscope'))
import gpu_exclusive as g
from nvml_process_query import query
path=g.common_lock()
with path.open('a+') as f:
 try:
  fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
  free=True
  fcntl.flock(f,fcntl.LOCK_UN)
 except BlockingIOError: free=False
print(json.dumps(dict(nvml=query(),lock_free=free,markers=[str(p) for p in Path('build').glob('*recovery*.json')],known_gpu_commands={p:r['argv'] for p,r in g.processes().items() if g.gpu_command(r)})))
