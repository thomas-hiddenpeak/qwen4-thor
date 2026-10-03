"""One frozen min-new partition study; no model service or GPU execution.

Build is explicit. Run requires a saved NO_GO decision for the first online
candidate. Only two first-8192-row prefill forwards (48 layers each) are replayed.
The rest of each source is validated, not evaluated. No parameter search.
"""

import argparse
from array import array
from collections import Counter
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import time

from analyze import sha
from offload_replay import ForwardTrace, ReplayConfig, partition_forward
from offload_trace import iter_layers, validate_request
from run_offload_study import token_order


ROOT = Path(__file__).resolve().parents[2]
DIMENSIONS = dict(experts=512, capacity=256, topk=10, rows=8192, layers=48)
THRESHOLDS = dict(minimum_gpu_miss_reduction_percent=15,
                  structural_bound_must_decrease=True,
                  chunk_count_must_not_increase=True,
                  fallback_layers_must_equal=0)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def save(path, value):
    with Path(path).open('x') as file:
        json.dump(value, file, indent=2)
        file.write('\n')


def artifact_path(path):
    path = Path(path).resolve()
    require(any(path.is_relative_to(ROOT / p) for p in ('build', '.q4t-work')),
            'artifacts must be under build/ or .q4t-work/')
    return path


def verify_identity(item):
    path = Path(item['path']).resolve()
    require(sha(path) == item['sha256'], 'identity mismatch: ' + str(path))
    return path


def validate_plan(plan, *, check_sources=True):
    require(plan.get('schema') == 1 and plan.get('algorithm') == 'min_new_csr_v1',
            'unsupported partition plan')
    require(plan.get('dimensions') == DIMENSIONS and
            plan.get('work_budget_multiplier') == 32 and
            plan.get('thresholds') == THRESHOLDS,
            'partition dimensions, work budget or gates changed')
    require(plan.get('execution_order') == 'construction_order' and
            plan.get('tie_break') == 'original_cpp_lex_rank' and
            plan.get('initial_state') == 'reset_same_hot_per_sample_and_policy' and
            plan.get('required_initial_decision') == 'NO_GO',
            'partition scheduling/state contract changed')
    requests = plan.get('requests', [])
    require(len(requests) == 2 and
            [r.get('name') for r in requests] ==
            ['external-45056', 'business-fv-long-first'] and
            all(r.get('selection') == 'first_committed_prefill_forward_8192'
                for r in requests) and
            len({r['sha256'] for r in requests}) == 2,
            'expected two distinct frozen first-prefill sources')
    require(plan.get('runtime_changed') is False and
            plan.get('physical_io_prediction') is False,
            'scope must remain offline GPU residency only')
    if check_sources:
        require(plan.get('tool_sources'), 'missing tool source identities')
        for item in plan['tool_sources']:
            verify_identity(item)


def build(plan_path, build_dir):
    """Compile only; never execute a contract or read model/trace payloads."""
    plan_path, build_dir = Path(plan_path).resolve(), artifact_path(build_dir)
    build_dir.mkdir(parents=True, exist_ok=False)
    record = dict(schema=1, complete=False, tests_executed=False,
                  plan_sha256=None, commands=[])
    try:
        record['plan_sha256'] = sha(plan_path)
        plan = json.loads(plan_path.read_text())
        validate_plan(plan)
        record['sources'] = plan['tool_sources']
        compiler = shutil.which('g++-14')
        require(compiler is not None, 'g++-14 compiler unavailable')
        record['compiler'] = compiler
        version = subprocess.run([compiler, '--version'], capture_output=True,
                                 text=True, check=True, timeout=30)
        record['compiler_version'] = version.stdout
        log = build_dir / 'build.log'
        with log.open('x') as output:
            for source, name in [('offload_partition.cpp', 'offload_partition'),
                                 ('test_offload_partition.cpp',
                                  'test_offload_partition')]:
                command = [compiler, '-std=c++23', '-O2', '-Wall', '-Wextra',
                           '-Werror', str(ROOT / 'tools/trace' / source),
                           '-o', str(build_dir / name)]
                record['commands'].append(command)
                process = subprocess.run(command, stdout=output, stderr=output,
                                         timeout=120)
                require(process.returncode == 0, 'partition host build failed')
        require(log.stat().st_size == 0, 'host build emitted unexpected output')
        record['binaries'] = {
            name: dict(path=str(build_dir / name), sha256=sha(build_dir / name))
            for name in ('offload_partition', 'test_offload_partition')}
        validate_plan(plan)
        record['complete'] = True
        return 0
    except Exception as error:
        record['failure'] = type(error).__name__ + ': ' + str(error)
        print(record['failure'], file=sys.stderr)
        return 1
    finally:
        save(build_dir / 'build.json', record)


def validate_chunks(chunks, ids, topk, capacity):
    rows = len(ids) // topk
    require(isinstance(chunks, list) and chunks, 'empty partition')
    seen = bytearray(rows)
    sets = []
    for chunk in chunks:
        require(isinstance(chunk, list) and chunk, 'empty chunk')
        needed = set()
        for row in chunk:
            require(type(row) is int and 0 <= row < rows and not seen[row],
                    'duplicate or invalid row in partition')
            seen[row] = 1
            needed.update(ids[row * topk:(row + 1) * topk])
        require(len(needed) <= capacity, 'chunk exceeds expert capacity')
        sets.append(needed)
    require(all(seen), 'partition omitted a row')
    return sets


def gpu_replay(chunks, ids, topk, capacity, hot):
    """Phase-D-off PlanResolve identities/ticks, no L2, I/O or timing model.

    InitHot's final slot ticks follow hot-list order. Needed-mark and in-call
    reservations exclude every requested expert and every earlier reserved slot.
    Duplicate requests become planned hits. All plan commits finish before the
    next chunk. Results carry across chunks, not between independent samples.
    """
    sets = validate_chunks(chunks, ids, topk, capacity)
    require(len(hot) <= capacity and len(set(hot)) == len(hot), 'invalid hot list')
    slots = list(hot) + [-1] * (capacity - len(hot))
    ticks = list(range(1, len(hot) + 1)) + [0] * (capacity - len(hot))
    positions = {e: s for s, e in enumerate(hot)}
    tick, misses, runtime_decode_misses = len(hot), 0, 0
    for chunk, needed in zip(chunks, sets):
        tick += 1
        ordered = dict.fromkeys(e for row in chunk
                                for e in ids[row * topk:(row + 1) * topk])
        empty = [s for s, e in enumerate(slots) if e < 0]
        eligible = sorted((s for s, e in enumerate(slots)
                           if e >= 0 and e not in needed),
                          key=lambda s: (ticks[s], s))
        victims, plan = iter(empty + eligible), []
        for expert in ordered:
            if expert in positions:
                ticks[positions[expert]] = tick
            else:
                victim = next(victims, None)
                require(victim is not None, 'GPU plan has no eligible slot')
                plan.append((expert, victim))
        misses += len(plan)
        if len(chunk) == 1:
            runtime_decode_misses += len(plan)
        for expert, slot in plan:
            if slots[slot] >= 0:
                del positions[slots[slot]]
            slots[slot], positions[expert], ticks[slot] = expert, slot, tick
    lower_bound = sum(max(0, len(a | b) - capacity)
                      for a, b in zip(sets, sets[1:]))
    require(lower_bound <= misses, 'GPU misses violate transition lower bound')
    return dict(gpu_misses=misses, subchunks=len(chunks),
                transition_lower_bound=lower_bound,
                singleton_runtime_decode_misses=runtime_decode_misses,
                true_prefill_lookups=len(ids),
                sum_chunk_distinct_experts=sum(len(s) for s in sets),
                final_gpu_slots_sha256=hashlib.sha256(
                    json.dumps(slots, separators=(',', ':')).encode()).hexdigest())


def helper_output(raw, trace):
    value = json.loads(raw)
    require(value.get('schema') == 1 and type(value.get('fallback')) is bool,
            'invalid partition helper schema')
    config = ReplayConfig(capacity=256, pread_merge=False)
    original = [list(c.token_indices) for c in partition_forward(trace, config)]
    require(value['baseline_chunks'] == original,
            'C++ baseline partition differs from validated runtime replay')
    for key in ('baseline_chunks', 'candidate_chunks'):
        validate_chunks(value[key], trace.topk_ids, trace.topk, config.capacity)
    counters = value['counters']
    keys = {'csr_visits', 'reset_rows', 'bucket_adds', 'bucket_removes',
            'bucket_summary_checks', 'bucket_word_clears', 'selections',
            'work_budget', 'work_used', 'metadata_payload_bytes'}
    require(set(counters) == keys and
            all(type(counters[k]) is int and counters[k] >= 0 for k in keys),
            'missing or invalid operation counters')
    require(counters['work_budget'] == 32 * len(trace.topk_ids) and
            counters['work_used'] == counters['csr_visits'] + counters['reset_rows']
            and counters['work_used'] <= counters['work_budget'],
            'partition work budget violated')
    for key in ('baseline_us', 'candidate_us'):
        require(type(value.get(key)) is int and value[key] >= 0,
                'invalid helper CPU timing')
    if value['fallback']:
        require(value['candidate_chunks'] == original and
                counters['work_used'] == counters['work_budget'],
                'fallback must discard the entire candidate partition')
    else:
        require(counters['selections'] == len(trace.token_order),
                'candidate did not select every row')
    return value


def sample_decision(baseline, candidate, fallback_layers):
    b, c = baseline['gpu_misses'], candidate['gpu_misses']
    gates = dict(gpu_miss_reduction_at_least_15_percent=(
        b > 0 and c * 100 <= b * 85),
        structural_bound_decreased=(candidate['transition_lower_bound'] <
                                    baseline['transition_lower_bound']),
        chunk_count_did_not_increase=(candidate['subchunks'] <=
                                     baseline['subchunks']),
        no_fallback=(fallback_layers == 0))
    return dict(go=all(gates.values()), gates=gates,
                gpu_miss_reduction_percent=(100 * (b - c) / b if b else None))


def first_prefill_layers(path, validation):
    selected, first = [], None
    # Exhaust the validated parser. Later forwards are not partitioned/replayed.
    for layer in iter_layers(path, validation):
        if first is None:
            require(layer['stage'] == 1 and layer['position'] == 0 and
                    layer['rows'] == 8192, 'first forward is not prefill8192')
            first = layer['forward_id']
        if layer['forward_id'] == first:
            selected.append(layer)
    require(len(selected) == 48 and [r['layer'] for r in selected] == list(range(48)),
            'first forward must contain exactly all 48 layers')
    return selected


def run(plan_path, build_manifest, initial_decision, output):
    plan_path, output = Path(plan_path).resolve(), artifact_path(output)
    output.mkdir(parents=True, exist_ok=False)
    status = dict(complete=False, started=time.time(), runtime_changed=False,
                  HTTP_executed=False, GPU_executed=False,
                  performance_acceptance=False, completed_samples=[])
    try:
        decision_path = Path(initial_decision).resolve()
        require(json.loads(decision_path.read_text()).get('decision') == 'NO_GO',
                'first online candidate must have a saved NO_GO decision')
        decision_sha, plan_sha = sha(decision_path), sha(plan_path)
        plan = json.loads(plan_path.read_text())
        validate_plan(plan)
        built = json.loads(Path(build_manifest).read_text())
        require(built.get('complete') is True and built.get('sources') ==
                plan['tool_sources'] and built.get('plan_sha256') == sha(plan_path),
                'helper build does not bind the frozen plan and sources')
        helper = verify_identity(built['binaries']['offload_partition'])
        sources = {k: verify_identity(plan[k]) for k in
                   ('hot_list', 'source_binary', 'checker', 'sort_helper')}
        hot = json.loads(sources['hot_list'].read_text())
        require(set(hot) == {str(i) for i in range(48)} and all(
            len(v) == 256 and len(set(v)) == 256 and
            all(type(e) is int and 0 <= e < 512 for e in v) for v in hot.values()),
            'invalid frozen hot list')
        save(output / 'plan.json', plan)
        save(output / 'identity.json', dict(
            plan_sha256=plan_sha, build_manifest_sha256=sha(build_manifest),
            initial_decision_path=str(decision_path),
            initial_decision_sha256=decision_sha,
            helper=built['binaries']['offload_partition']))
        reports = []
        for request in plan['requests']:
            source = verify_identity(request)
            validation = validate_request(
                source, checker=sources['checker'], source_binary=sources['source_binary'],
                expected_sha256=request['sha256'])
            require(validation['manifest']['model_index_sha256'] ==
                    plan['model_index_sha256'] and
                    validation['manifest']['model_config_sha256'] ==
                    plan['model_config_sha256'], 'frozen model identity mismatch')
            save(output / (request['name'] + '-source.json'), validation)
            layers = first_prefill_layers(source, validation)
            baseline, candidate, operations = Counter(), Counter(), Counter()
            details, fallback_layers, peak_payload = [], 0, 0
            for layer in layers:
                ids = layer['topk_ids']
                order = token_order(ids, 8192, sources['sort_helper'])
                trace = ForwardTrace(layer['layer'], str(layer['forward_id']),
                                     request['name'], 'prefill', ids, 10, order)
                id_bytes, order_bytes = array('H', ids), array('I', order)
                if sys.byteorder != 'little':
                    id_bytes.byteswap()
                    order_bytes.byteswap()
                payload = struct.pack('<4I', 512, 256, 10, 8192)
                payload += id_bytes.tobytes() + order_bytes.tobytes()
                command = [str(helper)]
                started = time.monotonic()
                try:
                    process = subprocess.run(command, input=payload,
                                             capture_output=True, timeout=30)
                    stdout, stderr, rc = (process.stdout, process.stderr,
                                          process.returncode)
                    timed_out = False
                except subprocess.TimeoutExpired as error:
                    stdout, stderr, rc = error.stdout or b'', error.stderr or b'', None
                    timed_out = True
                # Preserve even rejected helper evidence, with a unique name.
                prefix = f"{request['name']}-layer-{layer['layer']:02}"
                with (output / (prefix + '-helper.stdout')).open('xb') as file:
                    file.write(stdout)
                with (output / (prefix + '-helper.stderr')).open('xb') as file:
                    file.write(stderr)
                save(output / (prefix + '-helper-exit.json'), dict(
                    command=command, exit_code=rc, timed_out=timed_out,
                    wall_seconds=time.monotonic() - started))
                require(not timed_out and rc == 0 and not stderr,
                        'partition helper failed or emitted diagnostics')
                value = helper_output(stdout, trace)
                per_layer = dict(layer=layer['layer'], forward_id=layer['forward_id'],
                                 fallback=value['fallback'], counters=value['counters'],
                                 baseline_us=value['baseline_us'],
                                 candidate_us=value['candidate_us'])
                for name, totals in [('baseline', baseline), ('candidate', candidate)]:
                    stats = gpu_replay(value[name + '_chunks'], ids, 10, 256,
                                       hot[str(layer['layer'])])
                    per_layer[name] = stats
                    totals.update({k: v for k, v in stats.items() if type(v) is int})
                details.append(per_layer)
                fallback_layers += value['fallback']
                operations.update({k: v for k, v in value['counters'].items()
                                   if k != 'metadata_payload_bytes'})
                peak_payload = max(peak_payload,
                                   value['counters']['metadata_payload_bytes'])
                operations['baseline_us'] += value['baseline_us']
                operations['candidate_us'] += value['candidate_us']
            report = dict(name=request['name'], layers=48, rows_per_layer=8192,
                          baseline=dict(baseline), candidate=dict(candidate),
                          operations=dict(operations), fallback_layers=fallback_layers,
                          max_metadata_payload_bytes=peak_payload,
                          metadata_scope='planner vector payload; excludes input/output/allocator; not RSS',
                          decision=sample_decision(baseline, candidate, fallback_layers))
            save(output / (request['name'] + '-layers.json'), details)
            save(output / (request['name'] + '-summary.json'), report)
            verify_identity(request)
            reports.append(report)
            status['completed_samples'].append(request['name'])
        validate_plan(plan)
        verify_identity(built['binaries']['offload_partition'])
        for key in ('hot_list', 'source_binary', 'checker', 'sort_helper'):
            verify_identity(plan[key])
        require(sha(plan_path) == plan_sha and sha(decision_path) == decision_sha,
                'frozen plan or initial decision changed during study')
        save(output / 'decision.json', dict(
            decision=('OFFLINE_GO_FOR_CONSIDERATION' if all(r['decision']['go']
                      for r in reports) else 'NO_GO'), samples=reports,
            online_candidate_accepted=False, total_layers=96,
            route_scope='two frozen first-prefill forwards; not whole requests',
            GPU_residency_only=True, physical_IO_or_TTFT_prediction=False,
            numerical_or_HTTP_validation=False))
        status['complete'] = True
        return 0
    except Exception as error:
        status['failure'] = type(error).__name__ + ': ' + str(error)
        print(status['failure'], file=sys.stderr)
        return 1
    finally:
        status['ended'] = time.time()
        save(output / 'exit.json', status)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest='command', required=True)
    build_ap = sub.add_parser('build')
    build_ap.add_argument('--plan', type=Path, required=True)
    build_ap.add_argument('--build-dir', type=Path, required=True)
    run_ap = sub.add_parser('run')
    run_ap.add_argument('--plan', type=Path, required=True)
    run_ap.add_argument('--build-manifest', type=Path, required=True)
    run_ap.add_argument('--initial-decision', type=Path, required=True)
    run_ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    if args.command == 'build':
        return build(args.plan, args.build_dir)
    return run(args.plan, args.build_manifest, args.initial_decision, args.output)


if __name__ == '__main__':
    sys.exit(main())
