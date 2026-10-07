"""Bounded decode supply identification; no worker schedule or time model."""

import argparse
from collections import Counter
import json
from pathlib import Path
import time

from decode_supply_bounds import plan_direct_loss_upper
from gpu_cache_replay import GpuCacheState, UINT64_MAX
from offload_trace import iter_layers
from run_gpu_cache_study import (
    COUNTERS, FIELDS, GROUPS, INTERVALS, compare_counts, compare_states,
    counts, read_bound, require, sha, subtract, validate_baseline,
    validate_manifest, source_paths,
)


DECODE_INTERVALS = INTERVALS[1:]
SUPPLY_KEYS = ('l2_hits', 'l2_misses', 'mirror_hits')
DEPENDENCIES = (
    'tools/trace/analyze_decode_supply.py',
    'tools/trace/decode_supply_bounds.py',
    'tools/trace/gpu_cache_replay.py',
    'tools/trace/run_gpu_cache_study.py',
    'tools/trace/offload_trace.py', 'tools/trace/analyze.py',
    'src/model/moe.cu', 'src/quant/moe_residency.cpp',
    'include/q4t/quant/moe_residency.h',
)


def uint(value, label, maximum=UINT64_MAX):
    require(type(value) is int and 0 <= value <= maximum,
            'invalid ' + label)
    return value


def validate_scope(scope):
    expected = dict(stage='decode_supply_identification_and_direct_race_bound',
                    fixed_groups=GROUPS, requests_per_group=4,
                    decode_forwards_per_request=255, layers=48, experts=512,
                    top_k=10, gpu_slots=256, l2_slots=16, mirror_slots=8,
                    new_runtime_changes_initial_stage=False,
                    new_model_runs_initial_stage=0, new_HTTP_initial_stage=0,
                    bench=False)
    require(all(scope.get(k) == v for k, v in expected.items()),
            'frozen supply scope differs')
    require(scope['decision_gate']['primary_position'] == 1 and
            scope['decision_gate']['max_direct_extra_losses'] ==
            'U_long, never U_long minus U_short', 'decision gate differs')


def validate_scope_bindings(scope, sources):
    bindings = scope.get('source_sha256')
    require(isinstance(bindings, dict) and bindings and
            isinstance(sources, dict) and all(
                sources.get(path) == identity
                for path, identity in bindings.items()),
            'execution ledger differs from frozen scope sources')


def lower_layer(value, gpu, index):
    require(value['layer'] == index and gpu['layer'] == index,
            'lower layer order differs')
    require(all(value[key] == gpu[key] for key in FIELDS),
            'lower/GPU snapshot identity differs')
    GpuCacheState(value)
    for key, length in (('l2_experts', 16), ('mirror_experts', 8)):
        ids = value[key]
        require(isinstance(ids, list) and len(ids) == length and all(
            type(e) is int and -1 <= e < 512 for e in ids),
            'invalid ' + key)
        occupied = [e for e in ids if e >= 0]
        require(len(set(occupied)) == len(occupied),
                'duplicate lower-cache expert')
    clock = uint(value['l2_clock'], 'L2 clock')
    ticks = value['l2_ticks']
    require(isinstance(ticks, list) and len(ticks) == 16,
            'invalid L2 tick array')
    for tick in ticks:
        uint(tick, 'L2 tick', clock)
    uint(value['mirror_cursor'], 'mirror cursor', 7)


def validate_lower_snapshots(request):
    for index, snapshot in enumerate(request['snapshots']):
        stats = snapshot['lower_stats']
        for key in SUPPLY_KEYS:
            uint(stats[key], key)
            uint(snapshot['counters'][key], 'counter ' + key)
            require(stats[key] == snapshot['counters'][key],
                    'lower stats/counter identity differs: ' + key)
        layers = snapshot['lower_layers']
        if index in (0, 1, 5):
            require(isinstance(layers, list) and len(layers) == 48,
                    'missing full lower-cache checkpoint')
            for layer, (lower, gpu) in enumerate(zip(
                    layers, snapshot['gpu_layers'])):
                lower_layer(lower, gpu, layer)
        else:
            require(layers is None, 'prefix has invented lower-cache state')


def supply_delta(after, before):
    result = {key: uint(after[key], key) - uint(before[key], key)
              for key in SUPPLY_KEYS}
    require(all(value >= 0 for value in result.values()),
            'lower-cache counter decreased')
    return result


def ordered_plan(state, needed):
    """Project ordered first misses and actual occupied victims from GPU core."""
    require(len(needed) == 10 and len(set(needed)) == 10 and all(
        type(e) is int and 0 <= e < 512 for e in needed),
        'decode router row must contain ten distinct experts')
    before = state.snapshot()
    resident = set(before['slot_experts'])
    missing = [expert for expert in needed if expert not in resident]
    result = state.resolve(needed, decode_phase=True)
    after = state.snapshot()
    positions = {e: s for s, e in enumerate(after['slot_experts']) if e >= 0}
    victims = [before['slot_experts'][positions[e]] for e in missing]
    occupied = [expert for expert in victims if expert >= 0]
    require(len(missing) == result['loads'] == result['misses'] and
            len(occupied) == result['evictions'] and
            not set(occupied).intersection(needed),
            'GPU plan projection differs')
    return missing, occupied, result


def identify_layer_supply(loads, before, after):
    """Valid only after aggregate-zero L2 hits has been established."""
    reads = uint(after['l2_clock'], 'end L2 clock') - uint(
        before['l2_clock'], 'entry L2 clock')
    require(0 <= reads <= loads, 'L2 clock delta exceeds layer loads')
    return reads, loads - reads


def interval_index(step):
    require(type(step) is int and 1 <= step <= 255, 'invalid decode step')
    return 0 if step == 1 else 1 if step <= 8 else 2 if step <= 32 else 3


def analyze_request(group, request, prior, output):
    """One actual decode entry, four checked intervals, full source exhaustion."""
    output.update(group=group['id'], position=request['position'], complete=False)
    validate_lower_snapshots(request)
    snapshots = request['snapshots']
    observed = subtract(snapshots[5]['counters'], snapshots[1]['counters'])
    compare_counts(observed, counts(prior['phases']['decode']),
                   'prior decode identity')
    supply = supply_delta(snapshots[5]['lower_stats'],
                          snapshots[1]['lower_stats'])
    require(supply['l2_hits'] == 0 and
            sum(supply.values()) == observed['loads'],
            'zero-L2-hit identification precondition failed')
    states = [GpuCacheState(layer) for layer in snapshots[1]['lower_layers']]
    compare_states(states, snapshots[1]['gpu_layers'], 'decode actual entry')
    possible = [set(e for e in layer['mirror_experts'] if e >= 0)
                for layer in snapshots[1]['lower_layers']]
    layer_rows = [dict(layer=layer, loads=0, evictions=0,
        raw_direct_loss_upper=0, plan_miss_hist=[0] * 11,
        interval_raw_upper={name: 0 for name in DECODE_INTERVALS},
        first32_raw_upper=0, last32_raw_upper=0) for layer in range(48)]
    output.update(group=group['id'], arm=group['arm'],
        position=request['position'], input_tokens=request['input_tokens'],
        output_tokens=256, complete=False, intervals={}, layers=layer_rows,
        initialized_from_actual_decode_entry=True,
        counterfactual_history_simulated=False, iterator_exhausted=False)
    cumulative = Counter(dict.fromkeys(COUNTERS, 0))
    previous = dict(cumulative)
    forwards, last_layer, decode_steps, prefill_rows = 0, 47, 0, 0
    for trace in iter_layers(request['trace_path'], request['validation']):
        layer = trace['layer']
        require(layer == (last_layer + 1) % 48, 'trace layer order differs')
        last_layer = layer
        if layer == 0:
            forwards += 1
        require(forwards <= len(request['forwards']) and all(
            trace[key] == request['forwards'][forwards - 1][key]
            for key in ('forward_id', 'stage', 'position', 'rows')),
            'forward identity differs')
        if trace['stage'] == 1:
            require(decode_steps == 0, 'prefill returned after decode')
            if layer == 47:
                prefill_rows += trace['rows']
            continue
        require(trace['stage'] == 2 and trace['rows'] == 1 and
                trace['top_k'] == 10 and
                prefill_rows == request['input_tokens'],
                'true decode shape/order differs')
        step = decode_steps + 1
        which = DECODE_INTERVALS[interval_index(step)]
        missing, victims, result = ordered_plan(states[layer],
                                               list(trace['topk_ids']))
        # Current victims cannot supply the current missing set. Add them
        # only after bounding this plan, preserving strictly-earlier history.
        bound = plan_direct_loss_upper(missing, victims, possible[layer])
        possible[layer].update(victims)
        row = layer_rows[layer]
        row['loads'] += result['loads']
        row['evictions'] += result['evictions']
        row['raw_direct_loss_upper'] += bound
        row['plan_miss_hist'][len(missing)] += 1
        row['interval_raw_upper'][which] += bound
        if step <= 32:
            row['first32_raw_upper'] += bound
        if step >= 224:
            row['last32_raw_upper'] += bound
        cumulative.update({key: result[key] for key in COUNTERS})
        if layer != 47:
            continue
        decode_steps += 1
        if decode_steps in (1, 8, 32, 255):
            index = (1, 8, 32, 255).index(decode_steps) + 2
            expected = subtract(snapshots[index]['counters'],
                                snapshots[1]['counters'])
            compare_counts(dict(cumulative), expected, 'decode prefix')
            interval = subtract(dict(cumulative), previous)
            original_supply = supply_delta(snapshots[index]['lower_stats'],
                                            snapshots[index-1]['lower_stats'])
            require(original_supply['l2_hits'] == 0 and
                    sum(original_supply.values()) == interval['loads'],
                    'interval supply does not close')
            raw_upper = sum(r['interval_raw_upper'][which] for r in layer_rows)
            output['intervals'][which] = dict(counters=interval,
                supply=original_supply, raw_direct_loss_upper=raw_upper,
                capped_direct_loss_upper=min(raw_upper,
                                              original_supply['l2_misses']))
            previous = dict(cumulative)
    output['iterator_exhausted'] = True
    require(last_layer == 47 and decode_steps == 255 and forwards ==
            len(request['forwards']) and len(output['intervals']) == 4,
            'trace iterator ended before complete decode')
    compare_counts(dict(cumulative), observed, 'decode full phase')
    compare_states(states, snapshots[5]['gpu_layers'], 'decode full endpoint')
    prior_layers = prior['bounds']['decode']['layers']
    require([r['layer'] for r in prior_layers] == list(range(48)),
            'prior layer order differs')
    for layer, row in enumerate(layer_rows):
        require(row['loads'] == prior_layers[layer]['loads'] and
                sum(row['plan_miss_hist']) == 255 and
                sum(i*n for i, n in enumerate(row['plan_miss_hist'])) ==
                row['loads'], 'per-layer baseline load identity differs')
        reads, mirrors = identify_layer_supply(row['loads'],
            snapshots[1]['lower_layers'][layer],
            snapshots[5]['lower_layers'][layer])
        row.update(actual_software_read_misses=reads,
            actual_mirror_hits=mirrors, actual_l2_hits=0,
            capped_direct_loss_upper=min(row['raw_direct_loss_upper'], reads))
    require(sum(r['actual_software_read_misses'] for r in layer_rows) ==
            supply['l2_misses'] and sum(r['actual_mirror_hits']
            for r in layer_rows) == supply['mirror_hits'],
            'per-layer clock supply does not close to measured aggregate')
    output.update(complete=True, counters=dict(cumulative), supply=supply,
        raw_direct_loss_upper=sum(r['raw_direct_loss_upper'] for r in layer_rows),
        capped_direct_loss_upper=sum(r['capped_direct_loss_upper']
                                     for r in layer_rows),
        first32_raw_upper=sum(r['first32_raw_upper'] for r in layer_rows),
        last32_raw_upper=sum(r['last32_raw_upper'] for r in layer_rows),
        last32_has_independent_read_cap=False,
        exact_gpu_endpoint_checked=True)


def comparisons(rows):
    require([(row['group'], row['position']) for row in rows] ==
            [(group, k) for group in GROUPS for k in range(4)] and
            all(row['complete'] for row in rows), 'fixed collection incomplete')
    by_id = {(row['group'], row['position']): row for row in rows}
    result = []
    for arm, short, long in [('A', GROUPS[0], GROUPS[3]),
                              ('C', GROUPS[1], GROUPS[2])]:
        for position in (1, 2, 3):
            s, l = by_id[short, position], by_id[long, position]
            deficit = s['supply']['mirror_hits'] - l['supply']['mirror_hits']
            upper = l['capped_direct_loss_upper']
            layers = []
            for a, b in zip(s['layers'], l['layers']):
                layer_deficit = a['actual_mirror_hits'] - b['actual_mirror_hits']
                layers.append(dict(layer=a['layer'], mirror_deficit=layer_deficit,
                    software_read_delta=b['actual_software_read_misses'] -
                    a['actual_software_read_misses'], load_delta=b['loads']-a['loads'],
                    long_direct_upper=b['capped_direct_loss_upper'],
                    short_direct_upper=a['capped_direct_loss_upper'],
                    deficit_beyond_long_upper=max(0, layer_deficit -
                                                  b['capped_direct_loss_upper'])))
            result.append(dict(arm=arm, position=position,
                mirror_deficit=deficit, load_delta=l['counters']['loads'] -
                s['counters']['loads'], software_read_delta=l['supply']['l2_misses'] -
                s['supply']['l2_misses'],
                short_direct_upper=s['capped_direct_loss_upper'],
                long_direct_upper=upper,
                positive_deficit_not_fully_explained=deficit > 0 and upper < deficit,
                deficit_beyond_long_upper=max(0, deficit - upper), layers=layers))
    primary = [r for r in result if r['position'] == 1]
    excluded = all(r['positive_deficit_not_fully_explained'] for r in primary)
    decision = ('DIRECT_RACE_FULL_EXPLANATION_EXCLUDED' if excluded else
                'DIRECT_RACE_NOT_EXCLUDED_OBSERVER_APPENDIX_REQUIRED')
    return result, decision


def execute(args):
    output = args.output.resolve()
    workspace = Path(__file__).resolve().parents[2]
    runtime_root = (getattr(args, 'runtime_source_root', None) or workspace).resolve()
    require(any(output.is_relative_to(workspace / name)
                for name in ('build', '.q4t-work')),
            'output outside artifact roots')
    report = dict(schema=1, started_t=time.time(), passed=False, failure=None,
        scope_sha256=args.scope_sha256, manifest_sha256=args.manifest_sha256,
        execution_plan_sha256=args.execution_plan_sha256, requests=[],
        model_runs=0, HTTP_requests=0, performance_acceptance=False,
        physical_IO_prediction=False, source_sha256={},
        interpretation='Direct same-plan source-loss ceiling only; no indirect '
        'history bound, measured race frequency, independent worker schedule, '
        'exclusive waiting attribution or runtime speedup claim.')
    with output.open('x') as destination:
        try:
            scope = read_bound(args.scope, args.scope_sha256)
            validate_scope(scope)
            execution = read_bound(args.execution_plan, args.execution_plan_sha256)
            require(execution['scope_sha256'] == args.scope_sha256 and
                    execution['manifest_sha256'] == args.manifest_sha256,
                    'execution plan identity differs')
            sources = execution['source_sha256']
            validate_scope_bindings(scope, sources)
            required = source_paths(DEPENDENCIES, workspace, runtime_root)
            require(all(str(path) in sources for path in required),
                    'execution missing required source dependencies')
            for path, identity in sources.items():
                require(sha(path) == identity, 'source changed before run: ' + path)
            manifest = read_bound(args.manifest, args.manifest_sha256)
            validate_manifest(manifest, args.scope_sha256)
            ledger = manifest['source_sha256']
            for path, identity in ledger.items():
                require(sha(path) == identity, 'evidence changed before run: ' + path)
            baseline_path = str(Path(manifest['baseline_path']).resolve())
            require(ledger.get(baseline_path) == manifest['baseline_sha256'],
                    'baseline is not source bound')
            baseline = read_bound(baseline_path, manifest['baseline_sha256'])
            validate_baseline(baseline, baseline['scope_sha256'],
                baseline['manifest_sha256'], baseline['helper_sha256'],
                baseline['execution_plan_sha256'])
            baseline_rows = {(row['group'], row['position']): row
                             for row in baseline['requests']}
            for group in manifest['groups']:
                for request in group['requests']:
                    result = {}
                    report['requests'].append(result)
                    analyze_request(group, request,
                        baseline_rows[group['id'], request['position']], result)
            contrasts, decision = comparisons(report['requests'])
            report.update(comparisons=contrasts, direct_race_decision=decision)
            combined = {**sources, **ledger, str(args.scope.resolve()):
                args.scope_sha256, str(args.manifest.resolve()):
                args.manifest_sha256, str(args.execution_plan.resolve()):
                args.execution_plan_sha256}
            for path, identity in combined.items():
                require(sha(path) == identity, 'source changed during run: ' + path)
            report.update(source_sha256=combined, passed=True,
                          status='DECODE_SUPPLY_IDENTIFIED_AND_BOUNDED')
        except BaseException as error:
            report.update(status='FAIL_RETAINED',
                          failure=type(error).__name__ + ': ' + str(error))
        report['ended_t'] = time.time()
        json.dump(report, destination, indent=2, allow_nan=False)
        destination.write('\n')
    return 0 if report['passed'] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('manifest', 'scope', 'execution_plan'):
        flag = name.replace('_', '-')
        parser.add_argument('--' + flag, type=Path, required=True)
        parser.add_argument('--' + flag + '-sha256', required=True)
    parser.add_argument('--runtime-source-root', type=Path,
                        help='read-only archived runtime source tree')
    parser.add_argument('--output', type=Path, required=True)
    return execute(parser.parse_args())


if __name__ == '__main__':
    raise SystemExit(main())
