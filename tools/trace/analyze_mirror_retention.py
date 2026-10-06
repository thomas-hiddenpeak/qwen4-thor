"""Frozen mirror opportunity analysis, without simulating lower-cache history.

Every selected trace is consumed through EOF. GPU plans are reconstructed from
actual decode-entry state; observed lower-cache endpoints are never interpolated.
"""

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import time

from analyze_decode_supply import lower_layer, ordered_plan
from gpu_cache_replay import GpuCacheState
from mirror_retention import analyze_layer, classify_snapshot, compare_layers
from offload_trace import iter_layers
from run_gpu_cache_study import (
    COUNTERS, ENDPOINTS, compare_counts, compare_states, counts, read_bound,
    require, sha, subtract,
)

GROUPS = ['s02-as-on', 's03-al-on']
DEPENDENCIES = (
    'tools/trace/analyze_mirror_retention.py',
    'tools/trace/mirror_retention.py',
    'tools/trace/analyze_decode_supply.py',
    'tools/trace/decode_supply_bounds.py',
    'tools/trace/gpu_cache_replay.py',
    'tools/trace/run_gpu_cache_study.py',
    'tools/trace/offload_trace.py', 'tools/trace/analyze.py',
    'src/model/moe.cu', 'src/quant/moe_residency.cpp',
    'include/q4t/quant/moe_residency.h',
    'include/q4t/quant/moe_supply_observer.h',
)
SUPPLY = ('l2_hits', 'l2_misses', 'mirror_hits')
ERRORS = ('plans_failed', 'plans_scope_mismatch', 'claim_errors', 'read_errors',
          'commit_errors', 'duplicate_claims', 'counter_overflow',
          'entry_candidate_unclaimed', 'entry_candidate_to_read_other')
CANDIDATE_PARTS = (
    'entry_candidate_to_mirror', 'entry_candidate_to_l2',
    'entry_candidate_to_read_direct_active',
    'entry_candidate_to_read_direct_published',
    'entry_candidate_to_read_other', 'entry_candidate_unclaimed',
)
LIMITS = [
    'Potential support is actual decode-entry mirrors plus strictly earlier '
    'occupied GPU victims; it does not assert actual retained membership.',
    'Distances count plans and strictly intervening victim events, not time '
    'or within-plan worker publication order.',
    'Entry-candidate counters are accumulated per-plan opportunities, not '
    'a request-entry cache capacity.',
    'Common/only capacity intervals form a conservative feasible superset, '
    'not exclusive causal shares or counterfactual savings.',
    'Endpoint duplicate presence does not establish persistence until reuse '
    'or available alternatives at an actual overwrite.',
    'No lower-cache policy replay, new HTTP, model run, performance acceptance '
    'or physical I/O prediction; old sixteen requests are not pooled.',
]


def uint(value, label):
    require(type(value) is int and 0 <= value < (1 << 64),
            'invalid unsigned ' + label)
    return value


def validate_scope(scope):
    expected = dict(schema=1, stage='mirror_retention_opportunities_offline',
        groups=GROUPS, requests_per_group=4, layers=48, experts=512,
        decode_forwards=255, top_k=10, gpu_slots=256, l2_slots=16,
        mirror_slots=8, primary_position=1, new_HTTP=0, model_runs=0,
        runtime_changes=False)
    require(all(type(scope.get(k)) is type(v) and scope.get(k) == v
                for k, v in expected.items()), 'frozen retention scope differs')
    require(scope['decision_gate']['hypothesis_confirmable_here'] is False,
            'scope cannot identify actual overwrite causality')


def validate_ledger(ledger):
    require(isinstance(ledger, dict) and bool(ledger) and all(
        isinstance(p, str) and Path(p).is_absolute() and
        isinstance(h, str) and re.fullmatch('[0-9a-f]{64}', h)
        for p, h in ledger.items()), 'source ledger missing or invalid')



def validate_execution(execution, scope_sha, manifest_sha, workspace):
    require(execution.get('schema') == 1 and
            execution['scope_sha256'] == scope_sha and
            execution['manifest_sha256'] == manifest_sha,
            'execution plan identity differs')
    expected = dict(request_count=8, actual_trace_passes=1,
                    new_HTTP=0, model_runs=0)
    require(all(type(execution.get(k)) is int and execution[k] == v
                for k, v in expected.items()), 'execution scope differs')
    sources = execution['source_sha256']
    validate_ledger(sources)
    required = [str(workspace / name) for name in DEPENDENCIES]
    require(all(path in sources for path in required),
            'execution missing required source dependencies')
    receipt = read_bound(execution['validation_results_path'],
                         execution['validation_results_sha256'])
    require(receipt.get('schema') == 1 and receipt.get('passed') is True,
            'new analysis contracts have not passed')
    validate_ledger(receipt['source_sha256'])
    require(all(receipt['source_sha256'].get(path) == sources[path]
                for path in required), 'validated tool source differs')
    return sources


def observer_counts(value, plans):
    require(isinstance(value, dict) and value and all(
        type(v) is int and 0 <= v < (1 << 64) for v in value.values()),
        'invalid observer counter')
    c = value
    require(all(c[key] == 0 for key in ERRORS), 'observer error is retained')
    require(c['plans_started'] == c['plans_complete'] == plans,
            'observer plan count differs')
    require(c['planned_loads'] == c['committed_loads'] ==
            c['source_l2'] + c['source_mirror'] + c['source_read'] and
            c['planned_loads'] <= 10 * plans, 'observer source closure differs')
    require(c['entry_mirror_candidate'] == sum(c[k] for k in CANDIDATE_PARTS)
            and c['entry_mirror_candidate'] <= c['entry_mirror_present'] <=
            c['planned_loads'] and c['entry_l2_present'] +
            c['entry_mirror_candidate'] <= c['planned_loads'],
            'observer entry partition differs')
    require(c['source_mirror'] == c['entry_candidate_to_mirror'] and
            c['source_mirror_outside_entry_candidates'] ==
            c['entry_claimed_mirrors'] == 0 and
            c['entry_candidate_to_l2'] <= c['source_l2'],
            'true decode source premise differs')
    direct = sum(c[k] for k in CANDIDATE_PARTS[2:4])
    require(direct <= c['source_read'] and direct <=
            c['writeback_pending_entry_candidate_targets'] <=
            c['writeback_pending_missing_targets'] <=
            c['writeback_reservations'] and
            c['writeback_published'] + c['writeback_aborted'] ==
            c['writeback_reservations'] and
            c['samples_confirmed_total'] == direct,
            'observer direct/reservation closure differs')
    return c


def validate_request(request):
    snapshots = request['snapshots']
    require([(s['event'], s['decode_forwards']) for s in snapshots] == ENDPOINTS
            and all(s['decode_forward_count'] == s['decode_forwards']
                    for s in snapshots), 'snapshot endpoints differ')
    aliases = dict(prefill_lookups='shape_multi_lookups',
        decode_lookups='shape_single_lookups',
        prefill_misses='shape_multi_misses', decode_misses='shape_single_misses')
    for i, snap in enumerate(snapshots):
        counts(snap['counters'])
        for key in COUNTERS:
            require(snap['counters'][key] == uint(
                snap['stats'][aliases.get(key, key)], key),
                'stats/counter projection differs')
        for key in SUPPLY:
            uint(snap['stats'][key], key)
        layers = snap['layers']
        if i in (0, 1, 5):
            require(isinstance(layers, list) and len(layers) == 48,
                    'full lower-cache checkpoint missing')
            for index, layer in enumerate(layers):
                lower_layer(layer, layer, index)
                require(len(layer['slot_experts']) == 256 and
                        not any(layer['slot_protected']),
                        'captured GPU dimension/protection differs')
        else:
            require(layers is None, 'prefix lower-cache state was invented')
    for before, after in zip(snapshots, snapshots[1:]):
        subtract(after['counters'], before['counters'])
        require(all(after['stats'][k] >= before['stats'][k] for k in SUPPLY),
                'source counter decreased')
    observer = request['observer_decode']
    require(observer['scope'] == 'actual_decode' and
            observer['true_decode_entry_premise_applied'] is True and
            observer['prefix_layer_deltas_available'] is False and
            len(observer['layers']) == 48, 'observer decode scope differs')
    aggregate = observer_counts(observer['counters'], 48 * 255)
    sums = Counter()
    for index, row in enumerate(observer['layers']):
        require(row['layer'] == index, 'observer layer order differs')
        c = observer_counts(row['counters'], 255)
        require(set(c) == set(aggregate), 'observer layer counter keys differ')
        sums.update(c)
    require(dict(sums) == aggregate, 'observer aggregate differs from layers')
    require(observer['direct_read_losses'] ==
            aggregate['entry_candidate_to_read_direct_active'] +
            aggregate['entry_candidate_to_read_direct_published'],
            'observer direct total differs')


def validate_manifest(manifest, scope_sha):
    require(manifest.get('schema') == 1 and
            manifest.get('scope_sha256') == scope_sha, 'manifest scope differs')
    ledger = manifest['source_sha256']
    validate_ledger(ledger)
    require([g['id'] for g in manifest['groups']] == GROUPS,
            'group set/order differs')
    for group in manifest['groups']:
        require(len(group['requests']) == 4, 'request count differs')
        for position, request in enumerate(group['requests']):
            wanted_input = 8193 if group['id'] == GROUPS[1] and position == 0 else 1024
            require(request['position'] == position and
                    request['input_tokens'] == wanted_input and
                    request['output_tokens'] == 256, 'request shape differs')
            validate_request(request)
            validation = request['validation']
            path = Path(request['trace_path'])
            require(path.is_absolute() and str(path) == validation['path'],
                    'trace validation path differs')
            package, metadata, summary = (validation[k] for k in
                                            ('manifest', 'metadata', 'summary'))
            require(package['schema'] == 1 and package['complete'] is True and
                    package['failure'] == 'none' and
                    [package[k] for k in ('layers', 'experts', 'top_k', 'max_rows')]
                    == [48, 512, 10, 8192] and
                    package['requests_started'] == package['requests_published'] == 4
                    and package['binary_sha256'] == manifest['runtime_binary_sha256'],
                    'trace package dimensions/completion differs')
            require(metadata['request_id'] == position + 1 and
                    metadata['prompt_tokens'] == wanted_input and
                    metadata['http_id'] == request['response_id'],
                    'trace request metadata differs')
            require(summary == dict(forwards=257 if wanted_input == 8193 else 256,
                prefill_rows=wanted_input, decode_rows=255,
                route_ids=(wanted_input + 255) * 48 * 10, output_tokens=256),
                'trace validated totals differ')
            wanted_paths = {str(path), str(path.with_suffix('.json')),
                           str(path.with_suffix('.tokens')),
                           str(path.parent / 'manifest.json')}
            require(set(validation['bindings']) == wanted_paths,
                    'trace binding set differs')
            for name, binding in validation['bindings'].items():
                require(binding['sha256'] == ledger.get(name) and
                        isinstance(binding['stat'], list) and
                        len(binding['stat']) == 4 and all(type(v) is int and v >= 0
                        for v in binding['stat']), 'trace binding identity differs')
            forwards = request['forwards']
            wanted = [(1, 0, min(8192, wanted_input))]
            if wanted_input == 8193:
                wanted.append((1, 8192, 1))
            wanted.extend((2, wanted_input + i, 1) for i in range(255))
            require([(f['stage'], f['position'], f['rows']) for f in forwards] == wanted
                and all(type(f['forward_id']) is int and f['forward_id'] > 0
                        for f in forwards) and all(a['forward_id'] < b['forward_id']
                        for a, b in zip(forwards, forwards[1:])),
                'frozen forward identities differ')


def supply_delta(after, before):
    result = {k: uint(after[k], k) - uint(before[k], k) for k in SUPPLY}
    require(all(v >= 0 for v in result.values()), 'supply counter decreased')
    return result


def analyze_request(group, request, output, plan_output):
    """Reconstruct true decode only, then require EOF and actual state closure."""
    identity = dict(group=group['id'], position=request['position'],
                    input_tokens=request['input_tokens'], output_tokens=256)
    output.update(identity, complete=False, iterator_exhausted=False)
    plan_output.update(identity, complete=False,
        layers=[dict(layer=i, plans=[]) for i in range(48)])
    validate_request(request)
    snapshots = request['snapshots']
    entry, end = snapshots[1], snapshots[5]
    states = [GpuCacheState(layer) for layer in entry['layers']]
    cumulative = Counter(dict.fromkeys(COUNTERS, 0))
    layer_counts = [Counter(dict.fromkeys(COUNTERS, 0)) for _ in range(48)]
    output['prefixes'] = []
    forwards, last_layer, decode_steps, prefill_rows = 0, 47, 0, 0
    for trace in iter_layers(request['trace_path'], request['validation']):
        layer = trace['layer']
        require(type(layer) is int and layer == (last_layer + 1) % 48,
                'trace layer order differs')
        last_layer = layer
        if layer == 0:
            forwards += 1
        require(forwards <= len(request['forwards']) and all(
            trace[k] == request['forwards'][forwards - 1][k]
            for k in ('forward_id', 'stage', 'position', 'rows')),
            'forward identity differs')
        require(trace['top_k'] == 10 and
                len(trace['topk_ids']) == trace['rows'] * 10,
                'trace shape differs')
        if trace['stage'] == 1:
            require(decode_steps == 0, 'prefill returned after decode')
            if layer == 47:
                prefill_rows += trace['rows']
            continue
        require(trace['stage'] == 2 and trace['rows'] == 1 and
                prefill_rows == request['input_tokens'] and decode_steps < 255,
                'true decode shape/order differs')
        needed = list(trace['topk_ids'])
        missing, victims, result = ordered_plan(states[layer], needed)
        plan_output['layers'][layer]['plans'].append(
            dict(needed=needed, missing=missing, victims=victims))
        selected = {k: result[k] for k in COUNTERS}
        cumulative.update(selected)
        layer_counts[layer].update(selected)
        if layer != 47:
            continue
        decode_steps += 1
        if decode_steps in (1, 8, 32, 255):
            index = (1, 8, 32, 255).index(decode_steps) + 2
            expected = subtract(snapshots[index]['counters'], entry['counters'])
            compare_counts(dict(cumulative), expected, 'decode prefix')
            source = supply_delta(snapshots[index]['stats'], entry['stats'])
            require(sum(source.values()) == cumulative['loads'],
                    'decode prefix source closure differs')
            output['prefixes'].append(dict(decode_forwards=decode_steps,
                                           counters=dict(cumulative), supply=source))
    output['iterator_exhausted'] = True
    require(last_layer == 47 and decode_steps == 255 and
            forwards == len(request['forwards']) and len(output['prefixes']) == 4,
            'trace iterator ended before complete decode')
    compare_states(states, end['layers'], 'decode exact GPU endpoint')
    compare_counts(dict(cumulative), subtract(end['counters'], entry['counters']),
                   'decode full phase')
    source = supply_delta(end['stats'], entry['stats'])
    observer = request['observer_decode']
    totals = observer['counters']
    require([totals[k] for k in ('source_l2', 'source_read', 'source_mirror')] ==
            [source[k] for k in SUPPLY] and totals['planned_loads'] ==
            cumulative['loads'] and totals['writeback_published'] ==
            end['stats']['mirror_writebacks'] - entry['stats']['mirror_writebacks']
            and totals['writeback_skipped'] == end['stats']['mirror_skips'] -
            entry['stats']['mirror_skips'], 'observed aggregate supply differs')
    rows = []
    for i, (a, b) in enumerate(zip(entry['layers'], end['layers'])):
        c = observer['layers'][i]['counters']
        require(c['planned_loads'] == layer_counts[i]['loads'] and
                b['slot_clock'] - a['slot_clock'] == 255 and
                b['l2_clock'] - a['l2_clock'] == c['source_l2'] + c['source_read']
                == observer['layers'][i]['l2_clock_delta'],
                'per-layer source/load/clock closure differs')
        row = analyze_layer(a, plan_output['layers'][i]['plans'],
                            c['entry_mirror_candidate'])
        rows.append(dict(layer=i, counters=dict(layer_counts[i]),
                         observed=c, opportunities=row))
    endpoints = [dict(event=s['event'], decode_forwards=s['decode_forwards'],
        layers=[dict(layer=i, **classify_snapshot(layer))
                for i, layer in enumerate(s['layers'])])
        for s in (snapshots[0], entry, end)]
    output.update(complete=True, exact_gpu_endpoint_checked=True,
        initialized_from_actual_decode_entry=True,
        lower_cache_history_simulated=False, counters=dict(cumulative),
        supply=source, observer_counters=totals, layers=rows, endpoints=endpoints)
    plan_output['complete'] = True


def comparisons(requests, plans, manifest):
    expected = [(group, k) for group in GROUPS for k in range(4)]
    require([(r['group'], r['position']) for r in requests] == expected and
            [(r['group'], r['position']) for r in plans] == expected and
            all(r['complete'] for r in requests + plans),
            'fixed eight-request collection incomplete')
    rows, original = [], {(g['id'], q['position']): q
                        for g in manifest['groups'] for q in g['requests']}
    by_result = {(r['group'], r['position']): r for r in requests}
    by_plan = {(r['group'], r['position']): r for r in plans}
    for k in range(4):
        s, l = (by_result[g, k] for g in GROUPS)
        sp, lp = (by_plan[g, k] for g in GROUPS)
        sq, lq = (original[g, k] for g in GROUPS)
        layers = []
        for i in range(48):
            comp = compare_layers(sq['snapshots'][1]['layers'][i],
                sp['layers'][i]['plans'],
                s['layers'][i]['observed']['entry_mirror_candidate'],
                lq['snapshots'][1]['layers'][i], lp['layers'][i]['plans'],
                l['layers'][i]['observed']['entry_mirror_candidate'])
            layers.append(dict(layer=i, **comp))
        interval = {key: sum(row['common_candidate_difference_interval'][key]
                            for row in layers) for key in ('lower', 'upper')}
        mixed = []
        for result in (s, l):
            for layer in result['endpoints'][1]['layers']:
                if layer['counts']['gpu_only'] + layer['counts']['both'] > 0 and \
                        layer['counts']['sole'] > 0:
                    mixed.append(dict(group=result['group'], layer=layer['layer']))
        token_shas = [q['validation']['bindings'][str(
            Path(q['trace_path']).with_suffix('.tokens'))]['sha256']
            for q in (sq, lq)]
        rows.append(dict(position=k, input_tokens_S=s['input_tokens'],
            input_tokens_L=l['input_tokens'],
            input_token_sha256_S=token_shas[0],
            input_token_sha256_L=token_shas[1],
            same_input_tokens=token_shas[0] == token_shas[1],
            same_input_length=s['input_tokens'] == l['input_tokens'],
            route_equal=all(row['route_equal'] for row in layers),
            observed_candidate_difference=s['observer_counters']['entry_mirror_candidate']
                - l['observer_counters']['entry_mirror_candidate'],
            common_candidate_difference_interval=interval,
            mixed_GPU_covered_and_sole_decode_entry_layers=mixed, layers=layers))
    primary = rows[1]
    nominate = (primary['same_input_length'] and primary['same_input_tokens']
        and primary['route_equal'] and
        primary['common_candidate_difference_interval']['lower'] > 0 and
        bool(primary['mixed_GPU_covered_and_sole_decode_entry_layers']))
    return rows, ('FUTURE_GPU_COVERED_MIRROR_RECYCLING_CANDIDATE_ONLY' if nominate
                  else 'DEMAND_RETENTION_NON_IDENTIFIABLE_NO_CANDIDATE')


def artifact_path(path, workspace):
    path = path.resolve()
    require(path.is_relative_to(workspace.parent) and
            (not path.is_relative_to(workspace) or
             path.is_relative_to(workspace / '.q4t-work')),
            'output outside artifact roots')
    return path


def execute(args):
    workspace = Path(__file__).resolve().parents[2]
    output = artifact_path(args.output, workspace)
    plans_path = artifact_path(args.plans_output, workspace)
    require(output != plans_path and not output.exists() and not plans_path.exists(),
            'outputs exist or alias; retain prior evidence')
    report = dict(schema=1, started_t=time.time(), passed=False, failure=None,
        scope_sha256=args.scope_sha256, manifest_sha256=args.manifest_sha256,
        execution_plan_sha256=args.execution_plan_sha256, requests=[],
        model_runs=0, HTTP_requests=0, runtime_changes=False,
        performance_acceptance=False, physical_IO_prediction=False,
        source_sha256={}, limits=LIMITS)
    plans = dict(schema=1, passed=False, requests=[],
                 manifest_sha256=args.manifest_sha256,
                 scope_sha256=args.scope_sha256,
                 execution_plan_sha256=args.execution_plan_sha256)
    with output.open('x') as destination, plans_path.open('x') as plan_dest:
        try:
            scope = read_bound(args.scope, args.scope_sha256)
            validate_scope(scope)
            execution = read_bound(args.execution_plan, args.execution_plan_sha256)
            sources = validate_execution(execution, args.scope_sha256,
                                         args.manifest_sha256, workspace)
            require(all(sources.get(p) == h
                for p, h in scope['source_sha256'].items()),
                'scope sources differ from execution ledger')
            manifest = read_bound(args.manifest, args.manifest_sha256)
            validate_manifest(manifest, args.scope_sha256)
            combined = dict(sources)
            for path, identity in manifest['source_sha256'].items():
                require(path not in combined or combined[path] == identity,
                        'conflicting evidence SHA binding')
                combined[path] = identity
            own_bindings = {str(args.scope.resolve()): args.scope_sha256,
                str(args.manifest.resolve()): args.manifest_sha256,
                str(args.execution_plan.resolve()): args.execution_plan_sha256,
                str(Path(execution['validation_results_path']).resolve()):
                execution['validation_results_sha256']}
            for path, identity in own_bindings.items():
                require(path not in combined or combined[path] == identity,
                        'conflicting plan/validation SHA binding')
                combined[path] = identity
            for path, identity in combined.items():
                require(sha(path) == identity, 'source changed before run: ' + path)
            for group in manifest['groups']:
                for request in group['requests']:
                    result, projected = {}, {}
                    report['requests'].append(result)
                    plans['requests'].append(projected)
                    analyze_request(group, request, result, projected)
            contrasts, decision = comparisons(report['requests'], plans['requests'],
                                                manifest)
            for path, identity in combined.items():
                require(sha(path) == identity, 'source changed during run: ' + path)
            report.update(comparisons=contrasts, decision=decision,
                hypothesis_identified=False, runtime_candidate_admitted=False,
                source_sha256=combined, passed=True,
                status='MIRROR_RETENTION_OPPORTUNITIES_BOUNDED',
                reconstructed_decode_layer_plans=sum(len(layer['plans'])
                    for req in plans['requests'] for layer in req['layers']))
            require(report['reconstructed_decode_layer_plans'] == 97920,
                    'decode plan total differs')
            plans['passed'] = True
        except BaseException as error:
            report.update(passed=False, status='FAIL_RETAINED',
                          failure=type(error).__name__ + ': ' + str(error))
            plans['passed'] = False
        report['ended_t'] = time.time()
        json.dump(plans, plan_dest, separators=(',', ':'), allow_nan=False)
        plan_dest.write('\n')
        plan_dest.flush()
        report['plans_output'] = str(plans_path)
        report['plans_sha256'] = sha(plans_path)
        json.dump(report, destination, indent=2, allow_nan=False)
        destination.write('\n')
    return 0 if report['passed'] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('manifest', 'scope', 'execution_plan'):
        flag = name.replace('_', '-')
        parser.add_argument('--' + flag, type=Path, required=True)
        parser.add_argument('--' + flag + '-sha256', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--plans-output', type=Path, required=True)
    return execute(parser.parse_args())


if __name__ == '__main__':
    raise SystemExit(main())
