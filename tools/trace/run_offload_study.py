"""Frozen-route offload study; never starts a model or predicts latency."""
import argparse
from array import array
from collections import Counter, defaultdict
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys
import time

from analyze import sha
from offload_replay import ForwardTrace, ReplayConfig, replay
from offload_trace import iter_layers, validate_request
from offload_working_set import (WorkingSetObserver, contiguous_next_by_layer,
                                 validate_layout)


def check_identity(item):
    path = Path(item['path']).resolve()
    if sha(path) != item['sha256']:
        raise ValueError('identity mismatch: ' + str(path))
    return path


def validate_plan(plan):
    if (plan.get('schema') != 1 or
            plan.get('policies') != ['baseline', 'greedy_overlap'] or
            plan.get('schedules') != ['serial_eager', 'batch_deferred']):
        raise ValueError('unsupported frozen policies/schedules')
    cfg = plan['config']
    if cfg != dict(capacity=256, l2_slots=16, mirror_slots=8,
                   hot_protect=False, phase_d=False):
        raise ValueError('unsupported deployment configuration')
    requests = plan['requests']
    if len(requests) != 2 or [r['repeats'] for r in requests] != [2, 1]:
        raise ValueError('expected primary continuous pair and independent holdout')
    if (len({r['name'] for r in requests}) != len(requests) or
            len({r['sha256'] for r in requests}) != len(requests)):
        raise ValueError('duplicate study name or source')
    for item in [plan[k] for k in ('hot_list', 'source_binary', 'checker',
                                  'sort_helper', 'current_binary')] + requests:
        check_identity(item)


def token_order(ids, rows, helper):
    if len(ids) != rows * 10 or rows <= 0 or rows > 8192:
        raise ValueError('invalid token dimensions')
    if rows == 1:
        return [0]
    values = array('H', ids)
    if sys.byteorder != 'little':
        values.byteswap()
    output = subprocess.run(
        [str(helper)], input=struct.pack('<II', rows, 10) + values.tobytes(),
        capture_output=True, check=True, timeout=30).stdout
    if len(output) != rows * 4:
        raise ValueError('invalid C++ sort result size')
    order = array('I')
    order.frombytes(output)
    if sys.byteorder != 'little':
        order.byteswap()
    if sorted(order) != list(range(rows)):
        raise ValueError('C++ sort did not return a permutation')
    return order


def save(path, value):
    with path.open('x') as file:
        json.dump(value, file, indent=2)
        file.write('\n')


def summarize(result):
    requests = defaultdict(lambda: defaultdict(Counter))
    oracle = defaultdict(Counter)
    rows = []
    for forward in result.forwards:
        requests[forward.request_id][forward.phase].update(forward.counts)
        oracle[forward.request_id][forward.phase] += (
            forward.fixed_partition_oracle_gpu_misses)
        row = asdict(forward)
        # Preserve identities without repeating 512 integer slot IDs per layer.
        for key in ['resident_before', 'resident_after']:
            row[key + '_sha256'] = hashlib.sha256(
                json.dumps(row.pop(key), separators=(',', ':')).encode()).hexdigest()
        rows.append(row)
    by_request = []
    for request_id, phases in requests.items():
        by_request.append(dict(
            request_id=request_id,
            phases={p: dict(counts) for p, counts in phases.items()},
            same_entry_fixed_order_oracle_gpu_misses=dict(oracle[request_id])))
    return dict(aggregate=result.aggregate,
                by_trace_phase=result.by_trace_phase,
                by_runtime_phase=result.by_runtime_phase,
                by_request=by_request, assumptions=result.assumptions,
                final_states=result.final_states), rows


def execute(plan_path, layout_path, output):
    root = Path(__file__).resolve().parents[2]
    output = output.resolve()
    if not any(output.is_relative_to(root / p) for p in ['build', '.q4t-work']):
        raise ValueError('output must be under build/ or .q4t-work/')
    output.mkdir(parents=True, exist_ok=False)
    status = dict(complete=False, started=time.time(), runtime_changed=False,
                  new_HTTP_executed=False, performance_acceptance=False,
                  physical_RAM_total='INDETERMINATE', completed_variants=[])
    try:
        plan = json.loads(plan_path.read_text())
        validate_plan(plan)
        layout = json.loads(layout_path.read_text())
        save(output / 'layout-validation.json', validate_layout(layout))
        hot = json.loads(check_identity(plan['hot_list']).read_text())
        if (set(hot) != {str(i) for i in range(48)} or
                any(len(v) != 256 or len(set(v)) != 256 or
                    any(type(e) is not int or e < 0 or e >= 512 for e in v)
                    for v in hot.values())):
            raise ValueError('invalid frozen initial hot list')
        hot = {i: hot[str(i)] for i in range(48)}
        config = ReplayConfig(
            contiguous_next_by_layer=contiguous_next_by_layer(layout))
        helper = check_identity(plan['sort_helper'])
        save(output / 'plan.json', plan)
        save(output / 'tool-identities.json', dict(
            plan_sha256=sha(plan_path), layout_sha256=sha(layout_path),
            tools={p.name: sha(p) for p in Path(__file__).parent.glob('offload*')
                   if p.is_file()}, run_study_sha256=sha(Path(__file__))))
        all_results = []
        for selection in plan['requests']:
            name = selection['name']
            source = check_identity(selection)
            validation = validate_request(
                source, checker=check_identity(plan['checker']),
                source_binary=check_identity(plan['source_binary']),
                expected_sha256=selection['sha256'])
            if (validation['manifest']['model_index_sha256'] !=
                    layout['model_index_sha256'] or
                    validation['manifest']['model_config_sha256'] !=
                    layout['config_sha256']):
                raise ValueError('route and weight-layout identity mismatch')
            save(output / (name + '-source.json'), validation)
            forwards = []
            for layer in iter_layers(source, validation):
                ids = layer['topk_ids']
                forwards.append(ForwardTrace(
                    layer=layer['layer'], forward_id=str(layer['forward_id']),
                    request_id=name + '-r1',
                    phase='prefill' if layer['stage'] == 1 else 'decode',
                    topk_ids=ids,
                    token_order=token_order(ids, layer['rows'], helper)))
            print('validated and ordered', name, len(forwards), flush=True)
            for schedule in plan['schedules']:
                for policy in plan['policies']:
                    key = name + '-' + schedule + '-' + policy
                    print('replay start', key, flush=True)
                    observer = WorkingSetObserver(
                        layout, plan['object_cache_capacities_bytes'],
                        schedule_label=schedule)
                    def repeated():
                        for repeat in range(selection['repeats']):
                            for forward in forwards:
                                yield replace(forward, request_id=(
                                    name + '-r' + str(repeat + 1)))
                    started = time.monotonic()
                    result = replay(repeated(), replace(config, schedule=schedule),
                                    hot, policy=policy, observer=observer)
                    if policy == 'baseline' and any(
                            f.counts['gpu_misses'] < f.fixed_partition_oracle_gpu_misses
                            for f in result.forwards):
                        raise ValueError('baseline violates fixed-order miss oracle')
                    summary, details = summarize(result)
                    save(output / (key + '-forwards.json'), details)
                    working_set = observer.finish()
                    save(output / (key + '-working-set.json'), working_set)
                    save(output / (key + '-summary.json'), summary)
                    all_results.append(dict(name=name, schedule=schedule,
                                            policy=policy, **summary))
                    status['completed_variants'].append(key)
                    print('replay done', key, 'seconds',
                          round(time.monotonic() - started, 2), flush=True)
            check_identity(selection)
        save(output / 'results.json', all_results)
        # Every frozen source and binary must remain unchanged at delivery.
        validate_plan(plan)
        status['complete'] = True
        return 0
    except Exception as error:
        status['failure'] = type(error).__name__ + ': ' + str(error)
        print(status['failure'], file=sys.stderr, flush=True)
        return 1
    finally:
        status['ended'] = time.time()
        save(output / 'exit.json', status)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--plan', type=Path, required=True)
    ap.add_argument('--layout', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    return execute(args.plan, args.layout, args.output)


if __name__ == '__main__':
    sys.exit(main())
