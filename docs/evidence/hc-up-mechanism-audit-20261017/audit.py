import json,hashlib
from pathlib import Path
r=Path('.q4t-work/hc-up-mechanism-audit-20261017')
p=Path('.q4t-work/hc-up-host-observer-20261016/host-review.json');h=json.loads(p.read_text());rows={}
for n,x in h['runs'].items():
 t=x['totals'];rows[n]={'thread_cpu_s':t['cpu_ns']/1e9,'user_cycles':t['cycles'],'user_instructions':t['instructions'],'user_ipc':t['instructions']/t['cycles'],'user_branch_misses_per_1000_instructions':t['branch_misses']/t['instructions']*1000,'layer_event_s':t['layer_event_ms']/1000}
pmus={}
for name in ['nvidia_ucf_gpu_pmu_0','nvidia_ucf_pmu_0']:
 d=Path('/sys/bus/event_source/devices')/name
 pmus[name]={'type':(d/'type').read_text().strip(),'cpumask':(d/'cpumask').read_text().strip(),'format':{p.name:p.read_text().strip() for p in (d/'format').iterdir()},'events':{p.name:p.read_text().strip() for p in (d/'events').iterdir()}}
result={'source_sha256':{str(p):hashlib.sha256(p.read_bytes()).hexdigest()},'host_derived':rows,'existing_comparisons':{k:h['comparisons'][k] for k in ['cycles','instructions','branch_misses']},'scope':['Historical instrumented host stage only; not paired with ASLR stage','Counters cover current thread/user mode only; no cache, TLB, memory stall, kernel-mode or GPU PMU counters','CPU time includes user and kernel, so user cycles/thread CPU time is not a clock-frequency measurement','Old Nsight activity union does not report SM-active cycles or DRAM/L2 stalls and A timing was contaminated','Cannot subtract host-stage PMU and ASLR-stage TTFT to claim a mechanism'],'uncore_sysfs':pmus,'uncore_access':json.loads((r/'uncore-access.json').read_text()),'perf_event_paranoid':Path('/proc/sys/kernel/perf_event_paranoid').read_text().strip(),'ncu_version':'2026.2.0.0 build 37790515','new_model_requests':0,'causal_proof':False}
(r/'mechanism-review.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result['existing_comparisons'],indent=2))
