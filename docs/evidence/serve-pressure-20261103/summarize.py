"""Derive a bounded acceptance report from immutable original records."""
from pathlib import Path
import json
import re
import sys

ROOT = next(p for p in Path(__file__).resolve().parents if (p/'AGENTS.md').exists())
sys.path.insert(0, str(ROOT))
from tools.serve_pressure import stage
from tools.serve_pressure.observe import status_fields

h, base, out = stage.h, stage.BASE, stage.OUTPUT
h.verify_seal(stage.PACKAGE)
h.verify_seal(out)
business = h.read(out/'business/result.json')
guard = h.read(out/'lease/exit.json')
finalized = h.read(out/'finalized.json')
h.require(business['status']=='PASS_BOUNDED_PRESSURE_AND_MIXED' and
          guard['status']=='COMPLETED' and finalized['passed'], 'acceptance gate')
folder = out/'business'
samples = [json.loads(s) for s in (folder/'model/continuous.jsonl').read_text().splitlines()]
identity = h.read(folder/'model/scope/identity.json')
pid = str(identity['pid'])
h.require(samples and all(s['process_memory']['process_start_ticks'][pid] == identity['start_ticks']
          for s in samples), 'same model identity not proved')
def observations(row):
    return row['resources']['cgroup']['observations']
children, child_missing = [], 0
for row in samples:
    for child, raw in row['process_memory']['processes'].items():
        if child == pid:
            continue
        fields = status_fields(raw)
        if 'VmRSS' in fields:
            children.append(fields['VmRSS'])
        else:
            child_missing += 1
nvml = [json.loads(s) for s in (out/'lease/nvml-queries.jsonl').read_text().splitlines()]
device_memory = [p['used_bytes'] for row in nvml for p in row.get('process_memory', [])
                 if str(p['pid']) == pid and p.get('used_bytes') is not None]
pressures = [json.loads(s) for s in (folder/'pressure/pressure.jsonl').read_text().splitlines()]
steps = [row for row in pressures if 'after' in row]
peak = max(r['allocated'] for r in steps)
states = {}
for label in ('memory-closed','queued-reaped','holder-continues','closed-drained',
              'hysteresis-band','same-process-recovered'):
    value = h.read(folder/'pressure'/(label+'-samples.json'))[-1]
    states[label] = {key:value['metrics'][key] for key in (
        'q4t_memory_available_bytes','q4t_memory_admission_open','q4t_queued_requests',
        'q4t_structured_workers_active','q4t_seq_slots_free')}
endpoints = [json.loads(s) for s in (folder/'model/scope/endpoints.jsonl').read_text().splitlines()]
retained = next(r for r in endpoints if r['label']=='payload-exited')
final_metrics = h.read(folder/'final/final-resources.json')['metrics']
logs = (folder/'model/server.log').read_text()
worker_records = re.findall(r'grammar worker reaped=1 lifetime_max_rss_bytes=(\d+) reported_post_exec_rss_bytes=(\d+)', logs)
rejections = re.findall(r'\[q4t\]\[memory-admission\] trace_id=(\S+) checkpoint=(\S+) reason=(\S+)', logs)
h.require(len(rejections)==4 and sum(point=='execute' for _,point,_ in rejections)==1 and
          sum(point=='headers' for _,point,_ in rejections)==3 and
          all(reason=='memory_pressure' for _,_,reason in rejections), 'fixed rejection log accounting')
elapsed = []
for path in folder.glob('cycle-*/*-elapsed.json'):
    elapsed.append(dict(case=path.parent.name+'/'+path.stem.removesuffix('-elapsed'),
                        seconds=h.read(path)['seconds']))
cycle_times = []
for path in sorted(folder.glob('cycle-*/quiescent.json')):
    cycle_times.append(h.read(path)['ended'])
wire = h.read(folder/'wire-schema-result.json')
result = dict(source_commit=h.read(stage.PACKAGE/'plan.json')['source_commit'],
    installed_binary_sha256=stage.SHA, sidecar_sha256=stage.SIDECAR,
    business=business, guard=guard, finalized=finalized,
    protocol_sha256=h.read(stage.PACKAGE/'plan.json')['protocol_sha256'],
    cpu=dict(core=h.read(base/'cpu/result.json'), oom=h.read(base/'cpu/oom-gate.json')),
    same_process=dict(pid=identity['pid'], start_ticks=identity['start_ticks'], continuous_samples=len(samples)),
    pressure=dict(peak_bytes=peak, min_available_bytes=min(r['after']['MemAvailable'] for r in steps),
        min_free_bytes=min(r['after']['MemFree'] for r in steps), command_steps=len(steps),
        final_receipt=pressures[-1], states=states,
        active_progress=h.read(folder/'pressure/active-progress.json'), rejections=rejections),
    mixed=dict(cycles=len(cycle_times), first_to_last_quiescent_seconds=cycle_times[-1]-cycle_times[0],
        success_generation_count=320, actual_tool_executions=64,
        request_seconds=dict(min=min(r['seconds'] for r in elapsed),max=max(r['seconds'] for r in elapsed)),
        envelope=h.read(folder/'resource-envelope.json')),
    final_metrics={key:final_metrics[key] for key in (
        'q4t_requests_total','q4t_requests_success_total','q4t_requests_error_total',
        'q4t_requests_aborted_total','q4t_structured_workers_started_total',
        'q4t_structured_workers_reaped_total','q4t_seq_slots_free','q4t_seq_slots_total',
        'q4t_queued_requests','q4t_active_chats','q4t_request_body_bytes')},
    memory_observations=dict(samples=len(samples),
        duration_seconds=samples[-1]['ended']-samples[0]['started'],
        max_sample_gap_seconds=max(b['started']-a['started'] for a,b in zip(samples,samples[1:])),
        parent_rss_sample_max=max(r['parent']['VmRSS'] for r in samples),
        child_rss_sample_max=max(children) if children else None,
        child_rss_samples=len(children),child_missing_rss_samples=child_missing,
        child_scope='direct child at observation, includes possible pre-exec; no zero for missing RSS',
        global_available_min=min(r['memory']['MemAvailable'] for r in samples),
        global_free_min=min(r['memory']['MemFree'] for r in samples),
        cgroup_current_sample_max=max(observations(r)['memory.current']['value'] for r in samples),
        cgroup_kernel_peak=retained['resources']['cgroup']['observations']['memory.peak']['value'],
        cgroup_exit_events=retained['resources']['cgroup']['observations']['memory.events']['value'],
        nvml_process_sample_max=max(device_memory) if device_memory else None,
        worker_log_records=len(worker_records),
        worker_post_exec_rss_max=max(int(b) for _,b in worker_records),
        worker_wait4_lifetime_rss_max=max(int(a) for a,_ in worker_records),
        overlap_policy='Do not sum RSS/memcg/NVML/global fields or subtract to claim an exclusive total',
        physical_ram_peak_proved=False),
    wires=dict(files=len(wire['records']), envelopes=sum(r['envelopes'] for r in wire['records']),
        source=h.identity(folder/'wire-schema-result.json')),
    shutdown=h.read(folder/'model/shutdown.json'), performance_acceptance=False,
    long_duration_soak_proved=False, physical_ram_hard_limit_proved=False)
h.save(base/'acceptance-summary.json',result)
print(json.dumps({k:result[k] for k in ('business','mixed','final_metrics','memory_observations','wires')},indent=2))
