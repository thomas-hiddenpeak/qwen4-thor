"""Read partial or complete sealed startup-plan evidence without sampling.

This tool performs no CUDA, NVML, process launch, cache manipulation or sampling.
Final sealing is explicit. Historical passes do not accept a different binary.
Timing is descriptive; separate metadata planning time remains unknown.
"""
import argparse
import ast
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import re
import shutil
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
BASE = ROOT / '.q4t-work/startup-plan-20261020'
spec = importlib.util.spec_from_file_location('startup_plan_runner', HERE / 'run.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
sys.path.insert(0, str(ROOT / 'tools/verify/vmm_compare'))
from trace_audit import audit as audit_logical_trace

STAGES = ('mtp-order-v1', 'restart-order-v1', 'boundary-order-v1')
HISTORY = ('plain-v1', 'mtp-v1', 'mtp-v2', 'mtp-v3')
PREPARATION_FAILURES = {'mtp-v1': 3506176, 'mtp-v2': 79032320}
DEFAULT = '19ea4e57bf3a0e4f568a3e59c31ccbb4e71ad535db997682d8160d4be2f9c469'
SHA_PATTERN = re.compile('[0-9a-f]{64}')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'duplicate JSON key: ' + key)
        result[key] = value
    return result


def decode(text):
    return json.loads(text, object_pairs_hook=unique_keys,
                      parse_constant=lambda value: (_ for _ in ()).throw(
                          ValueError('nonfinite JSON value: ' + value)))


def read(path):
    return decode(path.read_text())


def write_new(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x') as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write('\n')


def finite(value):
    return type(value) in (float, int) and math.isfinite(value)


class SealedStage:
    def __init__(self, path):
        self.path = path.resolve(strict=True)
        self.manifest_path = self.path / 'sealed-files.json'
        self.manifest = read(self.manifest_path)
        require(isinstance(self.manifest, dict) and self.manifest,
                'empty stage seal: ' + str(path))
        actual = {str(p.relative_to(self.path)) for p in self.path.rglob('*')
                  if p.is_file() and p != self.manifest_path}
        require(actual == set(self.manifest), 'stage file set differs: ' + str(path))
        for relative, digest in self.manifest.items():
            target = self.path / relative
            if isinstance(digest, dict):
                require(Path(digest['path']).resolve() == target.resolve() and
                        target.stat().st_size == digest['bytes'],
                        'sealed path/size identity differs: ' + relative)
                digest = digest['sha256']
                self.manifest[relative] = digest
            require(target.resolve().is_relative_to(self.path) and
                    isinstance(digest, str) and SHA_PATTERN.fullmatch(digest),
                    'invalid sealed file identity: ' + relative)
            require(sha(target) == digest, 'sealed file changed: ' + str(target))
        self.used = set()

    def file(self, relative):
        require(relative in self.manifest, 'unsealed input: ' + relative)
        self.used.add(relative)
        return self.path / relative

    def document(self, relative):
        return read(self.file(relative))

    def text(self, relative):
        return self.file(relative).read_text()

    def lines(self, relative):
        return [decode(line) for line in self.text(relative).splitlines() if line]


def one_event(rows, event):
    selected = [row for row in rows if row['event'] == event]
    require(len(selected) == 1, 'expected one event: ' + event)
    return selected[0]


def expected_configuration(name):
    require(name in (*HISTORY, *STAGES), 'unknown matrix configuration')
    boundary = name == 'boundary-order-v1'
    return dict(max_seq=2 if boundary else 1,
                max_prefill=2048 if boundary else 8192,
                max_len=8192 if boundary else 262144,
                resident_slots=128 if boundary else 256,
                mtp=name != 'plain-v1' and not boundary, mtp_k=3,
                l2_slots=1 if boundary else 16, mirror_k=0 if boundary else 8,
                load_threads=1 if boundary else 16,
                requests=2 if name.startswith('mtp-') else 1)


def frozen_helpers(stage, build):
    # Compile only known pure function definitions, never the frozen driver's
    # imports, main, process controls, cache preparation or sampling functions.
    relative = 'source/tools/verify/startup_plan/run.py'
    frozen = ast.parse(stage.text(relative))
    names = ('require', 'read', 'records', 'capacity_evidence', 'check_plan',
             'reservation_evidence', 'environment', 'server_command',
             'configuration', 'binary_for', 'port_for', 'physical_order_evidence')
    functions = [node for node in frozen.body
                 if isinstance(node, ast.FunctionDef) and node.name in names]
    stages = next(node.value for node in frozen.body
                  if isinstance(node, ast.Assign) and any(
                      isinstance(target, ast.Name) and target.id == 'STAGES'
                      for target in node.targets))
    namespace = dict(json=json, Path=Path, re=re, hashlib=hashlib, ROOT=ROOT,
                     BASE=BASE, BINARY=Path(build['candidate']['path']),
                     MODEL=runner.MODEL, STAGES=ast.literal_eval(stages),
                     LEGACY=(*HISTORY, 'restart-v1', 'boundary-v1'),
                     BASE_ENVIRONMENT=runner.BASE_ENVIRONMENT, sha=sha,
                     audit_success=runner.audit_success)
    exec(compile(ast.Module(body=functions, type_ignores=[]),
                 str(stage.path / relative), 'exec'), namespace)
    helper = SimpleNamespace(**namespace)
    require(helper.configuration(stage.path.name) ==
            expected_configuration(stage.path.name),
            'frozen configuration differs from the accepted matrix')
    return helper, dict(sealed_runner_sha256=stage.manifest[relative],
                        pure_functions=[node.name for node in functions],
                        frozen_functions_only=True)


def verify_build_record(build, path):
    require(build['status'] == 'COMPLETED' and
            build['default_sha256'] == DEFAULT and
            build['default_replacement'] is False and
            build['candidate_sha256'] == build['candidate']['sha256'],
            'build/default identity contract failed')
    for identity in (build['candidate'], build['real_build_cache'],
                     *build['link_inputs'].values()):
        target = Path(identity['path'])
        require(target.stat().st_size == identity['bytes'] and
                sha(target) == identity['sha256'], 'build object changed')
    frozen = Path(build['source_snapshot'])
    for identity in build['source_and_plan_inputs'].values():
        target = frozen / Path(identity['path']).relative_to(ROOT)
        require(target.stat().st_size == identity['bytes'] and
                sha(target) == identity['sha256'], 'frozen build source changed')
    return dict(manifest=str(path), manifest_sha256=sha(path),
                candidate=build['candidate'], frozen_source_inputs_verified=True,
                current_default_unchanged=True)


def stage_identity(stage, build):
    state = stage.document('state.json')
    require(stage.document('build.json') == build and
            state['observer_sha'] == build['candidate_sha256'] and
            state['default_unchanged'] and state['default_sha'] == DEFAULT,
            'stage build/default identity differs')
    sources = stage.document('sources.json')
    for relative, digest in sources.items():
        require(stage.manifest.get('source/' + relative) == digest,
                'source receipt differs: ' + relative)
    for relative in ('tools/verify/device_reserve/reservation_audit.py',
                     'tools/verify/vmm_compare/trace_audit.py',
                     'tools/verify/vmm_compare/cache_start.py',
                     'tools/evalscope/offload_policy.py'):
        require(sha(ROOT / relative) == sources[relative],
                'imported helper differs from frozen source: ' + relative)
    for identity in build['source_and_plan_inputs'].values():
        relative = str(Path(identity['path']).relative_to(ROOT))
        require(stage.manifest.get('source/' + relative) == identity['sha256'],
                'compiled source differs from stage snapshot')


def audit_collectors(stage, state):
    kernel = stage.document('kernel/exit.json')
    resources = stage.document('resources/exit.json')
    require(kernel['status'] == resources['status'] == 'COMPLETED' and
            kernel['instance_removed'] and kernel['global_configuration_equal']
            and not any(kernel['loss_totals'].values()) and
            not resources['failures'] and not resources['truncated'],
            'collector cleanup/loss/observation contract failed')
    for key in ('kernel', 'resources'):
        require(state[key]['returncode'] == 0 and state[key]['result'] ==
                (kernel if key == 'kernel' else resources),
                'collector exit differs from supervisor receipt')
    return kernel, resources


def audit_nvml(stage, must_succeed=True):
    # Each line was produced by this exact frozen call site. Do not invent a
    # five-second bound on process spawning/teardown from the API deadline.
    source = stage.text('source/tools/evalscope/gpu_exclusive.py')
    tree = ast.parse(source)
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == 'nvml_pids')
    calls = [node for node in ast.walk(function) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute)
             and isinstance(node.func.value, ast.Name)
             and node.func.value.id == 'nvml_process_query'
             and node.func.attr == 'query']
    require(len(calls) == 1 and not calls[0].args and
            len(calls[0].keywords) == 1 and calls[0].keywords[0].arg == 'timeout'
            and isinstance(calls[0].keywords[0].value, ast.Constant)
            and calls[0].keywords[0].value.value == 5,
            'frozen NVML deadline is not five seconds')
    query_source = stage.text('source/tools/evalscope/nvml_process_query.py')
    query_tree = ast.parse(query_source)
    supervise = next(node for node in query_tree.body
                     if isinstance(node, ast.FunctionDef) and node.name == 'supervise')
    require(any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'communicate' and
                any(k.arg == 'timeout' and isinstance(k.value, ast.Name) and
                    k.value.id == 'timeout' for k in node.keywords)
                for node in ast.walk(supervise)), 'query deadline not supervised')
    queries = stage.lines('lease/nvml-queries.jsonl')
    require(queries, 'NVML queries missing')
    ids, api_maxima, failures = set(), {}, []
    for query in queries:
        success = query['status'] == 'OK'
        require((not must_succeed or success) and
                (not success or query['returncode'] == 0) and
                finite(query['seconds']) and query['seconds'] >= 0,
                'NVML query failed or has invalid timing')
        require(query['request_id'] not in ids, 'duplicate NVML query id')
        ids.add(query['request_id'])
        events = [decode(line) for line in query['stdout'].splitlines()]
        require(events and all(row['request_id'] == query['request_id']
                               for row in events), 'query worker identity differs')
        finals = [row for row in events if row['event'] == 'result']
        require(len(finals) <= 1, 'duplicate query worker result')
        final = finals[0] if finals else None
        if success:
            require(final and events[-1] == final and final['status'] == 'OK'
                    and final['pids'] == query['pids'] and
                    final.get('error') is None, 'query worker result differs')
        opened, seen, spans = {}, set(), {}
        previous = -1.0
        for row in events:
            require(finite(row['monotonic']) and row['monotonic'] >= previous,
                    'query time is not monotonic')
            previous = row['monotonic']
            if row['event'] == 'begin':
                require(row['api'] not in opened, 'query API began twice')
                opened[row['api']] = row['monotonic']
            elif row['event'] == 'end':
                start = opened.pop(row['api'])
                elapsed = row['monotonic'] - start
                api_maxima[row['api']] = max(api_maxima.get(row['api'], 0), elapsed)
                spans[row['api']] = elapsed
                seen.add(row['api'])
        if success:
            require(not opened and {'init', 'device_count', 'shutdown'} <= seen
                    and any(api.startswith('compute_v3[') for api in seen),
                    'query API lifecycle incomplete')
        else:
            failures.append(dict(status=query['status'],
                returncode=query['returncode'], seconds=query['seconds'],
                request_id=query['request_id'], worker_pid=query['worker_pid'],
                worker_result=final, completed_api_spans_seconds=spans,
                incomplete_apis=opened,
                outer_supervisor_status_takes_precedence=True))
    return dict(queries=len(queries), all_ok=not failures, failures=failures,
                successful_queries=len(queries) - len(failures),
                configured_timeout_seconds=5,
                maximum_observed_seconds=max(row['seconds'] for row in queries),
                maximum_api_span_seconds=api_maxima,
                deadline_evidence='sealed nvml_pids query(timeout=5) and '
                'worker process.communicate(timeout=timeout)',
                elapsed_scope='worker-process lifetime including launch/teardown')


def cache_holes(observation, expected):
    require(observation['complete_file_set_observed'] and
            not observation['errors'], 'cache state unknown')
    rows = [row for row in observation['files']
            if Path(row['path']).suffix in ('.safetensors', '.bin')]
    observed = {row['path']: row for row in rows}
    require(len(rows) == len(observed) and set(observed) == set(expected)
            and all(0 <= row['resident_bytes'] <= expected[path]
                    for path, row in observed.items()),
            'cache file set widened or has extra pages')
    missing = [path for path, row in observed.items()
               if row['resident_bytes'] < expected[path]]
    read_bytes = sum(observed[path]['size_bytes'] for path in missing)
    require(len(missing) <= 2 and read_bytes <= 1 << 30,
            'cache hole exceeds two files / 1GiB')
    return missing, read_bytes


def audit_cache(stage):
    expected = stage.document('run/cache-expected.json')
    require(sum(expected.values()) == runner.HOT_BYTES,
            'complete hot payload byte count differs')
    require(expected == runner.verified_cache_reference(),
            'complete hot vector differs from accepted prototype')
    for name in ('cache-ready.json', 'cache-launch.json'):
        observation = stage.document('run/' + name)
        page = observation['page_size_bytes']
        require(type(page) is int and page > 0 and not page & (page - 1),
                'invalid cache page-size identity')
    launch = stage.document('run/cache-launch.json')
    runner.check_vector(launch, expected)
    vector = {row['path']: row['resident_bytes'] for row in launch['files']}
    require(len(vector) == len(launch['files']), 'duplicate observed model file')
    gate = stage.document('run/cache-gate.json')
    require(gate['payload_bytes'] == runner.HOT_BYTES and
            type(gate['file_vector_exact']) is bool,
            'cache preparation target differs')
    ready = stage.document('run/cache-ready.json')
    if gate['file_vector_exact']:
        runner.check_vector(ready, expected)
        deferred = []
    else:
        require(stage.path.name != 'plain-v1', 'plain preparation was not exact')
        deferred, _ = cache_holes(ready, expected)
        require(deferred and set(gate['deferred_to_final_gate']) == set(deferred)
                and len(gate['deferred_to_final_gate']) == len(deferred) and
                gate['early_gate_error'].startswith('cache vector differs: '),
                'early gate was deferred without its bounded cache-hole cause')
    repair = None
    if stage.path.name != 'plain-v1':
        repair = stage.document('run/cache-launch-repair.json')
        before = stage.document('run/cache-launch-before.json')
        missing, repair_bytes = cache_holes(before, expected)
        require(set(repair['files']) == set(missing) and
                len(repair['files']) == len(missing) <= 2 and
                repair['bytes'] == repair_bytes <= 1 << 30 and
                repair['max_passes'] == 1 and repair['max_files'] == 2 and
                repair['max_bytes'] == 1 << 30,
                'final cache repair exceeded its bounded contract')
        require(before['end_t'] <= launch['start_t'],
                'cache repair observations out of order')
    return dict(payload_bytes=sum(expected.values()),
                payload_files=len(expected), observed_files=len(vector),
                page_size_bytes=launch['page_size_bytes'],
                payload_vector=expected, observed_vector=vector,
                early_gate_exact=gate['file_vector_exact'],
                early_gate_deferred_files=deferred,
                final_gate_repair=repair,
                exact_full_hot_gate=True,
                physical_fragmentation_equal=False)


def audit_scope(stage, accepted=True):
    prefix = 'run/normal/http/isolation/'
    spec = stage.document(prefix + 'spec.json')
    launch = stage.document(prefix + 'launch.json')
    identity = stage.document(prefix + 'identity.json')
    cleanup = stage.document(prefix + 'cleanup.json')
    payload_exit = stage.document(prefix + 'payload-exit.json')
    require(launch['host_cache_max_bytes'] == runner.HOST_LIMIT and
            launch['swap_max_bytes'] == 0, 'scope memory contract differs')
    require(identity['kind'] == 'scope' and identity['token'] == spec['token'] and
            identity['properties']['Description'] == spec['token'] and
            identity['properties']['MemoryMax'] == str(runner.HOST_LIMIT) and
            identity['properties']['MemorySwapMax'] == '0',
            'scope ownership/capacity differs')
    require(payload_exit['token'] == spec['token'] and
            (not accepted or payload_exit['returncode'] == 0) and
            cleanup['unit_removed'] and
            cleanup['properties_after']['LoadState'] == 'not-found',
            'scope payload/cleanup failed')
    state = stage.document('state.json')
    require(state['scope_final']['after']['LoadState'] == 'not-found',
            'supervisor did not confirm scope removal')
    cgroup = '/sys/fs/cgroup' + identity['properties']['ControlGroup']
    samples, maximum, event_maxima, swap = 0, 0, {}, 0
    for observation in stage.lines('resources/cgroup.jsonl'):
        require(not observation['failures'], 'cgroup collection failed')
        for row in observation['groups']:
            if row['path'] != cgroup or row['status'] != 'observed':
                continue
            samples += 1
            files = row['files']
            require(int(files['memory.max']['value']) == runner.HOST_LIMIT,
                    'sampled cgroup memory.max differs')
            maximum = max(maximum, int(files['memory.current']['value']))
            swap = max(swap, int(files['memory.swap.current']['value']))
            for line in files['memory.events']['value'].splitlines():
                key, value = line.split()
                event_maxima[key] = max(event_maxima.get(key, 0), int(value))
    require(samples > 0 and swap == 0 and
            not any(event_maxima.get(key, 0) for key in ('oom', 'oom_kill')),
            'missing scope samples, swap or OOM observed')
    return dict(unit=launch['unit'], pid=identity['pid'], scope_removed=True,
                host_cache_max_bytes=runner.HOST_LIMIT, total_physical_ram_limit=None,
                sampled_memory_current_peak_bytes=maximum, sampled_swap_peak_bytes=swap,
                memory_event_maxima=event_maxima, observed_samples=samples)


def audit_http(stage, name, log, plan_path, helper):
    cfg = helper.configuration(name)
    out = 'run/normal/http/'
    command = stage.document(out + 'server-command.json')
    require(command['argv'] == helper.server_command(name, stage.path / 'run'),
            'server invocation differs from frozen matrix')
    expected_env = helper.environment(name, stage.path / 'run', {})
    require(command['effective_q4t_environment'] == expected_env,
            'server environment differs')
    if cfg['mtp']:
        require(not any(flag in command['argv'] for flag in
                        ('--mtp', '--no-mtp', '--mtp-k')),
                'default MTP was overridden on command line')
    capacity = helper.capacity_evidence(log, name)
    require(stage.document(out + 'capacity.json') == capacity,
            'capacity receipt differs from server log')
    plan_gate = helper.check_plan(log, name, plan_path)
    require(stage.document(out + 'plan-ready.json') == plan_gate,
            'plan-ready receipt differs')
    exit_info = stage.document(out + 'exit.json')
    require(exit_info['server'] == 0 and exit_info['failure'] is None and
            exit_info['cleanup_failure'] is None and
            exit_info['http_output_checks_passed'] and
            exit_info['completed'] == cfg['requests'] and
            not exit_info['performance_acceptance'], 'HTTP exit gate failed')
    rows = stage.document(out + 'results.json')
    require(len(rows) == cfg['requests'] and
            len({row['response_id'] for row in rows}) == len(rows),
            'HTTP count/identity differs')
    fixture = stage.document('run/fixtures/manifest.json')
    require(len(fixture) == 1 and fixture[0]['length'] == 1024 and
            fixture[0]['expected'] == '710003', '1K fixture differs')
    actual_prompt = decode(stage.text('run/fixtures/requests.jsonl').strip())['prompt']
    require(hashlib.sha256(actual_prompt.encode()).hexdigest() ==
            fixture[0]['prompt_sha256'], 'prompt fixture hash differs')
    summaries = []
    for i, row in enumerate(rows):
        case = out + ('quality/' if i == 0 else 'quality-reuse/')
        response = stage.document(case + 'responses.json')
        require(len(response) == 1 and all(row[key] == value
                for key, value in response[0].items()), 'HTTP response summary differs')
        require(stage.document(case + 'client-exit.json')['returncode'] == 0,
                'evalscope client failed')
        require(row['success'] and row['exact_match'] and row['text'] == '710003'
                and row['actual_input'] == 1024 and row['actual_output'] == 7
                and row['requested_max_tokens'] == 32 and row['request_stream']
                and row['finish'] == ['stop'] and row['response_id_valid']
                and row['prompt_sha256'] == fixture[0]['prompt_sha256']
                and row['timing']['within_client_boundaries'],
                'exact 1K HTTP response failed')
        summaries.append({key: row[key] for key in
                          ('request_index', 'actual_input', 'actual_output', 'text',
                           'ttft', 'latency', 'response_id', 'prompt_sha256')})
    retained = stage.document(out + 'request-reservation-retention.json')
    reserve = runner.records(log, '[reserve-trace] ')
    handoffs = [row for row in reserve if row['event'] == 'handoff']
    expected = dict(plan_sha256=sha(plan_path), handed_live=len(handoffs),
                    reserved_pointers=[row['ptr'] for row in handoffs])
    require(len(retained) == len(rows) and all(row == expected for row in retained),
            'reserved storage did not persist across requests')
    events = stage.lines(out + 'events.jsonl')
    shutdown = one_event(events, 'server_before_shutdown')['monotonic_seconds']
    frees = [row for row in reserve if row['event'] == 'free']
    require(frees and min(row['mono'] for row in frees) >= shutdown,
            'reserved startup storage freed before shutdown')
    require(len([row for row in events if row['event'] == 'server_started']) == 1
            and len([row for row in events if row['event'] == 'client_before']) ==
            len(rows), 'model/request budget differs')
    return dict(effective=capacity, default_mtp_without_flags=cfg['mtp'],
                mtp_k=cfg['mtp_k'] if cfg['mtp'] else None,
                requests=summaries, same_instance_requests=len(rows),
                reserved_storage_unchanged_across_requests=True)


def timing(stage, log):
    events = stage.lines('run/normal/http/events.jsonl')
    started = one_event(events, 'server_started')['monotonic_seconds']
    ready = one_event(events, 'server_ready')['monotonic_seconds']
    before = one_event(events, 'server_before_shutdown')['monotonic_seconds']
    after = one_event(events, 'server_after_shutdown')['monotonic_seconds']
    require(started <= ready <= before <= after, 'model lifecycle timestamps differ')
    reserve = runner.records(log, '[reserve-trace] ')
    planned = one_event(reserve, 'plan')['mono']
    prepared = one_event(reserve, 'prepared')['mono']
    complete = one_event(reserve, 'startup_complete')['mono']
    require(planned <= prepared <= complete <= ready,
            'reservation/startup time ordering differs')
    opened, durations = {}, []
    for row in reserve:
        if row['event'] == 'alloc':
            if row['phase'] == 'begin':
                opened[row['index']] = row['mono']
            else:
                durations.append(row['mono'] - opened.pop(row['index']))
    require(durations and not opened and min(durations) >= 0,
            'physical allocation timing incomplete')
    phases = {}
    for name, ms in re.findall(r'\[q4t\]\[startup\] (\w+) ([0-9]+) ms', log):
        require(name not in phases, 'duplicate startup phase: ' + name)
        phases[name] = int(ms)
    require('startup_resource_plan' in phases, 'resource-plan phase not recorded')
    return dict(server_started_to_ready_seconds=ready - started,
                shutdown_seconds=after - before,
                startup_phase_reported_ms=phases,
                metadata_plan_seconds=None, metadata_timing_status='UNKNOWN',
                metadata_timing_reason='measured binary records a rounded combined '
                'metadata-plus-reservation phase, not separate metadata time',
                combined_plan_phase_ms=phases['startup_resource_plan'],
                reserve_plan_to_prepared_seconds=prepared - planned,
                reserve_prepared_to_startup_complete_seconds=complete - prepared,
                physical_allocation_seconds_sum=sum(durations),
                maximum_physical_allocation_seconds=max(durations),
                performance_acceptance=False)


def audit_stage(name, build):
    stage = SealedStage(BASE / name)
    stage_identity(stage, build)
    helper, helper_identity = frozen_helpers(stage, build)
    state = stage.document('state.json')
    require(state['status'] == 'RUNTIME_COMPLETED' and state['default_unchanged']
            and state['guard_returncode'] == 0 and state['model_load_budget'] == 1
            and state['http_budget'] == expected_configuration(name)['requests']
            and state['configuration'] == expected_configuration(name),
            'stage did not complete its bounded runtime contract: ' + name)
    require(stage.document('build.json') == build and
            state['observer_sha'] == build['candidate_sha256'],
            'stage build identity differs')
    require(stage.text('run/normal/http/binary.sha256').strip() ==
            build['candidate_sha256'], 'launched binary differs')
    require(sha(stage.file('run/normal/http/CMakeCache.txt')) ==
            build['real_build_cache']['sha256'], 'launched build cache differs')
    sources = stage.document('sources.json')
    for relative, digest in sources.items():
        require(stage.manifest.get('source/' + relative) == digest,
                'source receipt differs: ' + relative)
    # The reused post-hoc helper identities must match the sealed execution.
    for relative in ('tools/verify/device_reserve/reservation_audit.py',
                     'tools/verify/vmm_compare/trace_audit.py',
                     'tools/verify/vmm_compare/cache_start.py',
                     'tools/evalscope/offload_policy.py'):
        require(sha(ROOT / relative) == sources[relative],
                'offline helper differs from sealed source: ' + relative)
    for identity in build['source_and_plan_inputs'].values():
        relative = str(Path(identity['path']).relative_to(ROOT))
        require(stage.manifest.get('source/' + relative) == identity['sha256'],
                'compiled source differs from stage snapshot')
    guard = stage.document('lease/exit.json')
    require(guard['status'] == 'COMPLETED' and guard['child_returncode'] == 0
            and guard['compute_backend'] == 'nvml' and guard['external_lease']
            and 'cleanup_error' not in guard and state['guard'] == guard,
            'GPU guard failed')
    require(not Path(guard['lock_path'] + '.recovery-required').exists(),
            'unfinished GPU recovery remains')
    kernel, _ = audit_collectors(stage, state)
    require(stage.document('driver-identity/driver_identity.json')
            ['attribution_valid'], 'driver attribution failed')
    owned = stage.document('run/normal-process.json')
    require(owned['returncode'] == 0 and owned['runner_reaped'] and
            owned['cleanup_complete'] and not owned['failure'] and
            not owned['unexpected_live_descendants_after_runner_exit'],
            'HTTP runner descendants were not cleaned')
    plan_path = stage.file('run/startup-plan.json')
    plan = read(plan_path)
    log_path = stage.file('run/normal/http/server.log')
    log = log_path.read_text()
    reservation = helper.reservation_evidence(log, plan_path)
    require(stage.document('run/normal/http/reservation-audit.json') == reservation,
            'reservation receipt differs from recomputed ledger')
    logical = audit_logical_trace(log_path)
    require(not logical['errors'] and not logical['failed_calls'] and
            not logical['incomplete_calls'], 'logical allocation lifecycle failed')
    report = dict(name=name, status='PASS_BOUNDED_RELIABILITY',
                  candidate_sha256=build['candidate_sha256'], model_starts=1,
                  http_requests=expected_configuration(name)['requests'],
                  sealed_files=len(stage.manifest),
                  sealed_manifest_sha256=sha(stage.manifest_path),
                  offline_helper_identity=helper_identity,
                  configuration=expected_configuration(name),
                  plan_sha256=sha(plan_path), plan_count=plan['count'],
                  planned_device_bytes=plan['device_bytes'],
                  plan_effective=plan['effective'],
                  reservation=reservation, nvml=audit_nvml(stage),
                  scope=audit_scope(stage), cache=audit_cache(stage),
                  http=audit_http(stage, name, log, plan_path, helper),
                  timing=timing(stage, log),
                  logical_trace={k: v for k, v in logical.items()
                                 if k not in ('normalized', 'remaining')},
                  remaining_nonreserved_allocations=logical['remaining'],
                  trace_loss=kernel['loss_totals'], trace_instance_removed=True,
                  default_unchanged=True, performance_acceptance=False)
    if 'physical_order' in plan:
        report['physical_order'] = helper.physical_order_evidence(log, plan)
    report['direct_inputs_sha256'] = {
        name: stage.manifest[name] for name in sorted(stage.used)}
    return report, plan


def audit_preparation_failure(name, build):
    stage = SealedStage(BASE / name)
    stage_identity(stage, build)
    state = stage.document('state.json')
    guard = stage.document('lease/exit.json')
    worker = stage.document('run/state.json')
    require(state['status'] == 'RUNTIME_NOT_ACCEPTED' and
            state['default_unchanged'] and state['observer_sha'] ==
            build['candidate_sha256'] and stage.document('build.json') == build,
            'failed preparation build/status differs')
    require(guard['status'] == 'COMMAND_FAILED' and
            guard['child_returncode'] == state['guard_returncode'] == 1 and
            guard['compute_backend'] == 'nvml' and guard['external_lease'] and
            'cleanup_error' not in guard and state['guard'] == guard,
            'preparation was not a clean command failure')
    require(state['scope'] == state['scope_final'] == {'scope_created': False},
            'failed preparation created a model scope')
    error_matches = (worker['error'].startswith("RuntimeError('cache vector differs:")
                     if name in PREPARATION_FAILURES else worker['error'] ==
                     "RuntimeError('final cache repair exceeds two files / 1GiB')")
    require(worker['status'] == 'FAILED_OR_INTERRUPTED' and error_matches,
            'preparation failure reason differs')
    expected_events = (['prepare_begin', 'prepare_end'] if name == 'mtp-v1'
                       else ['prepare_begin'])
    require([row['event'] for row in stage.lines('run/milestones.jsonl')] ==
            expected_events, 'preparation launched a runner')
    forbidden = ('run/normal/', 'run/normal.log', 'run/normal-process.json',
                 'run/command.json', 'run/startup-plan.json',
                 'run/cache-launch.json')
    require(not any(name.startswith(prefix) for name in stage.manifest
                    for prefix in forbidden), 'failed preparation has run output')
    # Absence of files alone is insufficient: confirm the sealed control flow
    # rejected the cache gate before creating/launching the HTTP command.
    source = stage.text('source/tools/verify/startup_plan/run.py')
    tree = ast.parse(source)
    worker_node = next(node for node in tree.body
                       if isinstance(node, ast.FunctionDef) and node.name == 'worker')
    calls = [node for node in ast.walk(worker_node) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name)]
    blocked_call = ('check_vector' if name == 'mtp-v1' else
                    'prepare' if name == 'mtp-v2' else 'prepare_cache')
    checks = [node for node in calls if node.func.id == blocked_call]
    launches = [node for node in calls if node.func.id == 'run_owned_runner']
    require(len(checks) == len(launches) == 1 and
            checks[0].lineno < launches[0].lineno,
            'failed preparation source cannot establish zero model launches')
    for row in stage.lines('resources/resources.jsonl'):
        require(not row['failures'] and not any(target['kind'] == 'model'
                for target in row['targets']), 'model observed in failed preparation')
    expected = stage.document('run/cache-expected.json')
    require(expected == runner.verified_cache_reference(),
            'failed preparation used another cache target')
    ready = stage.document('run/cache-ready.json')
    if name == 'mtp-v1':
        runner.check_vector(ready, expected)
        gate = stage.document('run/cache-gate.json')
        require(gate['file_vector_exact'] and
                gate['payload_bytes'] == runner.HOT_BYTES,
                'first preparation did not complete the early exact gate')
        before = stage.document('run/cache-before-http-worker.json')
    else:
        require('run/cache-gate.json' not in stage.manifest and
                'run/cache-before-http-worker.json' not in stage.manifest,
                'second preparation unexpectedly passed its early gate')
        before = ready
    require(before['complete_file_set_observed'] and not before['errors'],
            'failed preparation cache observation incomplete')
    actual = {row['path']: row['resident_bytes'] for row in before['files']
              if Path(row['path']).suffix in ('.safetensors', '.bin')}
    require(set(actual) == set(expected), 'failed preparation file set differs')
    missing = {path: expected[path] - actual[path] for path in expected
               if expected[path] != actual[path]}
    missing_file_bytes = sum(row['size_bytes'] for row in before['files']
                             if row['path'] in missing)
    if name in PREPARATION_FAILURES:
        require(len(missing) == 1 and
                sum(missing.values()) == PREPARATION_FAILURES[name] and
                Path(next(iter(missing))).name ==
                'layer-00000-experts-0000-0127.safetensors',
                'failed preparation differs from the authorized retry cause')
    else:
        require(name == 'mtp-order-v1' and missing and
                all(value > 0 for value in missing.values()) and
                (len(missing) > 2 or missing_file_bytes > 1 << 30),
                'ordered preparation did not exceed its frozen repair bound')
    kernel, _ = audit_collectors(stage, state)
    nvml = audit_nvml(stage)
    return dict(name=name, status='PREPARATION_FAILED_RETAINED',
                candidate_sha256=build['candidate_sha256'], model_starts=0,
                http_requests=0, http_worker_launched=False,
                failure=worker['error'], missing_cache_bytes=missing,
                missing_total_bytes=sum(missing.values()),
                missing_files=len(missing), repair_file_bytes=missing_file_bytes,
                full_hot_preparation_passed=name == 'mtp-v1',
                failure_gate='parent_post_prepare' if name == 'mtp-v1' else
                'prepare_internal_cache_ready' if name == 'mtp-v2' else
                'bounded_deferral_rejected', scope_created=False,
                nvml=nvml, trace_loss=kernel['loss_totals'], cleanup_complete=True,
                sealed_files=len(stage.manifest),
                sealed_manifest_sha256=sha(stage.manifest_path),
                direct_inputs_sha256={name: stage.manifest[name]
                                      for name in sorted(stage.used)})


def audit_environment_abort(build):
    stage = SealedStage(BASE / 'mtp-v3')
    stage_identity(stage, build)
    helper, helper_identity = frozen_helpers(stage, build)
    state = stage.document('state.json')
    guard = stage.document('lease/exit.json')
    require(state['status'] == 'RUNTIME_NOT_ACCEPTED' and
            state['guard_returncode'] == 75 and guard['status'] == 'ENV_ABORT'
            and guard['compute_backend'] == 'nvml' and guard['external_lease']
            and 'NVML process query: TIMEOUT' in guard['reason'] and
            'cleanup_error' not in guard and state['guard'] == guard,
            'historical environment abort differs')
    owned = stage.document('run/normal-process.json')
    require(owned['runner_reaped'] and owned['cleanup_complete'] and
            not owned['unexpected_live_descendants_after_runner_exit'] and
            not owned['after_cleanup']['live_pids'] and
            not owned['after_cleanup']['zombie_pids'], 'aborted runner not reaped')
    exit_info = stage.document('run/normal/http/exit.json')
    require(exit_info['completed'] == 0 and exit_info['server'] == -15 and
            exit_info['cleanup_failure'] is None and
            not exit_info['http_output_checks_passed'], 'abort HTTP exit differs')
    events = stage.lines('run/normal/http/events.jsonl')
    starts = [row for row in events if row['event'] == 'server_started']
    require(len(starts) == 1 and not any(row['event'] in
            ('server_ready', 'client_before', 'client_after') for row in events),
            'aborted startup unexpectedly became ready or ran HTTP')
    command = stage.document('run/normal/http/server-command.json')
    require(command['argv'] == helper.server_command('mtp-v3', stage.path / 'run')
            and not any(flag in command['argv']
                        for flag in ('--mtp', '--no-mtp', '--mtp-k')),
            'aborted startup was not the unchanged default-MTP configuration')
    plan_path = stage.file('run/startup-plan.json')
    plan = read(plan_path)
    log = stage.text('run/normal/http/server.log')
    require(plan['schema'] == 1 and
            plan['derivation'] == 'configuration_tensor_metadata' and
            plan['count'] == len(plan['allocations']) == 1462 and
            plan['device_bytes'] == sum(row['bytes'] for row in plan['allocations']),
            'aborted metadata plan differs')
    cfg = expected_configuration('mtp-v3')
    require(all(plan['effective'][key] == value for key, value in cfg.items()
                if key != 'requests') and
            plan['effective']['initial_scratch_capacity'] == 4,
            'aborted metadata effective configuration differs')
    configured = one_event(helper.records(log, '[startup-plan] '), 'configured')
    require(configured['plan_sha256'] == sha(plan_path) and
            configured['effective'] == plan['effective'] and
            configured['count'] == plan['count'] and
            configured['device_bytes'] == plan['device_bytes'],
            'aborted configured summary differs')
    rows = helper.records(log, '[reserve-trace] ')
    require([row['seq'] for row in rows] == list(range(len(rows))) and
            all(row['event'] in ('plan', 'alloc') for row in rows),
            'aborted reservation progressed beyond physical preparation')
    event = one_event(rows, 'plan')
    require(event['plan_sha256'] == sha(plan_path) and
            event['count'] == plan['count'] and
            event['total_bytes'] == plan['device_bytes'], 'aborted plan hash differs')
    begins = [row for row in rows if row['event'] == 'alloc'
              and row['phase'] == 'begin']
    ends = [row for row in rows if row['event'] == 'alloc' and row['phase'] == 'end']
    require([row['index'] for row in begins] == list(range(1417)) and
            [row['index'] for row in ends] == list(range(1416)),
            'aborted physical allocation prefix differs')
    durations = []
    for begin, end in zip(begins, ends):
        require(begin['bytes'] == end['bytes'] ==
                plan['allocations'][begin['index']]['bytes'] and
                end['rc'] == 0 and end['ptr'] and begin['mono'] <= end['mono'],
                'aborted allocation completion invalid')
        durations.append(end['mono'] - begin['mono'])
    require(not helper.records(log, '[memory-trace] ') and
            '[q4t] serving on port ' not in log,
            'aborted startup performed logical allocation or became ready')
    nvml = audit_nvml(stage, must_succeed=False)
    require(len(nvml['failures']) == 1 and
            nvml['failures'][0]['status'] == 'TIMEOUT',
            'historical NVML timeout count differs')
    kernel, _ = audit_collectors(stage, state)
    report = dict(name='mtp-v3', status='ENV_ABORT_RETAINED',
        candidate_sha256=build['candidate_sha256'], model_starts=1, http_requests=0,
        runtime_accepted=False, candidate_quality_no_go=False,
        reason=guard['reason'], configuration=cfg, plan_sha256=sha(plan_path),
        plan_count=plan['count'], planned_device_bytes=plan['device_bytes'],
        nvml=nvml, cache=audit_cache(stage), scope=audit_scope(stage, accepted=False),
        physical_reservation=dict(started=len(begins), completed=len(ends),
            successful_bytes=sum(row['bytes'] for row in ends),
            incomplete=begins[len(ends):], handed_off=0, complete=False,
            maximum_completed_allocation_seconds=max(durations),
            application_release_status='UNKNOWN_PROCESS_ABORTED',
            physical_driver_reclamation='NOT_PROVEN_BY_SCOPE_CLEANUP'),
        metadata_plan_seconds=None, metadata_timing_status='UNKNOWN',
        offline_helper_identity=helper_identity, cleanup_complete=True,
        trace_loss=kernel['loss_totals'], sealed_files=len(stage.manifest),
        sealed_manifest_sha256=sha(stage.manifest_path), performance_acceptance=False)
    report['direct_inputs_sha256'] = {
        name: stage.manifest[name] for name in sorted(stage.used)}
    return report, plan


def audit_host_contracts(old_build, new_build):
    names = ('host-core-order-v1', 'order-owner-host-v2',
             'order-wrapper-host-v1', 'host-plan-v1')
    folders = {name: SealedStage(BASE / name) for name in names}
    results = {name: folder.document('result.json')
               for name, folder in folders.items()}
    require(all(row['status'] == 'PASS' and row['gpu_execution'] is False
                for row in results.values()), 'host contract evidence failed')
    core = results['host-core-order-v1']
    require(core['count'] == 35 and core['candidate_sha256'] ==
            new_build['candidate_sha256'] and core['gpu_api_calls'] ==
            core['payload_read_calls'] == core['cache_advice_calls'] == 0 and
            core['build_manifest']['sha256'] == sha(BASE / 'order-candidate/build.json'),
            'new core contracts do not match the candidate')
    for path, record in core['archives'].items():
        require(record['sha256'] == new_build['link_inputs'][path]['sha256'],
                'core test linked another candidate archive')
    owner = results['order-owner-host-v2']
    wrapper = results['order-wrapper-host-v1']
    require(owner['result']['host_contracts'] == 15 and
            owner['build_exit'] == owner['run_exit'] == 0 and wrapper['count'] == 9,
            'owner/wrapper contract counts differ')
    wrapper_inputs = folders['order-wrapper-host-v1'].document('inputs.json')
    for path, digest in {**owner['inputs'], **wrapper_inputs}.items():
        require(sha(Path(path)) == digest, 'host contract source changed: ' + path)
        if path in new_build['source_and_plan_inputs']:
            require(new_build['source_and_plan_inputs'][path]['sha256'] == digest,
                    'host contract does not match compiled source')
    plan = results['host-plan-v1']
    plan_inputs = folders['host-plan-v1'].document('inputs.json')
    require(plan['completed_cases'] == len(plan['cases']) == 27 and
            all(row['status'] == 'PASS' for row in plan['cases'].values()) and
            plan_inputs['candidate_sha256'] == old_build['candidate_sha256'] and
            plan_inputs['build_manifest_sha256'] == sha(BASE / 'build.json'),
            'historical configuration evidence identity differs')
    archives = [next(record for path, record in build['link_inputs'].items()
                     if Path(path).name == 'libq4t_startup_plan.a')
                for build in (old_build, new_build)]
    require(archives[0]['sha256'] == archives[1]['sha256'],
            'planner archive changed; historical shape tests cannot be reused')
    old_sources, new_sources = (build['source_and_plan_inputs']
                               for build in (old_build, new_build))
    require(old_sources.keys() == new_sources.keys(), 'compiled source set changed')
    changed = {str(Path(path).relative_to(ROOT)) for path in old_sources
               if old_sources[path]['sha256'] != new_sources[path]['sha256']}
    require(changed == {'tools/verify/device_reserve/owner.h',
                       'tools/verify/startup_plan/reservation.cpp'},
            'changes exceed the physical-order fix; shape reuse not established')
    runner_path = BASE / 'runner-order-host-v1/result.json'
    run_result = read(runner_path)
    require(run_result['status'] == 'PASS' and run_result['tests'] == 24 and
            run_result['gpu_execution'] is False and
            run_result['read_only_build_preflight_candidate_sha256'] ==
            new_build['candidate_sha256'], 'runner CPU evidence differs')
    for path, digest in run_result['source_sha256'].items():
        require(sha(ROOT / path) == digest and
                sha(runner_path.parent / Path(path).name) == digest,
                'runner tested source differs')
    return dict(new_candidate_contracts=dict(runner=24, core=35, owner=15, wrapper=9),
                reused_configuration_contracts=27, total=110,
                planner_archive_sha256=archives[0]['sha256'],
                unchanged_configuration_formulas=True,
                source_changes=sorted(changed), gpu_configuration_accepted=False,
                receipts={name: dict(files=len(folder.manifest),
                    sealed_manifest_sha256=sha(folder.manifest_path),
                    result_sha256=sha(folder.path / 'result.json'))
                    for name, folder in folders.items()},
                runner_result_sha256=sha(runner_path))


def compact_stage(row):
    result = {key: value for key, value in row.items() if key not in
              ('direct_inputs_sha256', 'logical_trace',
               'remaining_nonreserved_allocations')}
    if 'cache' in result:
        result['cache'] = {key: value for key, value in result['cache'].items()
                           if key not in ('payload_vector', 'observed_vector')}
    return result


def main():
    require(__debug__, 'reused ledger assertions require Python without -O')
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--partial', action='store_true',
                      help='write report only, never create a final seal')
    mode.add_argument('--finalize', action='store_true',
                      help='seal a completed research decision, including nonacceptance')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--full-output', type=Path, default=BASE / 'audit-full.json')
    parser.add_argument('--seal-output', type=Path, default=BASE / 'final-evidence.json')
    args = parser.parse_args()
    output = args.output.resolve()
    require(output.is_relative_to(ROOT) and not output.exists(),
            'output must be new and inside the repository')
    all_names = (*HISTORY, *STAGES)
    require(not any(output.is_relative_to(BASE / name) for name in all_names),
            'cannot write into sealed stages')
    require(sha(ROOT / 'build/q4t') == DEFAULT, 'default program identity changed')
    lock = read(ROOT / '.q4t-work/gpu-exclusive.lock')
    require(lock['state'] == 'released' and not
            (ROOT / '.q4t-work/gpu-exclusive.lock.recovery-required').exists(),
            'GPU lease/recovery is not closed')
    old_path, new_path = BASE / 'build.json', BASE / 'order-candidate/build.json'
    old_build, new_build = read(old_path), read(new_path)
    builds = dict(historical=verify_build_record(old_build, old_path),
                  ordered=verify_build_record(new_build, new_path))
    require(old_build['candidate_sha256'] != new_build['candidate_sha256'],
            'ordered candidate is not independent')
    plain, plain_plan = audit_stage('plain-v1', old_build)
    aborted, aborted_plan = audit_environment_abort(old_build)
    preparations = [audit_preparation_failure(name, old_build)
                    for name in PREPARATION_FAILURES]
    ordered, ordered_plans, stopped = [], {}, False
    for name in STAGES:
        path = BASE / name
        if not path.exists():
            ordered.append(dict(name=name, status='NOT_RUN', model_starts=0,
                                http_requests=0, runtime_accepted=False))
            stopped = True
            continue
        require(not stopped, 'a later stage ran after a missing or failed stage')
        status = read(path / 'state.json')['status']
        if status == 'RUNTIME_COMPLETED':
            result, plan = audit_stage(name, new_build)
            ordered.append(result)
            ordered_plans[name] = plan
        else:
            result = audit_preparation_failure(name, new_build)
            ordered.append(result)
            stopped = True
        receipt = read(path / 'attempt-budget.json')
        prior = [plain, *preparations, aborted, *ordered[:-1]]
        require(receipt['prior_model_starts'] == sum(
                    row['model_starts'] for row in prior) and
                receipt['prior_http_requests'] == sum(
                    row['http_requests'] for row in prior) and
                receipt['model_start_limit'] == receipt['http_limit'] == 5,
                'stage attempt budget does not include all prior failures')
        for prior_row in receipt['prior']:
            require(prior_row['manifest_sha256'] == sha(
                BASE / prior_row['name'] / 'sealed-files.json'),
                'attempt budget references another sealed history')
    results = [plain, *preparations, aborted, *ordered]
    observed = [row for row in results if row['status'] != 'NOT_RUN']
    successes = [row for row in ordered if row['status'] == 'PASS_BOUNDED_RELIABILITY']
    accepted = len(successes) == len(STAGES)
    if 'mtp-order-v1' in ordered_plans:
        logical = {key: value for key, value in ordered_plans['mtp-order-v1'].items()
                   if key != 'physical_order'}
        require(logical == aborted_plan,
                'physical-order change altered the logical startup plan')
    if 'restart-order-v1' in ordered_plans:
        require(ordered_plans['restart-order-v1'] == ordered_plans['mtp-order-v1'],
                'ordered MTP restart plan changed')
    starts = sum(row['model_starts'] for row in observed)
    requests = sum(row['http_requests'] for row in observed)
    require(starts <= 5 and requests <= 5, 'global bounded budget exceeded')
    launched = [row for row in observed if row['model_starts']]
    require(all(row['cache']['payload_vector'] == plain['cache']['payload_vector']
                for row in launched), 'launched complete hot vectors differ')
    host = audit_host_contracts(old_build, new_build)
    analysis = SealedStage(BASE / 'mtp-v3-analysis')
    analysis_receipt = dict(path=str(analysis.path), files=len(analysis.manifest),
        sealed_manifest_sha256=sha(analysis.manifest_path),
        reports_sha256={name: analysis.manifest[name] for name in
            ('analysis.json', 'analysis-reusable.json', 'summary.json',
             'prep-order-v1-v2.json', 'report.txt')},
        scope='sealed kernel/resource attribution; no new analysis execution')
    status = ('ENGINEERING_ACCEPTED_BOUNDED_ORDERED_STARTUP_RESEARCH' if accepted
              else 'INCOMPLETE_ENV_ABORT_AND_PREPARATION_FAILED')
    report = dict(schema=2, status=status, evidence_status='VERIFIED',
        runtime_acceptance='ACCEPTED_BOUNDED_RESEARCH' if accepted else 'NOT_ACCEPTED',
        candidate_quality_no_go=False, default_sha256=DEFAULT,
        default_replaced=False, performance_acceptance=False,
        builds=builds, candidate_sha256=new_build['candidate_sha256'],
        historical_candidate_sha256=old_build['candidate_sha256'],
        budget=dict(model_start_limit=5, http_limit=5,
            preparation_attempts=len(observed), model_starts=starts,
            http_requests=requests, preparation_only_failures=sum(
                row['status'] == 'PREPARATION_FAILED_RETAINED' for row in observed),
            ordered_candidate_model_starts=sum(row['model_starts'] for row in ordered),
            ordered_candidate_http_requests=sum(row['http_requests'] for row in ordered)),
        stages=results, host_contracts=host, mechanism_analysis=analysis_receipt,
        files_verified=sum(row['sealed_files'] for row in observed),
        nvml_queries=sum(row['nvml']['queries'] for row in observed),
        nvml_successful_queries=sum(row['nvml']['successful_queries'] for row in observed),
        nvml_failed_queries=sum(len(row['nvml']['failures']) for row in observed),
        nvml_configured_timeout_seconds=5,
        maximum_nvml_observed_seconds=max(
            row['nvml']['maximum_observed_seconds'] for row in observed),
        same_complete_hot_payload_vector_for_launched_models=True,
        metadata_plan_seconds=None, metadata_timing_status='UNKNOWN',
        configuration_totals={row['name']: dict(count=row['plan_count'],
            device_bytes=row['planned_device_bytes'],
            candidate_sha256=row['candidate_sha256']) for row in observed
            if 'plan_count' in row},
        limitations=[
            'Historical plain-mode success applies only to the historical binary.',
            'The ordered candidate has no GPU runtime evidence when cache preparation stops before launch.',
            'ENV_ABORT is retained as an environmental interruption, not a model quality NO_GO.',
            'Missing stages remain NOT_RUN; host shape contracts do not establish GPU acceptance.',
            'Metadata-only time is unknown; the measured combined phase includes reservation.',
            'Each attempt re-establishes the cache vector; no untouched-cache restart is claimed.',
            '64 GiB limits scope host/cache charges, not total physical RAM.',
            'Prewarm limit_gib=64 selects file bytes only; the preparation worker inherited a cgroup that was not captured, so empty observed groups do not prove absence of memcg pressure.',
            'Process/scope cleanup does not prove application frees or physical driver reclamation.',
            'GPU polling cannot exclude interference shorter than its sampling interval.',
            'No default replacement or performance acceptance.'
        ])
    if args.partial:
        write_new(output, report)
        return
    full, seal = args.full_output.resolve(), args.seal_output.resolve()
    snapshots = BASE / 'final-audit-tools'
    require(not output.is_relative_to(BASE) and full.is_relative_to(BASE) and
            seal.is_relative_to(BASE), 'final output placement differs')
    require(len({output, full, seal, snapshots}) == 4 and
            not any(path.exists() for path in (full, seal, snapshots)),
            'refusing to overwrite final evidence')
    require(not any(path.is_relative_to(BASE / name) for name in all_names
                    for path in (full, seal)), 'cannot write into sealed attempts')
    sources = [Path(__file__).resolve(), HERE / 'run.py',
               ROOT / 'tools/verify/device_reserve/reservation_audit.py',
               ROOT / 'tools/verify/vmm_compare/trace_audit.py']
    snapshots.mkdir()
    identities = {}
    for source in sources:
        shutil.copy2(source, snapshots / source.name)
        identities[str(source.relative_to(ROOT))] = sha(source)
    report['audit_sources_sha256'] = identities
    write_new(full, report)
    manifest = {str(path.relative_to(BASE)): sha(path) for path in BASE.rglob('*')
                if path.is_file() and path != seal}
    write_new(seal, manifest)
    for name, digest in manifest.items():
        require(sha(BASE / name) == digest, 'final evidence changed while sealing')
    compact = dict(report, stages=[compact_stage(row) for row in results],
        full_audit=dict(path=str(full), sha256=sha(full)),
        final_evidence=dict(path=str(seal), sha256=sha(seal), files=len(manifest)))
    write_new(output, compact)
    # Stay silent after sealing; redirected output must not mutate BASE.


if __name__ == '__main__':
    main()
