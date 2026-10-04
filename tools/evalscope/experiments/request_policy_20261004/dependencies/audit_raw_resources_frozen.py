"""Frozen, offline resource audit for four completed offload groups.

PREPARED ONLY. Run only after root releases the GPU and both matrices finish.
No subprocess, live /proc/sysfs, model payload, cache advice, or GPU call.
--self-test runs only embedded synthetic counter/window examples, when allowed.
The 5 s window and 10 ms clock limits qualify resource-count interpretation
only; they never change the frozen performance GO/NO_GO rules.
"""
import argparse
from bisect import bisect_left, bisect_right
from collections import Counter
import csv
from datetime import datetime, timezone
import hashlib
import itertools
import json
import math
from pathlib import Path
import statistics

ROOT = Path(__file__).resolve().parent
GROUPS = ('pilot-off', 'pilot-on', 'matrix-off', 'matrix-on')
LENGTHS = (1024, 4096, 8192, 45056, 204800, 261887)
MAX_FILE_BYTES = 256 << 20
MAX_TOTAL_BYTES = 768 << 20
MAX_ROWS = 40000
MAX_BOUNDARY_UNCERTAINTY_S = 5.0


def require(value, message):
    if not value:
        raise ValueError(message)


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def signature(path):
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


class Sources:
    """One sequential read/hash per raw source; no second raw-file scan."""
    def __init__(self, root):
        self.root = root.resolve()
        self.records = {}
        self.total_bytes = 0

    def lines(self, path):
        path = Path(path).resolve()
        require(path.is_relative_to(self.root), 'source outside evidence root')
        require(str(path) not in self.records, 'source consumed twice: ' + str(path))
        before = signature(path)
        require(before[2] <= MAX_FILE_BYTES, 'source exceeds frozen file bound')
        require(self.total_bytes + before[2] <= MAX_TOTAL_BYTES, 'audit read bound exceeded')
        digest, count, complete = hashlib.sha256(), 0, False
        record = dict(sha256=None, bytes_consumed=0, complete_file=False,
                      stat_before=before, stat_after=None)
        self.records[str(path)] = record
        try:
            with path.open('rb') as stream:
                for line in stream:
                    require(count+len(line)<=MAX_FILE_BYTES and
                            self.total_bytes+len(line)<=MAX_TOTAL_BYTES,
                            'stream exceeded frozen read bounds')
                    count += len(line)
                    self.total_bytes += len(line)
                    digest.update(line)
                    record.update(sha256=digest.hexdigest(), bytes_consumed=count)
                    yield line.decode('utf-8')
            complete = True
        finally:
            after = signature(path)
            record.update(
                sha256=digest.hexdigest(), bytes_consumed=count,
                complete_file=complete and count == before[2],
                stat_before=before, stat_after=after)
            require(before == after, 'source changed during read: ' + str(path))

    def raw(self, path):
        return ''.join(self.lines(path)).encode('utf-8')

    def json(self, path):
        return json.loads(self.raw(path))

    def jsonl(self, path):
        for line in self.lines(path):
            require(bool(line.strip()), 'blank JSONL row')
            yield json.loads(line)

    def stable(self):
        for path, record in self.records.items():
            require(record['complete_file'], 'source was not fully consumed: ' + path)
            require(signature(Path(path)) == tuple(record['stat_after']),
                    'source metadata changed after read: ' + path)


def completion_gate(source):
    """All four completion/cleanup records precede ANY raw/CSV read."""
    records = {}
    for name in GROUPS:
        directory = source.root / name
        wrapper = source.json(directory / 'wrapper-exit.json')
        http = source.json(directory / 'http/exit.json')
        cleanup = source.json(directory / 'http/isolation/cleanup.json')
        runner = source.json(directory / 'runner-process-group.json')
        require(wrapper['runner_rc'] == wrapper['monitor_rc'] == 0 and
                not wrapper['failure'] and wrapper['cleanup_failed'] is False and
                wrapper['unit_after_cleanup']['LoadState'] == 'not-found',
                name + ': wrapper incomplete or unclean')
        require(http['server'] == 0 and http['http_output_checks_passed'] is True and
                not http['failure'] and not http['cleanup_failure'] and
                http['completed'] == (1 if name.startswith('pilot') else 6),
                name + ': HTTP completion unavailable')
        if name.startswith('matrix'):
            require(http['full_offload_matrix_completed'] is True and
                    wrapper['full_offload_matrix_completed'] is True,
                    name + ': full matrix not complete')
        require(cleanup['unit_removed'] is True and cleanup['stop_rc'] == 0 and
                cleanup['properties_after']['LoadState'] == 'not-found',
                name + ': owned unit not cleaned')
        require(runner['cleanup_complete'] is True and runner['runner_reaped'] is True and
                not runner['after_cleanup']['live_pids'] and not runner['after_cleanup']['errors'],
                name + ': runner/client cleanup incomplete')
        records[name] = dict(wrapper=wrapper, http=http, cleanup=cleanup, runner=runner)
    return records


def location(row):
    return {k: row.get(k) for k in ('sequence', 'phase', 'start_t', 'end_t')}


def identity_key(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')) if value is not None else None


def cell(value, identity, error=None):
    if type(value) is not int or value < 0 or identity is None:
        return dict(value=None, identity=identity, error=error or 'missing_counter_or_identity')
    return dict(value=value, identity=identity, error=error)


def metric_cells(row, pid, ticks, expected_path):
    """Only root PID accounting; descendants and parent devices are not summed."""
    result = {}
    proc = next((p for p in row['processes'] if p['pid'] == pid), {})
    actual = proc.get('identity')
    pid_ok = (actual == {'pid': pid, 'start_ticks': ticks} and
              proc.get('consistent_identity') is True and
              row['binding']['expected_matches_actual'] is True and
              row['binding']['actual_cgroup_path'] == expected_path)
    io = proc.get('io', {})
    for field in ('read_bytes', 'rchar'):
        result['pid_' + field] = cell(
            (io.get('value') or {}).get(field), identity_key(actual) if pid_ok else None,
            io.get('error') or ('root_identity_or_binding_unavailable' if not pid_ok else None))
    cg = row.get('cgroup') or {}
    cg_ok = (cg.get('consistent_identity') is True and cg.get('identity') is not None
             and cg.get('path') == expected_path)
    cg_id = identity_key([cg.get('path'), cg.get('identity')]) if cg_ok else None
    observation = cg.get('observations', {}).get('io.stat', {})
    for device, counters in (observation.get('value') or {}).items():
        result['cg_rbytes:' + device] = cell(counters.get('rbytes'), cg_id, observation.get('error'))
    for device, item in row['model_devices']['devices'].items():
        dev_id = item['identity']['value']
        key = identity_key([device, item['sysfs_path'], dev_id]) if dev_id else None
        result['device_read_bytes:' + device] = cell(
            (item['stat']['value'] or {}).get('read_bytes'), key,
            item['stat']['error'] or item['identity']['error'])
    return result


def window_stats(windows):
    if not windows:
        return dict(count=0, max_duration_seconds=None, max_start_gap_seconds=None)
    durations = [b-a for a,b in windows]
    starts = [windows[i][0]-windows[i-1][0] for i in range(1,len(windows))]
    holes = [windows[i][0]-windows[i-1][1] for i in range(1,len(windows))]
    return dict(count=len(windows), min_duration_seconds=min(durations),
                median_duration_seconds=statistics.median(durations),
                max_duration_seconds=max(durations),
                max_start_gap_seconds=max(starts) if starts else None,
                max_unobserved_gap_seconds=max(holes) if holes else None,
                windows_ordered=all(b>=a for a,b in windows) and all(g>=0 for g in holes))


def select_brackets(samples, start, end):
    """Four fully-outside/inside windows, selected without skipping missing data."""
    if not (finite(start) and finite(end) and start < end and samples):
        return None, 'invalid_client_envelope'
    starts, ends = [s['start_t'] for s in samples], [s['end_t'] for s in samples]
    if any(not finite(a) or not finite(b) or b < a for a,b in zip(starts, ends)) or any(
            starts[i] < ends[i-1] for i in range(1,len(samples))):
        return None, 'resource_windows_overlap_or_clock_not_monotonic'
    outer_left = bisect_right(ends,start)-1
    outer_right = bisect_left(starts,end)
    inner_left = bisect_left(starts,start)
    inner_right = bisect_right(ends,end)-1
    indices = (outer_left,inner_left,inner_right,outer_right)
    if not (0<=outer_left<=inner_left<inner_right<=outer_right<len(samples)):
        return None, 'no_complete_inner_and_outer_brackets'
    uncertainty = max(start-starts[outer_left], ends[inner_left]-start,
                      end-starts[inner_right], ends[outer_right]-end)
    if uncertainty > MAX_BOUNDARY_UNCERTAINTY_S:
        return None, 'boundary_uncertainty_exceeds_frozen_5_seconds'
    return dict(indices=indices, boundary_uncertainty_seconds=uncertainty,
                outer_before=location(samples[outer_left]),inner_after_start=location(samples[inner_left]),
                inner_before_end=location(samples[inner_right]),outer_after=location(samples[outer_right]),
                before_outside_seconds=start-ends[outer_left],
                after_outside_seconds=starts[outer_right]-end), None


def span_delta(samples, left, right, metric):
    previous = None
    for row in samples[left:right+1]:
        value = row['metrics'].get(metric)
        if value is None or value['value'] is None or value['identity'] is None:
            return dict(value=None,reason='missing_counter_or_identity',at=location(row),
                        detail=value.get('error') if value else 'metric_absent')
        if previous and value['identity'] != previous['identity']:
            return dict(value=None,reason='identity_changed',at=location(row))
        if previous and value['value'] < previous['value']:
            return dict(value=None,reason='counter_decreased_or_reset',at=location(row))
        previous = value
    return dict(value=samples[right]['metrics'][metric]['value']-
                      samples[left]['metrics'][metric]['value'],reason=None)


def bounded_delta(samples, bracket, metric):
    if bracket is None:
        return dict(status='UNKNOWN',lower_bytes=None,upper_bytes=None,reason='no_reliable_windows')
    lo,li,ri,ro = bracket['indices']
    outer = span_delta(samples,lo,ro,metric)
    inner = span_delta(samples,li,ri,metric)
    if outer['value'] is None or inner['value'] is None:
        return dict(status='UNKNOWN',lower_bytes=None,upper_bytes=None,outer=outer,inner=inner)
    require(0<=inner['value']<=outer['value'], 'counter bracket algebra inconsistent')
    return dict(status='BOUNDED',lower_bytes=inner['value'],upper_bytes=outer['value'],
                includes_background=(metric.startswith('device_')))


def scalar(text, cast=float):
    return None if text == '' else cast(text)


def boolean(text):
    require(text in ('True','False'), 'invalid CSV boolean')
    return text == 'True'


def kb_peaks(fields, observed):
    # monitor.summarize preserves every KiB column, including all-unknown None.
    # Its peaks_bytes deliberately omits those unknown entries.
    return {key:observed.get(key) for key in fields if key.endswith('_kb')}


def read_samples(source, directory, summary, expected_path):
    pid,ticks = summary['root_pid'],summary['root_start_ticks']
    samples, csv_windows, gpu_windows, cache_windows = [],[],[],[]
    counts, unknown_cg, phases, status_counts = Counter(),Counter(),Counter(),Counter()
    pid_unknown, cg_unknown_nonpsi, identity_findings, delta_findings = [],[],[],[]
    missing_groups = {}
    cg_paths,cg_generations,root_generations = set(),set(),set()
    resource_peaks = {k:None for k in ('memory.current','memory.peak','memory.swap.current')}
    scalar_peaks = {}
    device_specs, device_missing = {},0
    event_maxima = {'memory.events':{},'memory.swap.events':{}}
    limit_values = {'memory.max':set(),'memory.swap.max':set()}
    swap_peak = None
    last_live_ref=final_ref=None
    checks, cache_endpoints = [],[]

    def issue(kind,row,**extra):
        if len(identity_findings)<256:
            identity_findings.append(dict(kind=kind,**location(row),**extra))
        counts['identity_finding_count']+=1

    resource_lines=source.jsonl(directory/'memory/resource-samples.jsonl')
    csv_rows=csv.DictReader(source.lines(directory/'memory/memory.csv'))
    csv_fields=csv_rows.fieldnames
    require(csv_fields is not None, 'CSV header missing')
    for number,pair in enumerate(itertools.zip_longest(resource_lines,csv_rows)):
        row,csvrow=pair
        require(row is not None and csvrow is not None, 'raw/CSV row count differs')
        require(number<MAX_ROWS and row['sequence']==number, 'row bound or sequence invalid')
        counts['sample_count']+=1;phases[row['phase']]+=1
        start,end=scalar(csvrow['t']),scalar(csvrow['sample_end_t'])
        require(all(finite(v) for v in (start,end,row['start_t'],row['end_t'])), 'nonfinite time')
        checks.append(row['phase']==csvrow['phase'] and row['memory_sample_start_t']==start and
                      start<=row['start_t']<=row['end_t']<=end and
                      math.isclose(end-start,float(csvrow['sample_duration_seconds']),abs_tol=1e-6))
        csv_windows.append((start,end))
        root=scalar(csvrow['root'],int);csvticks=scalar(csvrow['root_start_ticks'],int)
        if root:
            counts['target_sample_count']+=1
            if root!=pid or csvticks!=ticks: issue('CSV_root_identity',row,root=root,start_ticks=csvticks)
        elif csvticks not in (None,ticks): issue('CSV_exit_start_ticks_changed',row,start_ticks=csvticks)
        binding=row['binding']
        counts['binding_mismatch_samples']+=binding['status']=='mismatch'
        if binding['status']=='mismatch': issue('live_cgroup_binding_mismatch',row,binding=binding)
        if binding['status']=='bound_to_live_pid':
            counts['live_pid_sample_count']+=1
            if (binding['actual_cgroup_path']!=expected_path or
                    binding['expected_cgroup_path']!=expected_path or
                    binding['expected_matches_actual'] is not True):
                issue('bound_row_has_wrong_cgroup_path',row,binding=binding)
        if root and binding['root_pid']!=root: issue('raw_CSV_root_mismatch',row,binding=binding)
        for proc in row['processes']:
            if proc['pid']==pid and proc['identity'] is not None:
                root_generations.add(identity_key(proc['identity']))
                if proc['identity']!={'pid':pid,'start_ticks':ticks}: issue('PID_generation_changed',row,identity=proc['identity'])
            if proc['io']['value'] is None:
                counts['process_io_unknown_observations']+=1
                if len(pid_unknown)<256:
                    pid_unknown.append(dict(**location(row),pid=proc['pid'],io_error=proc['io']['error'],
                        identity_error=proc['identity_error'],identity=proc['identity'],
                        binding=binding,csv_root=root))
        cg=row.get('cgroup') or {};obs=cg.get('observations',{})
        if cg:
            cg_paths.add(cg['path'])
            if cg.get('identity') is not None: cg_generations.add(identity_key(cg['identity']))
            if cg['path']!=expected_path: issue('cgroup_path_changed',row,path=cg['path'])
        for key,observation in obs.items():
            value=observation['value']
            if value is None:
                unknown_cg[key]+=1
                missing_key=json.dumps([key,row['phase'],observation['error']],sort_keys=True)
                if missing_key not in missing_groups:
                    missing_groups[missing_key]=dict(field=key,phase=row['phase'],error=observation['error'],
                        count=0,first=location(row),last=location(row))
                missing_groups[missing_key]['count']+=1;missing_groups[missing_key]['last']=location(row)
                if key not in ('memory.pressure','io.pressure') and len(cg_unknown_nonpsi)<512:
                    cg_unknown_nonpsi.append(dict(field=key,**location(row),error=observation['error'],
                        cgroup_identity_error=cg.get('identity_error')))
            if key in resource_peaks and cg.get('consistent_identity') and type(value) is int:
                resource_peaks[key]=max(resource_peaks[key],value) if resource_peaks[key] is not None else value
            if cg.get('consistent_identity') and value is not None:
                if key in limit_values: limit_values[key].add(value)
                if key=='memory.swap.peak': swap_peak=max(swap_peak,value) if swap_peak is not None else value
                if key in event_maxima:
                    for counter,number_value in value.items():
                        event_maxima[key][counter]=max(event_maxima[key].get(counter,number_value),number_value)
        for device,item in row['model_devices']['devices'].items():
            spec={k:item[k] for k in ('device','filesystem_dev','sysfs_path','partition_parent_device')}
            if device in device_specs and device_specs[device]!=spec: issue('device_spec_changed',row,device=device)
            device_specs.setdefault(device,spec)
            device_missing+=item['stat']['value'] is None
        metrics=metric_cells(row,pid,ticks,expected_path)
        compact=dict(**location(row),metrics=metrics)
        if samples:
            old=samples[-1]
            for metric in metrics.keys()|old['metrics'].keys():
                a,b=old['metrics'].get(metric),metrics.get(metric)
                reason=None
                if a and b and a['value'] is not None and b['value'] is not None:
                    if a['identity']!=b['identity']: reason='identity_changed'
                    elif b['value']<a['value']: reason='counter_decreased_or_reset'
                if reason:
                    counts['counter_discontinuity_count']+=1
                    if len(delta_findings)<256: delta_findings.append(dict(metric=metric,reason=reason,**location(row)))
        samples.append(compact)
        ref=dict(**location(row),binding=binding,cgroup_path=cg.get('path'),cgroup_identity=cg.get('identity'),
                 cgroup_consistent_identity=cg.get('consistent_identity'),
                 memory_current_bytes=obs.get('memory.current',{}).get('value'),
                 memory_peak_bytes=obs.get('memory.peak',{}).get('value'))
        if binding['status']=='bound_to_live_pid': last_live_ref=ref
        final_ref=ref
        gpu=scalar(csvrow['gpu_bytes'],int);fresh=boolean(csvrow['gpu_fresh'])
        status_counts[csvrow['gpu_status']]+=1
        if root:
            counts['gpu_unknown_samples']+=gpu is None
            counts['gpu_scheduled_skip_samples']+=csvrow['gpu_status']=='scheduled_skip'
            counts['gpu_observed_samples']+=fresh
            counts['gpu_failed_observations']+=fresh and gpu is None
        if not fresh and gpu is not None: issue('stale_GPU_value_on_skip',row)
        if not fresh and (csvrow['gpu_start_t'] or csvrow['gpu_end_t']): issue('stale_GPU_window_on_skip',row)
        if csvrow['gpu_status']=='scheduled_skip' and fresh: issue('fresh_GPU_marked_skip',row)
        if fresh:
            ga,gb=scalar(csvrow['gpu_start_t']),scalar(csvrow['gpu_end_t'])
            require(finite(ga) and finite(gb), 'fresh GPU observation has no finite window')
            checks.append(start<=ga<=gb<=end);gpu_windows.append((ga,gb))
        cache_fresh=boolean(csvrow['model_file_cache_fresh'])
        counts['model_file_cache_observed_samples']+=cache_fresh
        counts['model_file_cache_live_observed_samples']+=bool(root and cache_fresh)
        if root and (cache_fresh or csvrow['model_file_cache_bytes']!='' or
                     csvrow['model_file_cache_known_bytes']!=''):
            issue('live_file_cache_observed_under_endpoints_protocol',row)
        if cache_fresh:
            ca,cb=scalar(csvrow['model_file_cache_start_t']),scalar(csvrow['model_file_cache_end_t'])
            require(finite(ca) and finite(cb), 'fresh cache observation has no finite window')
            checks.append(start<=ca<=cb<=end);cache_windows.append((ca,cb))
            cache_endpoints.append(dict(t=start,phase=row['phase'],
                model_file_cache_bytes=scalar(csvrow['model_file_cache_bytes'],int),
                model_file_cache_known_bytes=scalar(csvrow['model_file_cache_known_bytes'],int),
                model_file_cache_start_t=ca,model_file_cache_end_t=cb,
                model_file_cache_status=csvrow['model_file_cache_status']))
        for key,text in csvrow.items():
            if key.endswith(('_kb','_bytes')) and text!='' and (root or not key.startswith('model_file_cache')):
                value=int(text)
                scalar_peaks[key]=max(scalar_peaks.get(key,value),value)
    require(samples, 'empty raw sampling')
    saved=summary['resource_observations']
    summary_checks={key:counts[key]==summary[key] for key in (
        'sample_count','target_sample_count','gpu_unknown_samples','gpu_scheduled_skip_samples',
        'gpu_observed_samples','gpu_failed_observations','model_file_cache_observed_samples',
        'model_file_cache_live_observed_samples')}
    for key in ('sample_count','live_pid_sample_count','binding_mismatch_samples','process_io_unknown_observations'):
        summary_checks['resource.'+key]=counts[key]==saved[key]
    summary_checks.update({
        'resource.cgroup_unknown':dict(unknown_cg)==saved['cgroup_unknown_observations'],
        'resource.device_unknown':device_missing==saved['model_device_stat_unknown_observations'],
        'resource.peaks':resource_peaks==saved['observed_cgroup_counter_peaks_bytes'],
        'resource.paths':sorted(cg_paths)==saved['cgroup_paths'],
        'resource.last_live':last_live_ref==saved['last_live_pid_sample'],
        'resource.final':final_ref==saved['final_sample'],
        'gpu_peak':scalar_peaks.get('gpu_bytes')==summary['gpu_peak_bytes'],
        'cache_endpoint_rows':cache_endpoints==summary['model_file_cache_endpoint_observations'],
        'live_cache_peak':scalar_peaks.get('model_file_cache_bytes')==summary['model_file_cache_live_peak_bytes'],
        'peaks_kb':kb_peaks(csv_fields,scalar_peaks)==summary['peaks_kb'],
        'peaks_bytes':{k:v*1024 if k.endswith('_kb') else v for k,v in scalar_peaks.items()}==summary['peaks_bytes'],
        'CSV_raw_windows_and_phases':all(checks)})
    return samples,dict(summary_checks=summary_checks,all_summary_checks_match=all(summary_checks.values()),
        counts=dict(counts),phases=dict(phases),gpu_status_counts=dict(status_counts),
        window_statistics=dict(resource=window_stats([(r['start_t'],r['end_t']) for r in samples]),
            full_CSV_sample=window_stats(csv_windows),GPU_fresh=window_stats(gpu_windows),file_cache=window_stats(cache_windows)),
        process_io_unknown_exact=pid_unknown,process_io_unknown_records_truncated=counts['process_io_unknown_observations']>len(pid_unknown),
        cgroup_unknown_nonpsi_exact=cg_unknown_nonpsi,
        cgroup_unknown_nonpsi_records_truncated=(sum(v for k,v in unknown_cg.items()
            if k not in ('memory.pressure','io.pressure'))>len(cg_unknown_nonpsi)),
        cgroup_unknown_phase_reason_groups=list(missing_groups.values()),
        PSI_status='UNKNOWN' if unknown_cg['memory.pressure'] or unknown_cg['io.pressure'] else 'OBSERVED',
        identity_findings=identity_findings,counter_discontinuities=delta_findings,
        identity_findings_truncated=counts['identity_finding_count']>len(identity_findings),
        counter_discontinuities_truncated=counts['counter_discontinuity_count']>len(delta_findings),
        identity_contracts_passed=(counts['identity_finding_count']==0 and
                                   len(root_generations)==len(cg_generations)==1),
        root_generations=sorted(root_generations),cgroup_generations=sorted(cg_generations),
        cgroup_paths=sorted(cg_paths),device_specs=device_specs,
        model_device_scope_known=bool(device_specs),
        observed_limit_values={k:sorted(v,key=str) for k,v in limit_values.items()},
        memory_and_swap_event_maxima=event_maxima,observed_swap_peak_bytes=swap_peak,
        raw_resource_peaks_bytes=resource_peaks,sampled_NVIDIA_peak_bytes=scalar_peaks.get('gpu_bytes'),
        last_live=last_live_ref,final_sample=final_ref,
        per_file_cache_sidecar_not_recomputed=True)


def controller_delta(before,after,metric,pid,ticks,path):
    if before is None or after is None:
        return dict(value=None,reason='controller_endpoint_missing')
    values=[]
    for endpoint in (before,after):
        row=endpoint['resources']
        values.append(dict(sequence=endpoint['label'],phase=endpoint['label'],start_t=row['start_t'],
                           end_t=row['end_t'],metrics=metric_cells(row,pid,ticks,path)))
    if not (values[0]['start_t']<=values[0]['end_t']<=values[1]['start_t']<=values[1]['end_t']):
        return dict(value=None,reason='controller_windows_not_ordered')
    value=span_delta(values,0,1,metric)
    value['scope']='two controller snapshots; all q4t PID I/O, not expert-only reads'
    return value


def audit_group(source,name,completion):
    directory=source.root/name
    summary=source.json(directory/'memory/memory-peak.json')
    protocol=source.json(directory/'protocol.json')
    identity=source.json(directory/'http/isolation/identity.json')
    require(summary['sampling_complete'] is True and summary['stop_reason']=='target_exited' and
            summary['prelaunch_sample_present'] is True, name+': incomplete monitor lifecycle')
    pid,ticks=summary['root_pid'],summary['root_start_ticks']
    require(pid==identity['pid'] and int(identity['properties']['MainPID'])==pid,
            name+': initial PID identity mismatch')
    path='/sys/fs/cgroup'+identity['properties']['ControlGroup']
    require(path=='/sys/fs/cgroup/system.slice/'+protocol['unit'], 'unexpected cgroup binding')
    require(protocol['monitor']['file_cache_mode']=='endpoints', 'different cache protocol')
    for filename in ('monitor_memory.py','resource_metrics.py'):
        raw=source.raw(directory/'tools'/filename)
        require(hashlib.sha256(raw).hexdigest()==protocol['tool_sha256'][filename], 'snapshot SHA mismatch')
    endpoints=list(source.jsonl(directory/'http/isolation/endpoints.jsonl'))
    named={row['label']:row for row in endpoints}
    require(len(named)==len(endpoints), 'duplicate controller endpoint label')
    samples,inspection=read_samples(source,directory,summary,path)
    metrics=sorted(set().union(*(row['metrics'].keys() for row in samples)))
    io_metrics=[k for k in metrics if k.startswith(('pid_','cg_rbytes:','device_read_bytes:'))]
    lengths=(45056,) if name.startswith('pilot') else LENGTHS
    require(protocol['lengths']==list(lengths) and protocol['repeats']==3, 'unexpected request scope')
    requests=[]
    for length in lengths:
        boundaries=source.json(directory/f'http/context-{length}/request-boundaries.json')
        require(len(boundaries)==3, 'expected exactly three client envelopes')
        for index,boundary in enumerate(boundaries,1):
            label=f'context-{length}:run{index}'
            client=source.json(directory/f'http/context-{length}/run-{index}/client-exit.json')
            before,after=boundary['client_before'],boundary['client_after']
            require(before==client['before'] and after==client['after'] and client['returncode']==0 and
                    boundary['request_group']==label and boundary['request_index']==index,
                    'request/controller envelope differs')
            start,end=before['unix_seconds'],after['unix_seconds']
            mono_duration=after['monotonic_seconds']-before['monotonic_seconds']
            clock_drift=abs((end-start)-mono_duration)
            bracket,reason=select_brackets(samples,start,end)
            # A wall-clock jump cannot be aligned reliably to resource time.time.
            if clock_drift>0.01:
                bracket,reason=None,'wall_monotonic_offset_drift_over_10ms'
            bounded={metric:bounded_delta(samples,bracket,metric) for metric in io_metrics}
            first=named.get('before-'+label);last=named.get('after-'+label)
            exact={metric:controller_delta(first,last,metric,pid,ticks,path)
                   for metric in io_metrics if not metric.startswith('device_')}
            pid_value=exact['pid_read_bytes']['value']
            cg_compare={metric:dict(delta=value['value'],pid_delta=pid_value,
                exactly_agrees=(value['value']==pid_value if value['value'] is not None and pid_value is not None else None),
                plateau_with_positive_PID=(value['value']==0 and pid_value>0 if value['value'] is not None and pid_value is not None else None))
                for metric,value in exact.items() if metric.startswith('cg_rbytes:')}
            device_compare={}
            pid_bounds=bounded['pid_read_bytes']
            for metric,value in bounded.items():
                if not metric.startswith('device_'): continue
                known=value['status']==pid_bounds['status']=='BOUNDED'
                device_compare[metric]=dict(
                    intervals_overlap=(max(value['lower_bytes'],pid_bounds['lower_bytes'])<=
                                       min(value['upper_bytes'],pid_bounds['upper_bytes']) if known else None),
                    exact_controller_PID_within_device_bracket=(value['lower_bytes']<=pid_value<=value['upper_bytes']
                        if value['status']=='BOUNDED' and pid_value is not None else None),
                    whole_device_can_be_attributed_to_q4t=False,
                    interpretation='Magnitude comparison only; global device includes other workloads and PID includes all q4t reads')
            requests.append(dict(label=label,input_tokens=length,client_envelope=[start,end],
                wall_vs_monotonic_drift_seconds=clock_drift,bracket=bracket,bracket_unknown_reason=reason,
                sample_counter_bounds=bounded,controller_counter_deltas=exact,
                cgroup_vs_PID=cg_compare,device_vs_PID=device_compare,
                scope='Client process envelope, including launch/teardown; not an exact HTTP-only interval'))
    final=endpoints[-1]
    require(final['label']=='before_unit_cleanup', 'final retained cgroup endpoint missing')
    obs=final['resources']['cgroup']['observations']
    limit=obs['memory.max']['value'];peak=obs['memory.peak']['value']
    cg_agreements=[value['exactly_agrees'] for row in requests for value in row['cgroup_vs_PID'].values()]
    return dict(completion=completion,initial_identity=identity,source_protocol=protocol['monitor'],
        binary_sha256_recorded=protocol['binary_sha256'],inspection=inspection,requests=requests,
        memory=dict(memory_max_bytes=limit,charge_peak_bytes=peak,
            actual_charge_peak_minus_max_bytes=(peak-limit if type(peak) is int and type(limit) is int else None),
            final_memory_events=obs['memory.events'],final_swap_current=obs['memory.swap.current'],
            final_swap_peak=obs['memory.swap.peak'],final_swap_max=obs['memory.swap.max'],
            final_swap_events=obs['memory.swap.events'],final_counter_identity=final['resources']['cgroup']['identity'],
            final_counter_window=[final['resources']['start_t'],final['resources']['end_t']]),
        cg_IO_agreement=(all(cg_agreements) if cg_agreements and
                         all(value is not None for value in cg_agreements) else None),
        live_file_cache_peak_bytes=None,physical_union_peak_bytes=None,total_physical_RAM_54GB='INDETERMINATE',
        no_parent_disk_plus_partition_sum=True,
        interpretation='PID read_bytes is storage accounting for ALL q4t files; rchar and loader nvme counters are logical. '
                       'cgroup I/O may diverge or plateau; it never signs a storage saving. Device bytes include background. '
                       'NVIDIA/process/pinned/cache views overlap; no independent peaks are added.')


def run_audit(output):
    source=Sources(ROOT)
    report=dict(schema=1,audit_complete=False,performance_acceptance=False,
        total_physical_RAM_54GB='INDETERMINATE',physical_union_peak_bytes=None,
        method=dict(groups=list(GROUPS),max_file_bytes=MAX_FILE_BYTES,max_total_bytes=MAX_TOTAL_BYTES,
            max_rows_per_group=MAX_ROWS,boundary_uncertainty_limit_seconds=MAX_BOUNDARY_UNCERTAINTY_S,
            clock_offset_drift_limit_seconds=0.01,
            threshold_scope='Resource-counter interpretability only; does not modify frozen performance GO/NO_GO rules',
            bounds='Inside-window difference <= counter growth during client envelope <= outside-window difference, '
                   'conditional on continuous same-identity nondecreasing counters. Any intervening gap/reset invalidates the interval.',
            instantaneous_counter_read_time='unknown within each collection start/end window',
            no_interpolation=True,no_missing_as_zero=True,no_stale_carry_forward=True,
            resource_file_read_passes=1,post_read_integrity='stat unchanged; full SHA recorded during consumption',
            model_payload_read=False,live_system_or_GPU_query=False))
    # Exclusive output creation preserves earlier attempts including failures.
    with output.open('x') as file:
        try:
            completed=completion_gate(source)
            source.raw(Path(__file__))
            report['groups']={}
            for name in GROUPS:
                report['groups'][name]=audit_group(source,name,completed[name])
            source.stable()
            report['audit_complete']=True
            report['status']='RAW_RESOURCE_EVIDENCE_REVIEWED; KNOWN_GAPS_RETAINED'
            report['all_summary_recomputations_match']=all(
                g['inspection']['all_summary_checks_match'] for g in report['groups'].values())
            report['all_identity_contracts_satisfied']=all(
                g['inspection']['identity_contracts_passed'] for g in report['groups'].values())
            report['all_resources_observable']=False
        except (OSError,ValueError,KeyError,TypeError,IndexError) as error:
            report['status']='INCOMPLETE_OR_INVALID_EVIDENCE'
            report['failure']=type(error).__name__+': '+str(error)
        report['source_sha256']=source.records
        report['bytes_consumed']=source.total_bytes
        report['created_utc']=datetime.now(timezone.utc).isoformat()
        json.dump(report,file,indent=2,allow_nan=False);file.write('\n')
    print(report['status'])
    return 0 if (report['audit_complete'] and report['all_summary_recomputations_match'] and
                 report['all_identity_contracts_satisfied']) else 1


def self_test():
    """Eight pure host examples. No files, subprocesses or real evidence reads."""
    def rows():
        return [dict(sequence=i,phase='requests:test',start_t=float(i),end_t=i+0.01,
                     metrics={'pid_read_bytes':cell(100*i,'pid1')}) for i in range(12)]
    data=rows();bracket,reason=select_brackets(data,2.5,8.5)
    assert reason is None
    assert bounded_delta(data,bracket,'pid_read_bytes')['lower_bytes']==500
    assert bounded_delta(data,bracket,'pid_read_bytes')['upper_bytes']==700
    data[5]['metrics']['pid_read_bytes']=cell(None,'pid1')
    assert bounded_delta(data,bracket,'pid_read_bytes')['status']=='UNKNOWN'
    data=rows();data[6]['metrics']['pid_read_bytes']=cell(1,'pid1')
    assert bounded_delta(data,bracket,'pid_read_bytes')['status']=='UNKNOWN'
    data=rows();data[6]['metrics']['pid_read_bytes']=cell(600,'pid2')
    assert bounded_delta(data,bracket,'pid_read_bytes')['status']=='UNKNOWN'
    assert select_brackets(rows(),-1,8.5)[0] is None
    data=rows()
    for row in data: row['metrics']['pid_read_bytes']=cell(0,'pid1')
    assert bounded_delta(data,bracket,'pid_read_bytes')['upper_bytes']==0
    data=rows();data[6]['start_t']=4.0
    assert select_brackets(data,2.5,8.5)[0] is None
    assert kb_peaks(['rss_kb','private_kb','gpu_bytes'],{'rss_kb':128,'gpu_bytes':4096}) == {
        'rss_kb':128,'private_kb':None}
    print('8 synthetic counter/window/summary examples passed; no real evidence consumed')
    return 0


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--self-test',action='store_true')
    args=parser.parse_args()
    if args.self_test:
        require(args.output is None,'self-test takes no output/evidence')
        raise SystemExit(self_test())
    if args.output is None:
        parser.error('--output is required')
    output=args.output.resolve()
    require(output.is_relative_to(ROOT) and output.suffix=='.json','new output must be JSON under this evidence phase')
    raise SystemExit(run_audit(output))
