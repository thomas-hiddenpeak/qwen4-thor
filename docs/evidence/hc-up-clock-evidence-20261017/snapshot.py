"""One read-only availability snapshot, not historical request telemetry."""
import datetime,json,subprocess
from pathlib import Path
OUT=Path(__file__).resolve().parent
paths=['/etc/nv_tegra_release','/sys/devices/system/cpu/cpufreq/policy0/scaling_cur_freq','/sys/devices/system/cpu/cpufreq/policy0/cpuinfo_cur_freq','/sys/class/devfreq/bwmgr/cur_freq','/sys/class/devfreq/gpu-gpc-0/cur_freq','/sys/class/devfreq/gpu-nvd-0/cur_freq','/sys/kernel/debug/bpmp/debug/emc/stats','/sys/kernel/debug/bpmp/debug/emc/over_temp_state']
result={'utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'scope':'Current read-only availability; not simultaneous or historical request evidence','values':{}}
for path in paths:
 run=subprocess.run(['sudo','-n','cat',path],capture_output=True,text=True,timeout=5)
 result['values'][path]={'returncode':run.returncode,'stdout':run.stdout,'stderr':run.stderr}
result['unverified']=['EMC cumulative time units/reset behavior/wraparound','bwmgr and GPU cur_freq actual hardware versus software-state semantics','whether any historical request experienced throttling','no historical cpuinfo_cur_freq or EMC stats endpoint pairs']
result['official_reference']='https://docs.nvidia.com/jetson/archives/r39.2.1/DeveloperGuide/SD/PlatformPowerAndPerformance/JetsonThor.html'
(OUT/'snapshot.json').write_text(json.dumps(result,indent=2)+'\n')
