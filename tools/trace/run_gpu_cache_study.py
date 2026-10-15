"""Frozen source-faithful GPU-only replay of recorded request chains.

No model service, CUDA, lower-cache simulation or timing prediction. Baseline
must close against counters and physical GPU metadata before one counterfactual.
"""
import argparse
from array import array
from collections import Counter
import hashlib
import json
import re
from pathlib import Path
import struct
import subprocess
import sys
import time

from gpu_cache_replay import GpuCacheState, transition_load_lower_bound
from offload_trace import iter_layers

GROUPS = ['m02-as-on', 'm03-cs-on', 'm06-cl-on', 'm07-al-on']
COUNTERS = ('resolve_calls', 'expert_lookups', 'hits', 'misses', 'loads',
            'evictions', 'prefill_lookups', 'decode_lookups',
            'prefill_misses', 'decode_misses')
FIELDS = ('slot_experts', 'slot_ticks', 'slot_protected', 'slot_clock')
ENDPOINTS = [('prefill_begin', 0), ('prefill_end_decode_begin', 0),
             ('decode_prefix', 1), ('decode_prefix', 8),
             ('decode_prefix', 32), ('inference_end', 255)]
INTERVALS = ('prefill', 'decode_0_1', 'decode_1_8', 'decode_8_32',
             'decode_32_end')
GPU_DEPENDENCIES = (
    'tools/trace/run_gpu_cache_study.py',
    'tools/trace/gpu_cache_replay.py', 'tools/trace/offload_trace.py',
    'tools/trace/analyze.py', 'tools/trace/gpu_cache_schedule.cpp',
    'tools/trace/research/moe_partition.h', 'src/model/moe.cu',
    'src/quant/moe_residency.cpp', 'include/q4t/quant/moe_residency.h',
)


def source_paths(dependencies, workspace, runtime_root=None):
    """Bind tools to this checkout and runtime to an explicit archive."""
    runtime_root = (runtime_root or workspace).resolve()
    return [(workspace if name.startswith('tools/') else runtime_root) / name
            for name in dependencies]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def read_bound(path, expected):
    path = Path(path).resolve()
    require(path.is_file() and not path.is_symlink(), 'missing regular input')
    require(sha(path) == expected, 'input SHA differs: ' + str(path))
    value = json.loads(path.read_text())
    require(sha(path) == expected, 'input changed during read')
    return value


def subtract(after, before):
    result = {key: after[key] - before[key] for key in COUNTERS}
    require(all(value >= 0 for value in result.values()), 'counter decreased')
    return result


def counts(value):
    result = {key: value[key] for key in COUNTERS}
    require(all(type(v) is int and v >= 0 for v in result.values()),
            'invalid count type or sign')
    return result


def compare_counts(actual, expected, location):
    differences = {k: dict(actual=actual[k], expected=expected[k])
                   for k in COUNTERS if actual[k] != expected[k]}
    require(not differences, 'COUNTER_MISMATCH ' + location + ' ' +
            json.dumps(differences, sort_keys=True))


def compare_states(states, recorded, location):
    require(recorded is not None and len(recorded) == len(states),
            'missing complete GPU checkpoint')
    for layer, (state, expected) in enumerate(zip(states, recorded)):
        require(expected['layer'] == layer, 'checkpoint layer order differs')
        actual = state.snapshot()
        for field in FIELDS:
            if actual[field] != expected[field]:
                if isinstance(actual[field], list):
                    changed = [i for i, (a, b) in enumerate(zip(
                        actual[field], expected[field])) if a != b]
                    detail = dict(changed_slots=changed[:16],
                                  changed_slot_count=len(changed))
                else:
                    detail = dict(actual=actual[field], expected=expected[field])
                raise ValueError('STATE_MISMATCH ' + location + ' ' +
                                 json.dumps(dict(layer=layer, field=field,
                                                 detail=detail)))


def validate_manifest(manifest, scope_sha):
    require(manifest.get('schema') == 1 and
            manifest.get('scope_sha256') == scope_sha, 'manifest scope differs')
    ledger = manifest.get('source_sha256')
    require(isinstance(ledger, dict) and ledger and all(
        Path(p).is_absolute() and isinstance(h, str) and
        re.fullmatch('[0-9a-f]{64}', h) for p, h in ledger.items()),
        'manifest source ledger missing/invalid')
    require([g['id'] for g in manifest['groups']] == GROUPS,
            'group set/order differs')
    for group in manifest['groups']:
        require(group['arm'] == ('A' if group['id'] in GROUPS[::3] else 'C'),
                'arm identity differs')
        require(len(group['requests']) == 4, 'request count differs')
        for position, request in enumerate(group['requests']):
            expected_input = 8193 if position == 0 and group['id'] in GROUPS[2:] else 1024
            require(request['position'] == position and
                    request['input_tokens'] == expected_input and
                    request['output_tokens'] == 256, 'request shape differs')
            snapshots = request['snapshots']
            require([(s['event'], s['decode_forwards']) for s in snapshots] ==
                    ENDPOINTS, 'phase endpoints differ')
            for i, snapshot in enumerate(snapshots):
                counts(snapshot['counters'])
                layers = snapshot['gpu_layers']
                if i in (0, 1, 5):
                    require(layers is not None and len(layers) == 48,
                            'full checkpoint missing')
                    for layer, value in enumerate(layers):
                        require(value['layer'] == layer and
                                len(value['slot_experts']) == 256 and
                                not any(value['slot_protected']),
                                'captured GPU dimensions/protection differ')
                        GpuCacheState({k: value[k] for k in FIELDS})
                else:
                    require(layers is None, 'unexpected prefix GPU state')
            for left, right in zip(snapshots, snapshots[1:]):
                subtract(right['counters'], left['counters'])
            validation = request['validation']
            trace_path = Path(request['trace_path'])
            require(trace_path.is_absolute() and str(trace_path) == validation['path'],
                    'trace validation path differs')
            package, metadata, summary = (validation[k] for k in
                                            ('manifest', 'metadata', 'summary'))
            require(package['schema'] == 1 and package['complete'] is True and
                    package['failure'] == 'none' and
                    [package[k] for k in ('layers', 'experts', 'top_k', 'max_rows')]
                    == [48, 512, 10, 8192] and
                    package['requests_started'] == package['requests_published'] == 4 and
                    package['binary_sha256'] == manifest['runtime_binary_sha256'],
                    'trace package dimensions/completion differ')
            require(metadata['request_id'] == position + 1 and
                    metadata['prompt_tokens'] == expected_input and
                    metadata['http_id'] == request['response_id'],
                    'trace request metadata differs')
            require(summary == dict(forwards=257 if expected_input == 8193 else 256,
                    prefill_rows=expected_input, decode_rows=255,
                    route_ids=(expected_input + 255) * 48 * 10, output_tokens=256),
                    'trace validated totals differ')
            wanted_paths = {str(trace_path), str(trace_path.with_suffix('.json')),
                            str(trace_path.with_suffix('.tokens')),
                            str(trace_path.parent / 'manifest.json')}
            require(validation['trace_sha256'] == ledger.get(str(trace_path)),
                    'trace validated SHA differs')
            bindings = validation['bindings']
            require(set(bindings) == wanted_paths, 'trace binding set differs')
            for path, value in bindings.items():
                require(value['sha256'] == ledger.get(path) and
                        isinstance(value['stat'], list) and len(value['stat']) == 4 and
                        all(type(v) is int and v >= 0 for v in value['stat']),
                        'trace binding identity differs')
            forwards = request['forwards']
            wanted = [(1, 0, min(8192, expected_input))]
            if expected_input == 8193:
                wanted.append((1, 8192, 1))
            wanted.extend((2, expected_input + i, 1) for i in range(255))
            require([(f['stage'], f['position'], f['rows']) for f in forwards] == wanted
                    and all(type(f['forward_id']) is int and f['forward_id'] > 0
                            for f in forwards) and all(
                        a['forward_id'] < b['forward_id']
                        for a, b in zip(forwards, forwards[1:])),
                    'frozen forward identities differ')


class Scheduler:
    """Pure route/policy memoization; never depends on cache state."""
    def __init__(self, helper):
        self.helper = str(helper)
        self.cache = {}
        self.helper_calls = 0

    def chunks(self, trace, allow_partition):
        ids, rows, topk = trace['topk_ids'], trace['rows'], trace['top_k']
        require(rows * topk == len(ids) and topk == 10, 'trace shape differs')
        if rows == 1:
            return [[0]], dict(partition_applied=False, fallback=False)
        packed = array('H', ids)
        if sys.byteorder != 'little':
            packed.byteswap()
        payload = struct.pack('<5I', 512, 256, topk, rows,
                              int(allow_partition)) + packed.tobytes()
        key = hashlib.sha256(payload).hexdigest()
        if key not in self.cache:
            proc = subprocess.run([self.helper], input=payload,
                                  capture_output=True, timeout=30)
            require(proc.returncode == 0 and not proc.stderr,
                    'schedule helper failed: ' + proc.stderr.decode(errors='replace'))
            require(len(proc.stdout) < 2 << 20, 'schedule output exceeded bound')
            value = json.loads(proc.stdout)
            require(value['schema'] == 1, 'schedule schema differs')
            chunks = value['chunks']
            require(chunks and all(chunks), 'empty selected chunk')
            flat = [row for chunk in chunks for row in chunk]
            require(all(type(row) is int for row in flat) and
                    sorted(flat) == list(range(rows)), 'chunk permutation differs')
            require(sorted(value['token_order']) == list(range(rows)),
                    'lex order is not a permutation')
            for chunk in chunks:
                require(len({e for row in chunk for e in ids[row*topk:(row+1)*topk]})
                        <= 256, 'selected chunk exceeds GPU capacity')
            require(type(value['partition_applied']) is bool and
                    type(value['fallback']) is bool, 'invalid partition markers')
            require(allow_partition or (not value['partition_applied'] and
                    not value['fallback']), 'legacy applied new partition')
            self.cache[key] = (chunks, {k: value[k] for k in
                               ('partition_applied', 'fallback')})
            self.helper_calls += 1
        return self.cache[key]


def new_phase(states):
    return [dict(entry=set(e for e in state.snapshot()['slot_experts'] if e >= 0),
                 previous=None, demand=set(), capacity_bound=0, loads=0, chunks=0)
            for state in states]


def add_bound(row, needed, loads):
    needed = set(needed)
    previous = row['previous']
    increment = (len(needed - row['entry']) if previous is None else
                 transition_load_lower_bound(previous, needed, 256))
    row['capacity_bound'] += increment
    row['demand'].update(needed)
    row['previous'] = needed
    row['loads'] += loads
    row['chunks'] += 1


def finish_bounds(rows):
    result = []
    for layer, row in enumerate(rows):
        entry_bound = len(row['demand'] - row['entry'])
        bound = row['capacity_bound']
        require(row['chunks'] > 0 and bound <= row['loads'] and
                entry_bound <= row['loads'], 'load lower bound violated')
        result.append(dict(layer=layer, chunks=row['chunks'], loads=row['loads'],
                           entry_distinct_bound=entry_bound,
                           fixed_chunk_capacity_bound=bound,
                           combined_lower_bound=max(bound, entry_bound),
                           avoidable_load_upper_bound=row['loads'] - max(bound, entry_bound)))
    return dict(layers=result, loads=sum(r['loads'] for r in result),
                entry_distinct_bound=sum(r['entry_distinct_bound'] for r in result),
                fixed_chunk_capacity_bound=sum(r['fixed_chunk_capacity_bound'] for r in result),
                combined_lower_bound=sum(r['combined_lower_bound'] for r in result),
                avoidable_load_upper_bound=sum(r['avoidable_load_upper_bound'] for r in result),
                limit='Fixed phase entry/chunk order lower bounds; not a proven achievable policy or time/IO saving.')


def checkpoint(row, states, request, index, cumulative, previous, baseline):
    snapshot = request['snapshots'][index]
    current = {k: cumulative[k] for k in COUNTERS}
    expected = subtract(snapshot['counters'], request['snapshots'][0]['counters'])
    location = row['group'] + '/k' + str(row['position']) + '/' + str(index)
    if baseline:
        compare_counts(current, expected, location)
        if index in (1, 5):
            compare_states(states, snapshot['gpu_layers'], location)
    state_digest = (digest([state.snapshot() for state in states])
                    if index in (1, 5) else None)
    row['endpoints'].append(dict(index=index, event=snapshot['event'],
        decode_forwards=snapshot['decode_forwards'], cumulative=current,
        observed_cumulative=expected, gpu_state_sha256=state_digest,
        exact_observed_state_checked=baseline and index in (1, 5)))
    row['intervals'][INTERVALS[index-1]] = subtract(current, previous)
    return current


def replay(manifest, helper, baseline, report):
    scheduler = Scheduler(helper)
    for group in manifest['groups']:
        first = group['requests'][0]['snapshots'][0]['gpu_layers']
        states = [GpuCacheState({k: row[k] for k in FIELDS},
                  policy='physical_slot' if baseline else 'expert_id') for row in first]
        for request in group['requests']:
            row = dict(group=group['id'], arm=group['arm'], position=request['position'],
                       input_tokens=request['input_tokens'], output_tokens=request['output_tokens'],
                       complete=False, endpoints=[], intervals={}, phases={},
                       bounds={}, partition_counts=Counter())
            report['requests'].append(row)
            if baseline:
                compare_states(states, request['snapshots'][0]['gpu_layers'],
                               group['id'] + '/k' + str(request['position']) + '/entry')
            row['entry_exact_checked'] = baseline
            row['entry_gpu_state_sha256'] = digest([state.snapshot() for state in states])
            schedule_hash = hashlib.sha256()
            cumulative = Counter(dict.fromkeys(COUNTERS, 0))
            previous = dict(cumulative)
            bounds = new_phase(states)
            prefill_done = False
            decoder_count = 0
            last_layer = 47
            forwarded = 0
            for trace in iter_layers(request['trace_path'], request['validation']):
                layer = trace['layer']
                require(layer == (last_layer + 1) % 48, 'trace layer order differs')
                last_layer = layer
                if layer == 0:
                    forwarded += 1
                require(forwarded <= len(request['forwards']) and all(
                    trace[key] == request['forwards'][forwarded-1][key]
                    for key in ('forward_id', 'stage', 'position', 'rows')),
                    'trace differs from recorded forward identity')
                is_prefill = trace['stage'] == 1
                require(is_prefill != prefill_done, 'trace phase order differs')
                allow = group['arm'] == 'C' and (
                    not is_prefill or request['input_tokens'] > 8192)
                chunks, info = scheduler.chunks(trace, allow)
                schedule_hash.update(json.dumps(
                    [trace['forward_id'], layer, trace['rows'], int(allow), chunks],
                    separators=(',', ':')).encode() + b'\n')
                row['partition_counts']['layer_forwards'] += 1
                row['partition_counts']['applied'] += int(info['partition_applied'])
                row['partition_counts']['fallback'] += int(info['fallback'])
                ids, k = trace['topk_ids'], trace['top_k']
                for chunk in chunks:
                    needed = [e for token in chunk for e in ids[token*k:(token+1)*k]]
                    result = states[layer].resolve(needed, decode_phase=len(chunk) == 1)
                    cumulative.update({name: result[name] for name in COUNTERS})
                    add_bound(bounds[layer], needed, result['loads'])
                if layer != 47:
                    continue
                if is_prefill and trace['position'] + trace['rows'] == request['input_tokens']:
                    previous = checkpoint(row, states, request, 1, cumulative, previous, baseline)
                    row['phases']['prefill'] = dict(previous)
                    row['bounds']['prefill'] = finish_bounds(bounds)
                    prefill_done = True
                    bounds = new_phase(states)
                elif not is_prefill:
                    decoder_count += 1
                    if decoder_count in (1, 8, 32, 255):
                        index = (1, 8, 32, 255).index(decoder_count) + 2
                        previous = checkpoint(row, states, request, index, cumulative, previous, baseline)
            require(prefill_done and decoder_count == 255 and last_layer == 47 and
                    len(row['endpoints']) == 5 and forwarded ==
                    (257 if request['input_tokens'] == 8193 else 256),
                    'request iterator did not close all frozen forwards')
            row['phases']['decode'] = subtract(dict(cumulative), row['phases']['prefill'])
            row['bounds']['decode'] = finish_bounds(bounds)
            row['schedule_sha256'] = schedule_hash.hexdigest()
            row['complete'] = True
    report['schedule_helper_calls'] = scheduler.helper_calls
    require(len(report['requests']) == 16 and all(r['complete'] for r in report['requests']),
            'fixed request collection incomplete')


def validate_baseline(baseline, scope_sha, manifest_sha, helper_sha, execution_sha):
    require(baseline.get('passed') is True and baseline.get('mode') == 'baseline'
            and baseline.get('status') == 'EXACT_BASELINE_REPRODUCED' and
            baseline.get('scope_sha256') == scope_sha and
            baseline.get('manifest_sha256') == manifest_sha and
            baseline.get('helper_sha256') == helper_sha and
            baseline.get('execution_plan_sha256') == execution_sha,
            'baseline source identity/pass differs')
    rows = baseline.get('requests', [])
    require([(row['group'], row['position']) for row in rows] ==
            [(g, k) for g in GROUPS for k in range(4)],
            'baseline fixed request set differs')
    for row in rows:
        require(row.get('complete') is True and row.get('entry_exact_checked') is True
                and set(row['phases']) == {'prefill', 'decode'} and
                set(row['intervals']) == set(INTERVALS) and
                len(row['endpoints']) == 5 and
                isinstance(row.get('schedule_sha256'), str) and
                re.fullmatch('[0-9a-f]{64}', row['schedule_sha256']),
                'baseline has incomplete request')
        last = dict.fromkeys(COUNTERS, 0)
        for i, endpoint in enumerate(row['endpoints'], 1):
            require(endpoint['index'] == i and
                    (endpoint['event'], endpoint['decode_forwards']) == ENDPOINTS[i],
                    'baseline endpoint identity differs')
            actual = counts(endpoint['cumulative'])
            compare_counts(actual, counts(endpoint['observed_cumulative']), 'baseline')
            require(endpoint['exact_observed_state_checked'] is (i in (1, 5)),
                    'baseline state not fully checked')
            require(row['intervals'][INTERVALS[i-1]] == subtract(actual, last),
                    'baseline interval counters differ')
            last = actual
        require(row['phases']['prefill'] == row['endpoints'][0]['cumulative'] and
                row['phases']['decode'] == subtract(last, row['phases']['prefill']),
                'baseline full phase counters differ')


def candidate_decision(report, baseline):
    require(baseline['mode'] == 'baseline' and baseline['passed'] and
            len(baseline['requests']) == len(report['requests']) == 16,
            'candidate needs complete admitted baseline')
    require(all(row.get('complete') is True for row in baseline['requests']) and
            all(row.get('complete') is True for row in report['requests']),
            'candidate/baseline request incomplete')
    comparisons = []
    for actual, observed in zip(report['requests'], baseline['requests']):
        require((actual['group'], actual['position']) ==
                (observed['group'], observed['position']), 'baseline pair differs')
        require(actual['schedule_sha256'] == observed['schedule_sha256'] and
                actual['partition_counts'] == observed['partition_counts'],
                'candidate changed actual chunk schedule')
        phases = {}
        for phase in ('prefill', 'decode'):
            a, b = actual['phases'][phase], observed['phases'][phase]
            require(a['resolve_calls'] == b['resolve_calls'] and
                    a['expert_lookups'] == b['expert_lookups'], 'candidate changed schedule work')
            phases[phase] = dict(baseline_loads=b['loads'], candidate_loads=a['loads'],
                                delta_loads=a['loads']-b['loads'])
        comparisons.append(dict(group=actual['group'], position=actual['position'], phases=phases))
    gates = dict(all_prefill_nonincreasing=all(c['phases']['prefill']['delta_loads'] <= 0 for c in comparisons),
                 all_decode_nonincreasing=all(c['phases']['decode']['delta_loads'] <= 0 for c in comparisons),
                 any_prefill_strict_reduction=any(c['phases']['prefill']['delta_loads'] < 0 for c in comparisons))
    report['candidate_comparisons'] = comparisons
    report['offline_work_gates'] = gates
    report['offline_work_decision'] = ('GO_FOR_RUNTIME_CONSIDERATION' if all(gates.values())
                                       else 'NO_GO_OFFLINE_WORK')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ('manifest', 'scope', 'helper', 'execution_plan', 'output'):
        parser.add_argument('--' + flag.replace('_', '-'), type=Path, required=True)
    for flag in ('manifest', 'scope', 'helper', 'execution_plan'):
        parser.add_argument('--' + flag.replace('_', '-') + '-sha256', required=True)
    parser.add_argument('--runtime-source-root', type=Path,
                        help='read-only archived runtime source tree')
    parser.add_argument('--mode', choices=('baseline', 'candidate'), required=True)
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--baseline-sha256')
    args = parser.parse_args()
    output = args.output.resolve()
    require(not output.exists(), 'refuse overwriting prior evidence')
    workspace = Path(__file__).resolve().parents[2]
    runtime_root = (args.runtime_source_root or workspace).resolve()
    require(any(output.is_relative_to(workspace / name)
                for name in ('build', '.q4t-work')),
            'output outside artifact roots')
    report = dict(schema=1, started_t=time.time(), mode=args.mode, passed=False,
                  failure=None, scope_sha256=args.scope_sha256,
                  manifest_sha256=args.manifest_sha256,
                  helper_sha256=args.helper_sha256,
                  execution_plan_sha256=args.execution_plan_sha256, requests=[],
                  performance_acceptance=False, physical_io_prediction=False,
                  models_launched=0, HTTP_requests=0, source_sha256={})
    with output.open('x') as destination:
        try:
            scope = read_bound(args.scope, args.scope_sha256)
            require(scope['scope'] == 'source_faithful_gpu_cache_replay_v1', 'wrong scope')
            execution = read_bound(args.execution_plan, args.execution_plan_sha256)
            require(execution['scope_sha256'] == args.scope_sha256 and
                    execution['manifest_sha256'] == args.manifest_sha256,
                    'execution source identity differs')
            require(sha(args.helper) == args.helper_sha256, 'helper SHA differs')
            sources = dict(execution['source_sha256'])
            required = [args.helper.resolve(),
                        *source_paths(GPU_DEPENDENCIES, workspace, runtime_root)]
            require(all(str(path) in sources for path in required) and
                    sources[str(args.helper.resolve())] == args.helper_sha256,
                    'execution lacks required source/helper bindings')
            for p, h in sources.items():
                require(sha(p) == h, 'frozen tool/source changed: ' + p)
            require(sources.get(str(Path(__file__).resolve())) == sha(Path(__file__)),
                    'runner source not bound')
            manifest = read_bound(args.manifest, args.manifest_sha256)
            validate_manifest(manifest, args.scope_sha256)
            for p, h in manifest['source_sha256'].items():
                require(sha(p) == h, 'manifest source changed: ' + p)
            prior = None
            if args.mode == 'candidate':
                require(args.baseline is not None and args.baseline_sha256,
                        'candidate needs baseline binding')
                prior = read_bound(args.baseline, args.baseline_sha256)
                validate_baseline(prior, args.scope_sha256, args.manifest_sha256,
                                  args.helper_sha256, args.execution_plan_sha256)
                report['baseline_sha256'] = args.baseline_sha256
            replay(manifest, args.helper, args.mode == 'baseline', report)
            if prior is not None:
                candidate_decision(report, prior)
            for p, h in {**sources, **manifest['source_sha256']}.items():
                require(sha(p) == h, 'source changed during replay: ' + p)
            report['source_sha256'] = {**sources, **manifest['source_sha256'],
                str(args.manifest.resolve()): args.manifest_sha256,
                str(args.scope.resolve()): args.scope_sha256,
                str(args.execution_plan.resolve()): args.execution_plan_sha256,
                str(args.helper.resolve()): args.helper_sha256}
            report.update(passed=True, status='EXACT_BASELINE_REPRODUCED' if prior is None
                          else 'FIXED_CANDIDATE_REPLAY_COMPLETE')
        except BaseException as error:
            report.update(status='FAIL_RETAINED', failure=type(error).__name__ + ': ' + str(error))
        report['ended_t'] = time.time()
        json.dump(report, destination, indent=2, allow_nan=False)
        destination.write('\n')
    print(json.dumps({k: report.get(k) for k in
                     ('status', 'failure', 'offline_work_decision')}), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
