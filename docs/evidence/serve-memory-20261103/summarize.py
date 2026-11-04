from pathlib import Path
import json
import re
import sys
ROOT = next(p for p in Path(__file__).resolve().parents if (p / 'AGENTS.md').is_file())
sys.path.insert(0, str(ROOT))
from tools.serve_memory import stage
h, base = stage.h, stage.BASE
for folder in (stage.PACKAGE, stage.OUTPUT):
    h.verify_seal(folder)
plan = h.read(stage.PACKAGE / 'plan.json')
business = h.read(stage.OUTPUT / 'business/result.json')
final = h.read(stage.OUTPUT / 'finalized.json')
begin = h.read(stage.OUTPUT / 'begin.json')
h.require(business['status'] == 'PASS_BOUNDED_MEMORY_ADMISSION', 'business not accepted')
records = []
for path in sorted((stage.OUTPUT / 'business').rglob('*-resources.json')):
    item = h.read(path)
    procs = item['memory']['processes']
    values = {}
    for pid, status in procs.items():
        match = re.search(r'^VmRSS:\s+(\d+)\s+kB$', status, re.M)
        values[pid] = int(match[1])*1024 if match else None
    available = re.search(r'^MemAvailable:\s+(\d+)\s+kB$', item['memory']['meminfo'], re.M)
    records.append(dict(source=h.identity(path), available_bytes=int(available[1])*1024 if available else None,
                        rss_bytes=values, sampling_errors=item['memory']['sampling_errors']))
rss = [v for row in records for v in row['rss_bytes'].values() if v is not None]
avail = [row['available_bytes'] for row in records if row['available_bytes'] is not None]
worker = []
for path in sorted((stage.OUTPUT / 'business').rglob('server.log')):
    for item in re.finditer(r'grammar worker reaped=(\d+) lifetime_max_rss_bytes=(\d+) reported_post_exec_rss_bytes=(\d+) wait_status=(\d+)', path.read_text()):
        worker.append(dict(source=str(path), reaped=int(item[1]), lifetime_bytes=int(item[2]),
                           post_exec_bytes=int(item[3]), wait_status=int(item[4])))
h.save(base / 'memory-summary.json', dict(sparse_samples=records, sample_count=len(records),
    sampled_rss_max_bytes=max(rss) if rss else None,
    global_available_min_bytes=min(avail) if avail else None,
    worker_reports=worker, reported_post_exec_max_bytes=max((r['post_exec_bytes'] for r in worker), default=None),
    lifetime_ru_maxrss_max_bytes=max((r['lifetime_bytes'] for r in worker), default=None),
    sampling_scope='Between business operations; no continuous trace or concurrent worker RSS captured',
    complete_process_or_system_peak=False, counters_additive=False,
    lifetime_ru_maxrss_includes_pre_exec=True))
metrics = {}
for folder, name in [('production-high-floor','after-refusals'), ('production-mtp','after-business')]:
    metrics[folder] = h.read(stage.OUTPUT / 'business' / folder / (name+'-resources.json'))['metrics']
latencies = {str(p.relative_to(stage.OUTPUT / 'business')):h.read(p)['seconds']
             for p in (stage.OUTPUT / 'business').rglob('*-elapsed.json')}
wire = h.read(stage.OUTPUT / 'business/wire-schema-result.json')
post = h.read(base / 'postflight.json')
match = re.search(r'^MemAvailable:\s+(\d+)\s+kB$',post['meminfo'], re.M)
result = dict(source_commit=plan['source_commit'], runtime_files=len(h.read(stage.PACKAGE/'candidate/runtime-source.json')),
    binary=plan['binary'], protocol_sha256=plan['protocol_sha256'], installed=final['installed'],
    business=business, limits=plan['limits'], planned=plan['planned'],
    finalized_seconds=final['time']-begin['started'], final_metrics=metrics,
    elapsed_seconds=latencies, wire_response_files=len(wire['records']),
    wire_envelopes=sum(r['envelopes'] for r in wire['records']),
    controls_and_preflight_validated_separately=True,
    postflight_time=post['time'], postflight_available_bytes=int(match[1])*1024 if match else None,
    gpu=h.read(base/'postflight-gpu.json'), source_default_rollback_preserved=True,
    native_output_limitation='19 checks: tool transcript and guard/binary/registry preserved, raw stdout/stderr not archived')
h.save(base / 'acceptance-summary.json', result)
print(json.dumps({k: result[k] for k in ['source_commit','protocol_sha256','finalized_seconds','wire_response_files','wire_envelopes','postflight_available_bytes']},indent=2))
print(json.dumps({k:h.read(base/'memory-summary.json')[k] for k in ['sample_count','sampled_rss_max_bytes','global_available_min_bytes','reported_post_exec_max_bytes','lifetime_ru_maxrss_max_bytes']},indent=2))
