"""Read-only product startup acceptance; never run a model or change cache.

Each stage must pass before the next begins. Missing evidence is NOT_RUN or
INCOMPLETE, never a pass. Output is a new file outside sealed stage/build trees.
"""
import argparse
import ast
import difflib
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import re
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
BASE = ROOT / '.q4t-work/startup-delivery-20261021'
MODEL = Path.home() / 'models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream'
STAGES = ('default-cold-v1', 'default-inherited-v1', 'c256-hot-v1',
          'c256-inherited-v1', 'boundary-v2', 'off-v1')
REUSED_STAGES = STAGES[:4]
RETAINED_FAILURE = 'boundary-v1'
RETAINED_SEAL_SHA = 'd93e6e249fd6f8f370b69c43d401c015e756b0c64ac2c06fe480ab16e076c0cb'
RETAINED_AUDIT_SHA = 'ba445c7ac2506f16c0a3559f6687a398466394d6e0bee0a945beb78db63cc085'
ORIGINAL_RUN_SHA = '8f5f1d1f661f0815750d2ee5aae28f0598d727c53f46eedbd9bbbbe3477b7bbe'
DEPLOY_STAGES = ('deploy-mtp-v1', 'deploy-plain-v1')
HOT_BYTES = 67987791872
PROFILE = 'resources_nvml_production_summary'
OLD_CACHE = ROOT / '.q4t-work/startup-cache-20261021'
OLD_PLAN = ROOT / '.q4t-work/startup-plan-20261020'
INPUT_SOURCE = ROOT / '.q4t-work/device-reserve-20261020/reserve-v1'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def unique(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'duplicate JSON key: ' + key)
        result[key] = value
    return result


def decode(text):
    return json.loads(text, object_pairs_hook=unique,
                      parse_constant=lambda x: (_ for _ in ()).throw(
                          ValueError('nonfinite JSON: ' + x)))


def read(path):
    return decode(Path(path).read_text())


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def identity(path):
    path = Path(path).resolve(strict=True)
    return dict(path=str(path), sha256=sha(path), bytes=path.stat().st_size)


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def one(rows, event):
    selected = [row for row in rows if row['event'] == event]
    require(len(selected) == 1, 'expected one event: ' + event)
    return selected[0]


def verified_file(directory, relative):
    directory = Path(directory).resolve(strict=True)
    manifest = read(directory / 'sealed-files.json')
    require(relative in manifest, 'unsealed historical input: ' + relative)
    path = (directory / relative).resolve(strict=True)
    require(path.is_relative_to(directory), 'sealed path escapes stage')
    record = manifest[relative]
    digest = record['sha256'] if isinstance(record, dict) else record
    require(sha(path) == digest, 'historical input differs: ' + str(path))
    return path


class SealedStage:
    def __init__(self, path):
        self.path = Path(path).resolve(strict=True)
        self.manifest_path = self.path / 'sealed-files.json'
        self.manifest = read(self.manifest_path)
        require(self.manifest and isinstance(self.manifest, dict), 'empty seal')
        actual = {str(p.relative_to(self.path)) for p in self.path.rglob('*')
                  if p.is_file() and p != self.manifest_path}
        require(actual == set(self.manifest), 'sealed file set differs')
        for relative, record in list(self.manifest.items()):
            path = self.path / relative
            require(path.resolve(strict=True).is_relative_to(self.path),
                    'sealed path escapes stage')
            digest = record['sha256'] if isinstance(record, dict) else record
            require(isinstance(digest, str) and re.fullmatch('[0-9a-f]{64}', digest)
                    and sha(path) == digest, 'sealed file differs: ' + relative)
            if isinstance(record, dict):
                require(record['bytes'] == path.stat().st_size,
                        'sealed file size differs: ' + relative)
            self.manifest[relative] = digest
        self.used = set()

    def file(self, name):
        require(name in self.manifest, 'unsealed input: ' + name)
        self.used.add(name)
        return self.path / name

    def document(self, name):
        return read(self.file(name))

    def text(self, name):
        return self.file(name).read_text()

    def lines(self, name):
        return [decode(line) for line in self.text(name).splitlines() if line]


def configuration(name):
    require(name in (*STAGES, RETAINED_FAILURE), 'unknown stage')
    default = name.startswith('default-') or name == 'off-v1'
    boundary = name in ('boundary-v1', 'boundary-v2')
    return dict(real_default=default, max_seq=2 if boundary else 1,
                max_prefill=0 if default else 2048 if boundary else 8192,
                max_len=0 if default else 8192 if boundary else 262144,
                resident_slots=0 if default else 128 if boundary else 256,
                mtp=not boundary, startup_plan='off' if name == 'off-v1' else 'auto',
                l2_slots=None if default else 1 if boundary else 16,
                mirror_k=None if default else 0 if boundary else 8,
                load_threads=None if default else 1 if boundary else 16,
                memory_max=None if default else 64 << 30,
                cache='inherited' if 'inherited' in name else
                      'full_hot' if name == 'c256-hot-v1' else 'cold',
                requests=2 if name in ('default-cold-v1', 'c256-hot-v1',
                                       'boundary-v1', 'boundary-v2') else 1)


def pure_functions(path, names, namespace):
    tree = ast.parse(Path(path).read_text())
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef)
             and n.name in names]
    require({n.name for n in nodes} == set(names), 'pure helper set differs')
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), namespace)
    return SimpleNamespace(**namespace)


def cache_reference():
    return read(verified_file(INPUT_SOURCE, 'run/cache-expected.json'))


def cache_vector(observation, expected=None):
    require(observation['complete_file_set_observed'] and not observation['errors'],
            'cache observation incomplete')
    rows = observation['files']
    require(len({row['path'] for row in rows}) == len(rows), 'duplicate cache file')
    vector = {row['path']: row['resident_bytes'] for row in rows
              if Path(row['path']).suffix in ('.safetensors', '.bin')}
    reference = cache_reference()
    require(set(vector) == set(reference) and all(type(n) is int and n >= 0
                                                 for n in vector.values()),
            'payload cache file set or residency differs')
    if expected is not None:
        require(vector == expected, 'cache vector differs from full-hot fixture')
    return vector


def reused_helpers(stage):
    common = HERE.parent / 'startup_plan/audit.py'
    hot = HERE.parent / 'startup_cache/audit.py'
    for path in (common, hot):
        name = 'source/' + str(path.relative_to(ROOT))
        require(stage.manifest[name] == sha(path), 'reused audit source differs')
    shared = dict(ast=ast, Path=Path, json=json, re=re, hashlib=hashlib,
                  require=require, read=read, sha=sha, decode=decode,
                  finite=finite, one=one, HOT_BYTES=HOT_BYTES)
    common_helpers = pure_functions(common, ['audit_nvml'], dict(shared))
    shared['runner'] = SimpleNamespace(MODEL=MODEL,
        verified_cache_reference=cache_reference, check_vector=cache_vector)
    hot_helpers = pure_functions(hot, ['ordered', 'birth', 'helper_identity',
        'audit_locks', 'snapshot', 'audit_preparation', 'audit_owned'], shared)
    return common_helpers, hot_helpers


def audit_scope(stage, cfg):
    prefix = 'run/normal/http/isolation/'
    spec, launch, who, cleanup, exited = [stage.document(prefix + p + '.json')
        for p in ('spec', 'launch', 'identity', 'cleanup', 'payload-exit')]
    maximum = cfg['memory_max']
    require(launch['host_cache_max_bytes'] == maximum and launch['swap_max_bytes'] == 0,
            'scope launch limit differs')
    props = who['properties']
    require(who['kind'] == 'scope' and who['token'] == spec['token'] ==
            props['Description'] and props['MemoryMax'] ==
            ('infinity' if maximum is None else str(maximum)) and
            props['MemorySwapMax'] == '0', 'actual scope identity/limits differ')
    require(exited['token'] == spec['token'] and exited['returncode'] == 0 and
            cleanup['unit_removed'] and cleanup['properties_after']['LoadState'] ==
            'not-found' and stage.document('state.json')['scope_final']['after']
            ['LoadState'] == 'not-found', 'scope cleanup incomplete')
    group = '/sys/fs/cgroup' + props['ControlGroup']
    samples, peak, swap, events = 0, 0, 0, {}
    for observation in stage.lines('resources/cgroup.jsonl'):
        require(not observation['failures'], 'cgroup observation failed')
        for row in observation['groups']:
            if row['path'] != group or row['status'] != 'observed':
                continue
            samples += 1
            files = row['files']
            require(files['memory.max']['value'].strip() ==
                    ('max' if maximum is None else str(maximum)),
                    'sampled cgroup memory.max differs')
            peak = max(peak, int(files['memory.current']['value']))
            swap = max(swap, int(files['memory.swap.current']['value']))
            for line in files['memory.events']['value'].splitlines():
                key, value = line.split()
                events[key] = max(events.get(key, 0), int(value))
    require(samples and swap == 0 and not any(events.get(key, 0)
                for key in ('oom', 'oom_kill')), 'missing samples/swap/OOM')
    return dict(unit=launch['unit'], pid=who['pid'], scope_removed=True,
        host_cache_max_bytes=maximum, total_physical_ram_limit=None,
        sampled_memory_current_peak_bytes=peak, sampled_swap_peak_bytes=swap,
        memory_event_maxima=events, observed_samples=samples)


def audit_cache(stage, cfg, events, hot_helpers):
    gate = stage.document('run/cache-gate.json')
    require(gate['mode'] == cfg['cache'] and gate['repair_passes'] == 0 and
            gate['physical_fragmentation_equal'] is False, 'cache mode differs')
    launch = stage.document('run/cache-launch.json')
    vector = cache_vector(launch)
    clock = stage.document('run/cache-launch-time.json')
    before, started = one(events, 'server_before_start'), one(events, 'server_started')
    require(all(finite(clock[k]) for k in ('scan_begin_mono', 'scan_end_mono')) and
            clock['scan_begin_mono'] <= clock['scan_end_mono'] <=
            before['monotonic_seconds'] <= started['monotonic_seconds'] and
            launch['start_t'] <= launch['end_t'] <= before['unix_seconds'],
            'cache scan did not finish before model launch')
    if cfg['cache'] == 'full_hot':
        require(sum(vector.values()) == HOT_BYTES, 'full-hot byte total differs')
        locks = hot_helpers.audit_locks(stage, True)
        class GateView:
            def __getattr__(self, name):
                return getattr(stage, name)
            def document(self, name):
                value = stage.document(name)
                return {k: v for k, v in value.items() if k != 'mode'} \
                    if name == 'run/cache-gate.json' else value
        preparation = hot_helpers.audit_preparation(GateView(), locks, events)
        attempts = stage.document('run/cache-preparation.json')['attempts']
        require(len(attempts) == 1, 'full-hot preclear repeated')
        return dict(mode='full_hot', vector=vector, locks=locks,
                    preparation=preparation, preparation_attempts=1)
    require(not any(name.startswith('run/locked-warm/') for name in stage.manifest)
            and gate['temporary_locks_used'] is False, 'unexpected temporary lock')
    require(gate['payload_bytes'] == sum(vector.values()), 'cache byte sum differs')
    if cfg['cache'] == 'cold':
        preparation = stage.document('run/cache-preparation.json')
        attempts = preparation['attempts']
        revised = stage.path.name not in REUSED_STAGES
        limit = 2 if revised else 1
        require(not preparation['errors'] and 1 <= len(attempts) <= limit and
                sum(vector.values()) == 0 and gate['file_vector_exact'] is True
                and gate['preparation_passes'] == len(attempts),
                'cold preparation differs')
        before_observation = stage.document('run/cache-before.json')
        cache_vector(before_observation)
        all_paths = [row['path'] for row in before_observation['files']]
        require(len(launch['files']) == len(all_paths) and
                {row['path'] for row in launch['files']} == set(all_paths) and
                all(type(row['resident_bytes']) is int and row['resident_bytes'] >= 0
                    for observation in (before_observation, launch)
                    for row in observation['files']), 'cold complete file set differs')
        previous_remaining = all_paths
        for number, attempt in enumerate(attempts, 1):
            observation = attempt['observation']
            observed = cache_vector(observation)
            require(attempt['pass_number'] == number and
                    not attempt['advice_errors'] and
                    attempt['payload_resident_bytes'] == sum(observed.values()) and
                    len(observation['files']) == len(all_paths) and
                    {row['path'] for row in observation['files']} == set(all_paths)
                    and all(type(row['resident_bytes']) is int and
                            row['resident_bytes'] >= 0 for row in observation['files']),
                    'cold preparation observation differs')
            if revised:
                require(previous_remaining and
                        attempt['advised_files'] == previous_remaining,
                        'cold advice exceeded observed residual files')
            previous_remaining = [row['path'] for row in observation['files']
                if row['path'] in observed and row['resident_bytes'] > 0]
        if revised:
            require(gate['max_preparation_passes'] == 2 and
                    gate['residual_retry_files'] ==
                    (attempts[1]['advised_files'] if len(attempts) == 2 else []) and
                    (len(attempts) == 2 or not previous_remaining),
                    'cold residual retry receipt differs')
    else:
        require('run/cache-preparation.json' not in stage.manifest and
                gate['naturally_inherited'] is True and
                gate['file_vector_exact'] is False and gate['preparation_passes'] == 0,
                'inherited state was prepared or relabeled exact')
    return dict(mode=cfg['cache'], vector=vector, payload_bytes=sum(vector.values()),
                preparation_attempts=len(attempts) if cfg['cache'] == 'cold' else 0,
                physical_fragmentation_equal=False)


def frozen_runner(stage, build):
    path = stage.file('source/tools/verify/startup_delivery/run.py')
    policy = ast.parse(stage.text('source/tools/evalscope/offload_policy.py'))
    base_environment = next(ast.literal_eval(node.value) for node in policy.body
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and
            target.id == 'BASE_ENVIRONMENT' for target in node.targets))
    namespace = dict(json=json, Path=Path, re=re, ROOT=ROOT, BASE=BASE,
        BINARY=Path(build['candidate']['path']), MODEL=MODEL, STAGES=STAGES,
        DEPLOY_STAGES=DEPLOY_STAGES, BASE_ENVIRONMENT=base_environment,
        HOST_LIMIT=64 << 30)
    names = ['require', 'records', 'configuration', 'port_for', 'environment',
             'server_command', 'capacity_evidence', 'reservation_evidence']
    helper = pure_functions(path, names, namespace)
    require(helper.configuration(stage.path.name) == configuration(stage.path.name),
            'frozen matrix differs from audit specification')
    return helper


def plan_digest(plan):
    data = 'q4t-startup-plan-v1\nstable_descending_bytes\n'
    rows = plan['allocations']
    require(plan['count'] == len(rows) and len({r['name'] for r in rows}) == len(rows)
            and plan['device_bytes'] == sum(r['bytes'] for r in rows),
            'historical plan shape invalid')
    for row in rows:
        require(type(row['bytes']) is int and row['bytes'] > 0, 'invalid plan bytes')
        data += f'{len(row["name"].encode())}:{row["name"]}:{row["bytes"]}\n'
    return hashlib.sha256(data.encode()).hexdigest()


def audit_http(stage, build, cfg, helper):
    prefix = 'run/normal/http/'
    log = stage.text(prefix + 'server.log')
    command = stage.document(prefix + 'server-command.json')
    inputs = stage.document('run/input-identities.json')
    require(inputs['source'] == str(INPUT_SOURCE) and inputs['seal_sha256'] ==
            sha(INPUT_SOURCE / 'sealed-files.json') and inputs['inputs'],
            'fixture provenance differs')
    for relative, digest in inputs['inputs'].items():
        require(sha(verified_file(INPUT_SOURCE, relative)) == digest and
                stage.manifest.get(relative) == digest, 'copied fixture differs')
    require(command['argv'] == helper.server_command(stage.path.name,
                stage.path / 'run', binary=Path(build['candidate']['path'])) and
            command['effective_q4t_environment'] == helper.environment(
                stage.path.name, stage.path / 'run', {}), 'actual invocation differs')
    require(stage.text(prefix + 'binary.sha256').strip() == build['candidate_sha256'],
            'launched candidate hash differs')
    capacity = helper.capacity_evidence(log, stage.path.name)
    require(stage.document(prefix + 'capacity.json') == capacity, 'capacity differs')
    final = helper.reservation_evidence(log, stage.path.name, finalized=True)
    ready_log = '\n'.join(line for line in log.splitlines() if not (
        line.startswith('[startup-reservation] ') and
        decode(line.split(' ', 1)[1])['event'] == 'finalized'))
    ready = helper.reservation_evidence(ready_log, stage.path.name)
    require(stage.document(prefix + 'summary-ready.json') == ready and
            stage.document(prefix + 'summary-final.json') == final,
            'summary receipts differ from raw product logs')
    if not cfg['real_default']:
        old_name = 'boundary-lock-v3' if not cfg['mtp'] else 'mtp-lock-v3'
        plan_path = verified_file(OLD_CACHE / old_name, 'run/startup-plan.json')
        plan = read(plan_path)
        require(ready['plan_sha256'] == plan_digest(plan) and
                ready['count'] == plan['count'] and
                ready['device_bytes'] == plan['device_bytes'],
                'product plan differs from independently sealed name/byte oracle')
    exited = stage.document(prefix + 'exit.json')
    require(exited['server'] == 0 and exited['failure'] is None and
            exited['cleanup_failure'] is None and not exited['logging_errors'] and
            exited['completed'] == cfg['requests'] and
            exited['http_output_checks_passed'] is True and
            exited['performance_acceptance'] is False, 'HTTP exit failed')
    rows = stage.document(prefix + 'results.json')
    require(len(rows) == cfg['requests'] and len({r['response_id'] for r in rows}) ==
            len(rows), 'HTTP count/response identity differs')
    fixture = stage.document('run/fixtures/manifest.json')
    require(len(fixture) == 1 and fixture[0]['length'] == 1024 and
            fixture[0]['expected'] == '710003', 'fixture differs')
    prompt = decode(stage.text('run/fixtures/requests.jsonl').strip())['prompt']
    require(hashlib.sha256(prompt.encode()).hexdigest() == fixture[0]['prompt_sha256'],
            'prompt hash differs')
    for i, row in enumerate(rows):
        case = prefix + ('quality/' if i == 0 else 'quality-reuse/')
        actual = stage.document(case + 'responses.json')
        require(len(actual) == 1 and all(row[k] == v for k, v in actual[0].items())
                and stage.document(case + 'client-exit.json')['returncode'] == 0,
                'HTTP client result differs')
        require(row['success'] and row['exact_match'] and row['text'] == '710003' and
                row['actual_input'] == 1024 and row['actual_output'] == 7 and
                row['requested_max_tokens'] == 32 and row['request_stream'] and
                row['finish'] == ['stop'] and row['response_id_valid'] and
                row['prompt_sha256'] == fixture[0]['prompt_sha256'] and
                row['timing']['within_client_boundaries'], 'exact HTTP gate failed')
    require(stage.document(prefix + 'request-reservation-retention.json') ==
            [ready] * len(rows), 'plan rebuilt/changed across same-instance requests')
    events = stage.lines(prefix + 'events.jsonl')
    require(len([r for r in events if r['event'] == 'server_started']) == 1 and
            len([r for r in events if r['event'] == 'client_before']) == len(rows),
            'model/HTTP event budget differs')
    times = [one(events, name)['monotonic_seconds'] for name in
             ('server_before_start', 'server_started', 'server_ready',
              'server_before_shutdown', 'server_after_shutdown')]
    require(all(finite(v) for v in times) and times == sorted(times),
            'server lifecycle order differs')
    return dict(capacity=capacity, ready=ready, final=final, requests=rows,
                same_instance_requests=len(rows), events=events,
                startup_wall_seconds=times[2] - times[1])


def audit_stage(name, build):
    path = BASE / name
    result = dict(name=name, status='NOT_RUN', runtime_accepted=False,
                  model_starts=0, http_requests=0, preparations=0, errors=[])
    if not path.exists():
        return result
    if not (path / 'sealed-files.json').exists():
        return dict(result, status='INCOMPLETE', errors=['stage is not sealed'])
    try:
        stage = SealedStage(path)
        result['sealed_manifest_sha256'] = sha(stage.manifest_path)
        result['sealed_files'] = len(stage.manifest)
        state = stage.document('state.json')
        cfg = configuration(name)
        require(stage.document('build.json') == build and state['configuration'] == cfg
                and state['observer_sha'] == build['candidate_sha256'] and
                state['default_sha'] == build['default_sha256'] and
                state['default_unchanged'], 'stage identity/configuration differs')
        for relative, digest in stage.document('sources.json').items():
            require(stage.manifest['source/' + relative] == digest,
                    'runtime source receipt differs: ' + relative)
        for relative in ('tools/verify/startup_delivery/run.py',
                         'tools/verify/startup_delivery/build.py',
                         'tools/verify/warm_reclaim/resources.py',
                         'tools/verify/startup_cache/locked_warm.py',
                         'tools/verify/startup_cache/cgroup_observer.py',
                         'tools/verify/vmm_compare/cache_start.py',
                         'tools/evalscope/gpu_exclusive.py',
                         'tools/evalscope/nvml_process_query.py',
                         'tools/evalscope/isolated_scope.py',
                         'tools/evalscope/scope_payload.py',
                         'tools/evalscope/run_acceptance.py',
                         'tools/evalscope/run_budget_experiment.py',
                         'tools/evalscope/offload_policy.py',
                         'tools/evalscope/file_cache.py'):
            expected = (ORIGINAL_RUN_SHA if relative ==
                'tools/verify/startup_delivery/run.py' and
                name in (*REUSED_STAGES, RETAINED_FAILURE) else sha(ROOT / relative))
            require(stage.manifest['source/' + relative] == expected,
                    'runtime tool differs from sealed identity: ' + relative)
        budget = stage.document('attempt-budget.json')
        revised = name not in (*REUSED_STAGES, RETAINED_FAILURE)
        preceding = REUSED_STAGES if name == RETAINED_FAILURE else STAGES[:STAGES.index(name)]
        require([r['name'] for r in budget['prior']] == list(preceding) and
                budget['model_start_limit'] == 6 and
                budget['preparation_limit'] == (7 if revised else 6)
                and budget['http_limit'] == 9 and budget['full_hot_preparation_limit'] == 1
                and budget['planned_model_starts'] == 1 and
                budget['planned_http_requests'] == cfg['requests'] and budget['no_retry']
                and budget['prior_model_starts'] == len(preceding) and
                budget['prior_http_requests'] == sum(configuration(p)['requests']
                                                    for p in preceding),
                'frozen attempt budget differs')
        if revised:
            require(budget['retained_failures'] == [dict(name=RETAINED_FAILURE,
                model_starts=0, http_requests=0, preparations=1,
                manifest_sha256=RETAINED_SEAL_SHA, audit_sha256=RETAINED_AUDIT_SHA,
                candidate_sha256=build['candidate_sha256'])] and
                budget['prior_preparations'] == len(preceding) + 1 and
                budget['cold_pass_limit'] == 2 and budget['cold_retry_policy'] ==
                'observed_nonzero_files_only', 'revised attempt budget differs')
        for prior in budget['prior']:
            require(prior['model_starts'] == 1 and prior['http_requests'] ==
                    configuration(prior['name'])['requests'] and
                    prior['candidate_sha256'] == build['candidate_sha256'] and
                    prior['manifest_sha256'] == sha(BASE / prior['name'] /
                                                   'sealed-files.json'),
                    'prior-attempt identity differs')
        require(state['collector_profile'] == PROFILE and
                state['kernel_timeline'] == 'NOT_COLLECTED' and
                not any(p.startswith('kernel/') for p in stage.manifest),
                'collector profile differs')
        events_path = 'run/normal/http/events.jsonl'
        events = stage.lines(events_path) if events_path in stage.manifest else []
        result['model_starts'] = sum(r['event'] == 'server_started' for r in events)
        result['http_requests'] = sum(r['event'] == 'client_before' for r in events)
        result['preparations'] = int('run/cache-before.json' in stage.manifest)
        if state.get('no_owned_processes_started') is True:
            require(state['status'] in ('ENV_ABORT', 'RECOVERY_REQUIRED') and
                    result['model_starts'] == result['http_requests'] ==
                    result['preparations'] == 0,
                    'no-owned-process attempt has workload evidence')
            return dict(result, status='RUNTIME_NOT_ACCEPTED', state=state,
                        reason='lease/recovery gate prevented any owned workload')
        common, hot = reused_helpers(stage)
        guard = stage.document('lease/exit.json')
        result['nvml'] = common.audit_nvml(stage, must_succeed=False)
        resources = stage.document('resources/exit.json')
        result['resources'] = resources
        require(state['resources'] == dict(returncode=0, result=resources) and
                resources['status'] == 'COMPLETED' and not resources['truncated'] and
                not resources['failures'] and resources['system_samples'] > 0 and
                resources['cache_samples'] > 0 and resources['cgroup_samples'] > 0,
                'resource evidence incomplete')
        result['owned'] = hot.audit_owned(stage)
        if state['status'] != 'RUNTIME_COMPLETED' or guard['status'] != 'COMPLETED':
            return dict(result, status='RUNTIME_NOT_ACCEPTED', state=state,
                        guard=guard, kernel_timeline='NOT_COLLECTED')
        require(state['guard_returncode'] == 0 and state['guard'] == guard and
                guard['compute_backend'] == 'nvml' and guard['child_returncode'] == 0
                and guard['poll_interval_seconds'] == 1 and guard['external_lease']
                and not guard.get('cleanup_error') and result['nvml']['all_ok'],
                'GPU guard/NVML gate failed')
        helper = frozen_runner(stage, build)
        http = audit_http(stage, build, cfg, helper)
        require(resources['start']['monotonic'] <=
                one(events, 'server_before_start')['monotonic_seconds'] and
                resources['finished']['monotonic'] >=
                one(events, 'server_after_shutdown')['monotonic_seconds'],
                'resource collector did not cover model lifecycle')
        result['cache'] = audit_cache(stage, cfg, http.pop('events'), hot)
        result['scope'] = audit_scope(stage, cfg)
        result['http'] = http
        result['kernel_timeline'] = 'NOT_COLLECTED'
        result.update(status='PASS', runtime_accepted=True,
                      candidate_sha256=build['candidate_sha256'])
        result['used_sealed_inputs'] = sorted(stage.used)
    except (ValueError, KeyError, TypeError, OSError, IndexError, StopIteration) as error:
        result.update(status='FAILED', runtime_accepted=False)
        result['errors'].append(str(error))
    return result


def retained_failure(build):
    """Authenticate a known rejected preparation; never promote it to a pass."""
    path = BASE / RETAINED_FAILURE
    receipt_path = BASE / 'audit-boundary-v1.json'
    require(sha(path / 'sealed-files.json') == RETAINED_SEAL_SHA and
            sha(receipt_path) == RETAINED_AUDIT_SHA,
            'retained failure seal/audit identity differs')
    receipt = read(receipt_path)
    old = receipt['stages'][RETAINED_FAILURE]
    require(receipt['runtime_accepted'] is False and not receipt['errors'] and
            receipt['candidate_sha256'] == build['candidate_sha256'] and
            old['status'] == 'RUNTIME_NOT_ACCEPTED' and
            old['runtime_accepted'] is False and not old['errors'] and
            old['sealed_manifest_sha256'] == RETAINED_SEAL_SHA and
            old['model_starts'] == old['http_requests'] == 0 and
            old['preparations'] == 1, 'retained failure receipt differs')
    current = audit_stage(RETAINED_FAILURE, build)
    require(current['status'] == 'RUNTIME_NOT_ACCEPTED' and
            current['runtime_accepted'] is False and not current['errors'] and
            current['model_starts'] == current['http_requests'] == 0 and
            current['preparations'] == 1 and current['nvml']['all_ok'],
            'retained failure independent audit differs: ' + str(current['errors']))
    # The preceding call verified every file; reuse those authenticated bytes.
    state, guard = current['state'], current['guard']
    events = [decode(line) for line in
        (path / 'run/normal/http/events.jsonl').read_text().splitlines() if line]
    exited = read(path / 'run/normal/http/exit.json')
    require(state['status'] == 'RUNTIME_NOT_ACCEPTED' and state['guard'] == guard and
            state['guard_returncode'] == guard['child_returncode'] == 1 and
            guard['status'] == 'COMMAND_FAILED' and not guard.get('cleanup_error') and
            state['scope'] == state['scope_final'] == dict(scope_created=False) and
            state['lock_owner_cleanup'] == dict(started=False, live_lock_owner=False)
            and not any(row['event'] in ('server_before_start', 'server_started',
                'server_ready', 'client_before', 'client_after') for row in events)
            and not (path / 'run/normal/http/isolation').exists() and
            not (path / 'run/normal/http/server.log').read_text() and
            exited['server'] is None and exited['completed'] == 0 and
            exited['http_output_checks_passed'] is False and
            exited['failure'] == "RuntimeError('cold payload vector is nonzero')" and
            exited['cleanup_failure'] is None and not exited['logging_errors'],
            'retained failure is not the known clean pre-launch rejection')
    preparation = read(path / 'run/cache-preparation.json')
    attempts = preparation['attempts']
    require(not preparation['errors'] and len(attempts) == 1 and
            not attempts[0]['advice_errors'] and attempts[0]['pass_number'] == 1,
            'retained failure preparation differs')
    observed = cache_vector(attempts[0]['observation'])
    launch = cache_vector(read(path / 'run/cache-launch.json'))
    nonzero = {name: value for name, value in observed.items() if value}
    require(observed == launch and sum(observed.values()) == 262144 and
            attempts[0]['payload_resident_bytes'] == 262144 and
            nonzero == {str(MODEL / 'layer-00028-experts-0128-0255.safetensors'): 262144},
            'retained failure residual identity differs')
    return dict(name=RETAINED_FAILURE, status='PREPARATION_FAILED',
        runtime_accepted=False, model_starts=0, http_requests=0, preparations=1,
        candidate_sha256=build['candidate_sha256'],
        sealed_manifest=identity(path / 'sealed-files.json'),
        audit=identity(receipt_path), sealed_files=current['sealed_files'],
        residual_payload_bytes=262144, residual_files=nonzero,
        nvml=current['nvml'], resources=current['resources'], owned=current['owned'],
        reason='Advisory eviction left known nonzero payload; no model or HTTP began.')


def load_build(path):
    spec = importlib.util.spec_from_file_location('delivery_audit_build', HERE / 'build.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.verify(path)
    return read(path)


def cpp_function(source, signature):
    start = source.index(signature)
    begin = source.index('{', start)
    depth = 1
    for cursor in range(begin + 1, len(source)):
        depth += (source[cursor] == '{') - (source[cursor] == '}')
        if depth == 0:
            return source[start:cursor + 1]
    raise ValueError('unterminated selected C++ function')


def reused_contracts(build):
    """Bind old receipts to unchanged code, without running research binaries."""
    previous = OLD_CACHE / 'mtp-lock-v3'
    frozen = Path(build['source_snapshot'])
    unchanged = {}
    for relative in ('src/runtime/startup_model_plan.cpp',
                     'src/runtime/startup_mtp_plan.cpp',
                     'src/runtime/startup_weight_plan.h'):
        old_path = verified_file(previous, 'source/' + relative)
        new_path = frozen / relative
        require(sha(old_path) == sha(new_path), 'reused sizing source changed')
        unchanged[relative] = dict(previous=identity(old_path), current=identity(new_path))
    relative = 'src/runtime/startup_plan.cpp'
    old_path = verified_file(previous, 'source/' + relative)
    new_path = frozen / relative
    old_text, new_text = old_path.read_text(), new_path.read_text()
    functions = ('Status StartupPlan::AddBytes(', 'Status StartupPlan::Add(',
                 'Status StartupPlan::CheckTensor(', 'Status StartupPlan::AddTensor(',
                 'Status Product(', 'Status CheckArchitecture(')
    for signature in functions:
        require(cpp_function(old_text, signature) == cpp_function(new_text, signature),
                'reused core implementation changed: ' + signature)
    old_build = cpp_function(old_text, 'Status BuildStartupPlan(')
    new_build = cpp_function(new_text, 'Status BuildStartupPlan(')
    old_tail = old_build[old_build.index('Status status = CheckArchitecture(cfg);'):]
    new_tail = new_build[new_build.index('status = CheckArchitecture(cfg);'):]
    require(old_tail.removeprefix('Status ') == new_tail,
            'BuildStartupPlan metadata/plan logic changed after support selection')
    receipts = {}
    for name, directory, count_field, count in (
            ('core', OLD_PLAN / 'host-core-order-v1', 'count', 35),
            ('metadata', OLD_PLAN / 'host-plan-v1', 'completed_cases', 27)):
        path = verified_file(directory, 'result.json')
        result = read(path)
        require(result['status'] == 'PASS' and result[count_field] == count and
                result['gpu_execution'] is False, 'historical CPU receipt differs')
        receipts[name] = dict(receipt=identity(path),
            seal=identity(directory / 'sealed-files.json'), count=count)
    return dict(status='REUSED_UNCHANGED_IMPLEMENTATION', receipts=receipts,
        unchanged_sizing_files=unchanged, unchanged_core_functions=list(functions),
        build_tail_after_support_unchanged=True,
        core_sources=dict(previous=identity(old_path), current=identity(new_path)),
        core_source_diff=''.join(difflib.unified_diff(old_text.splitlines(True),
            new_text.splitlines(True), fromfile=str(old_path), tofile=str(new_path))),
        support_change_coverage='host-support-v1: actual selector CPU contract',
        research_plan_only_program_invoked=False,
        limitation='Historical CPU checks do not accept a different runtime binary.')


def host_contracts(build):
    frozen = Path(build['source_snapshot'])
    support = SealedStage(BASE / 'host-support-v1')
    support_result = support.document('result.json')
    require(support_result['status'] == 'PASS' and support_result['count'] == 11
            and support_result['gpu_execution'] is False and
            support_result['metadata_or_payload_io'] is False,
            'support CPU contract failed')
    for relative, digest in support_result['source_sha256'].items():
        path = frozen / relative
        if relative == 'include/q4t/server/server_options.h':
            # This exact approved comment correction occurred after the CPU
            # receipt. Reconstruct its old bytes; no semantic exception exists.
            new = ('  // 0 = derive capacity with the memory budget, or use ModelConfig\'s 8192\n'
                   '  // when --no-budget is set. Positive requests may be capped by the budget.\n')
            old = ('  // 0 = use ModelConfig default (8192); >0 overrides (e.g. 262144 for the\n'
                   '  // model\'s full context; see PHASES.md 262K memory budget — use max_seq=1).\n')
            text = path.read_text()
            require(text.count(new) == 1 and
                    hashlib.sha256(text.replace(new, old).encode()).hexdigest() == digest,
                    'support header differs beyond approved comment correction')
        else:
            require(sha(path) == digest, 'tested support input differs: ' + relative)
    reserve_dir = BASE / 'host-reservation-v1'
    reserve_seal = read(reserve_dir / 'seal.json')['files']
    require({str(p.relative_to(reserve_dir)) for p in reserve_dir.rglob('*')
             if p.is_file() and p.name != 'seal.json'} == set(reserve_seal),
            'reservation CPU file set differs')
    for name, item in reserve_seal.items():
        path = reserve_dir / name
        require(path.resolve().is_relative_to(reserve_dir.resolve()) and
                sha(path) == item['sha256'] and path.stat().st_size == item['bytes'],
                'reservation CPU seal differs')
    reservation = read(reserve_dir / 'result.json')
    require(reservation['status'] == 'PASS' and reservation['tests_passed'] == 19
            and reservation['build_returncode'] == reservation['test_returncode'] == 0
            and reservation['gpu_used'] is False and
            reservation['cuda_library_linked'] is False and
            reservation['source_after_sha256'] == reservation['source_sha256'],
            'reservation CPU contract failed')
    for name, digest in reservation['source_sha256'].items():
        require(sha(frozen / name) == digest, 'tested reservation source differs')
    release_path = BASE / 'host-release-v1/result.json'
    release = read(release_path)
    require(release['passed'] is True and release['exit'] == 0 and
            len(set(release['required_tests'])) == len(release['required_tests']) == 54
            and release['binary'] == build['host_test_binary']['path'] and
            release['binary_sha256'] == build['host_test_binary']['sha256'] and
            release['manifest_sha256'] == sha(release_path.parent / 'required_host_tests.txt'),
            'product host prerequisite receipt differs')
    runner = SealedStage(BASE / 'runner-host-accepted-v2')
    runner_result = runner.document('result.json')
    require(runner_result['status'] == 'PASS' and
            runner_result['effective_contracts'] == 25 and
            runner_result['reused_contracts'] == 21 and
            runner_result['new_affected_contracts'] == 4 and
            runner_result['gpu_execution'] is False and
            runner_result['actual_cache_operations'] is False and
            runner_result['sources'][str(HERE / 'run.py')] == ORIGINAL_RUN_SHA,
            'runner CPU contract/source differs')
    for item in runner_result['receipts']:
        require(sha(item['path']) == item['sha256'], 'runner raw CPU receipt changed')
    revised = SealedStage(BASE / 'runner-host-accepted-v3')
    revised_result = revised.document('result.json')
    require(revised_result['status'] == 'PASS' and
            revised_result['effective_contracts'] == 33 and
            revised_result['reused_contracts'] == 20 and
            revised_result['new_affected_contracts'] == 13 and
            revised_result['gpu_execution'] is False and
            revised_result['actual_cache_operations'] is False and
            revised_result['sources'][str(HERE / 'run.py')] == sha(HERE / 'run.py') and
            revised_result['sources'][str(HERE / 'test_run.py')] == sha(HERE / 'test_run.py'),
            'revised runner CPU contract/source differs')
    for item in revised_result['receipts']:
        require(sha(item['path']) == item['sha256'], 'revised runner raw receipt changed')
    require(str(runner.file('result.json')) in
            [row['path'] for row in revised_result['receipts']],
            'revised runner does not bind reused original CPU evidence')
    return dict(status='PASS', counts=dict(support=11, reservation=19,
                host_prerequisites=54, original_runner=25, runner=33),
                approved_comment_only_change='ServerOptions max_len documentation',
                evidence=[identity(support.manifest_path),
                    identity(support.file('result.json')),
                    identity(reserve_dir / 'seal.json'),
                    identity(reserve_dir / 'result.json'), identity(release_path),
                    identity(runner.manifest_path), identity(runner.file('result.json')),
                    identity(revised.manifest_path), identity(revised.file('result.json')),
                    *[identity(row['path']) for row in runner_result['receipts']],
                    *[identity(row['path']) for row in revised_result['receipts']]])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', choices=(*STAGES, RETAINED_FAILURE))
    parser.add_argument('--build-manifest', type=Path, default=BASE / 'candidate-v1/build.json')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    require(output.is_relative_to(BASE.resolve()) and
            not any(output.is_relative_to(BASE / name)
                    for name in (*STAGES, RETAINED_FAILURE)) and
            not output.is_relative_to(args.build_manifest.resolve().parent),
            'output must be a new unsealed BASE file')
    report = dict(schema=1, status='NOT_RUN', runtime_accepted=False, errors=[],
                  kernel_timeline='NOT_COLLECTED', performance_acceptance=False,
                  gpu_execution=False, audit_source_sha256=sha(Path(__file__)))
    if args.build_manifest.exists():
        try:
            build = load_build(args.build_manifest)
            report.update(candidate=build['candidate'],
                candidate_sha256=build['candidate_sha256'],
                build_manifest=identity(args.build_manifest),
                source_manifest=build['source_manifest'], default=build['default_pair'])
            report['reused_contracts'] = reused_contracts(build)
            report['host_contracts'] = host_contracts(build)
            failure = retained_failure(build)
            report['retained_failures'] = [failure]
            names = (args.stage,) if args.stage else STAGES
            stages = {name: audit_stage(name, build) for name in names}
            report['stages'] = stages
            report['budget'] = dict(model_starts=sum(s['model_starts'] for s in stages.values()),
                http_requests=sum(s['http_requests'] for s in stages.values()),
                preparations=sum(s['preparations'] for s in stages.values()) +
                    (0 if RETAINED_FAILURE in stages else failure['preparations']),
                model_start_limit=6, http_limit=9, preparation_limit=7,
                full_hot_preparation_limit=1, deployment_start_limit=2,
                deployment_http_limit=2, no_automatic_retry=True)
            require(report['budget']['model_starts'] <= 6 and
                    report['budget']['http_requests'] <= 9 and
                    report['budget']['preparations'] <= 7, 'candidate budget exceeded')
            for later, earlier in [('default-inherited-v1', 'default-cold-v1'),
                                   ('c256-inherited-v1', 'c256-hot-v1')]:
                if later in stages and stages[later]['runtime_accepted']:
                    prior = stages.get(earlier) or audit_stage(earlier, build)
                    require(prior['runtime_accepted'] and
                            stages[later]['http']['ready'] == prior['http']['ready'] and
                            stages[later]['http']['capacity'] == prior['http']['capacity'],
                            'restart plan/capacity differs')
            accepted = all(s['runtime_accepted'] for s in stages.values())
            report.update(runtime_accepted=accepted,
                status='PASS_BOUNDED_RELIABILITY' if accepted else
                       'FAILED' if any(s['status'] == 'FAILED' for s in stages.values())
                       else 'NOT_RUN' if all(s['status'] == 'NOT_RUN'
                                            for s in stages.values()) else 'INCOMPLETE')
            if args.stage:
                report['sealed_manifest_sha256'] = stages[args.stage].get('sealed_manifest_sha256')
            bound = [report['build_manifest'], report['source_manifest'],
                     *report['host_contracts']['evidence'],
                     failure['sealed_manifest'], failure['audit']]
            bound += [identity(BASE / n / 'sealed-files.json')
                      for n, s in stages.items() if s.get('sealed_manifest_sha256')]
            report['evidence_inputs'] = {item['path']: item for item in bound}
            report['limitations'] = [
                'Retained boundary-v1 is a rejected zero-start preparation, not an accepted run.',
                'No complete kernel timeline or driver causal/performance claim.',
                'Only frozen configurations and 1K exact text HTTP are accepted.',
                'Two-slot boundary uses serial requests, not a concurrency test.',
                'Reservation finalization covers planned storage only; runtime '
                'static caches may persist until CUDA context/process exit.',
                'Memcg limits are not total physical RAM limits.',
                'Natural inherited cache is measured, not asserted identical/full hot.']
        except (ValueError, KeyError, TypeError, OSError, RuntimeError) as error:
            report.update(status='FAILED', runtime_accepted=False)
            report['errors'].append(str(error))
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x') as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps({k: report[k] for k in ('status', 'runtime_accepted', 'errors')}))
    return 0 if report['runtime_accepted'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
