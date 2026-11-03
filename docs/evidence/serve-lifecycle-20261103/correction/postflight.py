"""Read-only postflight; verify identities, no process signals or HTTP."""
import datetime,fcntl,json,sys
from pathlib import Path
ROOT=next(p for p in Path(__file__).resolve().parents if (p/'AGENTS.md').exists());sys.path.insert(0,str(ROOT))
from tools.serve_lifecycle import repair as r,stage,observe,catalog
from tools.serve_pressure.pressure import memory
import gpu_exclusive
h=r.h
h.require(h.read(r.OUTPUT/'lease/exit.json')['status']=='COMPLETED','repair guard')
h.require(h.read(r.OUTPUT/'finalized.json')['status']=='DELIVERED','delivery gate')
identities={}
for directory in (stage.OUTPUT/'business',r.OUTPUT/'business',r.OUTPUT/'health'):
 for path in directory.glob('model-*/scope/identity.json'):
  value=h.read(path);identities[str(value['pid'])]=value['start_ticks']
  h.require(h.read(path.parent/'cleanup.json')['unit_removed'],'scope not removed')
 for path in directory.glob('*-inflight/before-signal.json'):identities.update(h.read(path)['children'])
with gpu_exclusive.common_lock().open('r') as lock:
 fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 gpu=r.a.require_idle_gpu(r.BASE/'postflight-gpu.json')
 rows=observe.child_states(identities);h.require(all(not row['running'] for row in rows),'owned process live')
 installed=h.read(r.OUTPUT/'release/binding.json')['new'];r.a.verify_hashes(ROOT/'build',installed)
 h.verify_pair(ROOT/'build/q4t',ROOT/'build/q4t.release.json',installed['q4t'])
 markers=[str(p) for p in (ROOT/'build').glob('*recovery*.json')];h.require(not markers,'pending recovery')
 history=catalog.history(ROOT/'build/q4t',ROOT/'build/q4t.release.json');h.save(r.BASE/'release-history.json',history)
 value=dict(time=datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat(),
  lock_available=True,gpu=gpu,owned_process_states=rows,owned_processes_not_live=True,
  memory=memory(),recovery_markers=markers,installed=installed,runtime_sources=199,
  history_pairs=len(history['pairs']),history_terminal=history['terminal'],atomic_hardware_isolation_proved=False)
 h.save(r.BASE/'postflight.json',value)
print(json.dumps(dict(time=value['time'],gpu=gpu['pids'],lock_available=True,MemAvailableGiB=value['memory']['MemAvailable']/1024**3,installed=installed)))
