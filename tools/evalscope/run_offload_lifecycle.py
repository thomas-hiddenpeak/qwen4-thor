"""Bounded offload business/lifecycle HTTP contracts on one explicit binary.

No performance verdict, cache clearing, new questions or adaptive retries.
All mode runs the one-shot fault/lifecycle sequence before the frozen six
business requests, retaining one service and its caches throughout.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import traceback

from isolated_service import IsolatedService, UNIT_PATTERN
from monitor_memory import find_pids
from offload_policy import AXES, partition_path_evidence, policy_environment

ROOT = Path(__file__).resolve().parents[2]


def save(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def require_idle_device():
    """Fail closed before creating evidence or starting any owned process."""
    if find_pids('q4t'):
        raise RuntimeError('another q4t process is active')
    gpu = subprocess.run(['nvidia-smi', '--query-compute-apps=pid',
                          '--format=csv,noheader,nounits'],
                         capture_output=True, text=True, timeout=10)
    if gpu.returncode or gpu.stdout.strip():
        raise RuntimeError('GPU compute state unavailable or occupied')


def contract_environment(chunk_order, mode, partition=0,
                         policy_axis='chunk-order'):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith('Q4T_') and k != 'LD_PRELOAD'}
    env.update(policy_environment(chunk_order, partition, policy_axis))
    env['Q4T_MOE_STREAMS'] = '1'
    if mode != 'business':
        env['Q4T_RESIDENCY_FAIL_EXPERT'] = 'first'
    return env


def reference_identity(model_dir, fixtures_path, manifest_path, env,
                        policy_axis='chunk-order'):
    """Bind metadata only; never read model tensor payloads for identity."""
    model_dir = Path(model_dir).resolve()
    axis_env = ('Q4T_MOE_PARTITION' if policy_axis == 'partition'
                else 'Q4T_MOE_CHUNK_ORDER')
    result = dict(model_dir=str(model_dir),
                model_config_sha256=sha(model_dir / 'config.json'),
                model_index_sha256=sha(model_dir / 'model.safetensors.index.json'),
                fixture_sha256=sha(fixtures_path), manifest_sha256=sha(manifest_path),
                comparable_q4t_environment={k: v for k, v in env.items()
                    if k.startswith('Q4T_') and k not in
                    (axis_env, 'Q4T_RESIDENCY_FAIL_EXPERT')})
    if policy_axis == 'partition':
        result['policy_axis'] = policy_axis
    return result


def require_reference_identity(protocol, identity):
    for key, value in identity.items():
        if protocol.get(key) != value:
            raise ValueError('same-binary reference identity mismatch: ' + key)
    axis_env = ('Q4T_MOE_PARTITION' if identity.get('policy_axis') == 'partition'
                else 'Q4T_MOE_CHUNK_ORDER')
    effective = {k: v for k, v in protocol.get('effective_q4t_environment', {}).items()
                 if k.startswith('Q4T_') and k not in
                 (axis_env, 'Q4T_RESIDENCY_FAIL_EXPERT')}
    if effective != identity['comparable_q4t_environment']:
        raise ValueError('same-binary reference effective environment mismatch')


class Contracts:
    def __init__(self, args, directory, service, fixtures, references):
        self.args, self.directory, self.service = args, directory, service
        self.fixtures, self.references = fixtures, references
        self.records = []
        self.business_results = []
        self.deadline = time.monotonic() + args.contract_timeout_s

    def remaining(self, limit):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('frozen contract deadline exceeded')
        return min(limit, remaining)

    def phase(self, label):
        (self.directory / 'memory-phase.txt').write_text(label + '\n')

    def record(self, value):
        self.records.append(value)
        save(self.directory / 'results.json', self.records)
        print(json.dumps(value, ensure_ascii=False), flush=True)

    def http(self, path, body=None, label=None, first=None, timeout=None):
        conn = HTTPConnection('127.0.0.1', self.args.port,
                              timeout=self.remaining(timeout or self.args.request_timeout_s))
        if label:
            save(self.directory / (label + '-request.json'), body)
        started = time.monotonic()
        raw = bytearray()
        try:
            conn.request('GET' if body is None else 'POST', path,
                         None if body is None else json.dumps(body),
                         {'Content-Type': 'application/json'})
            response = conn.getresponse()
            if body and body.get('stream'):
                while True:
                    self.remaining(self.args.request_timeout_s)
                    line = response.readline()
                    if not line:
                        break
                    raw.extend(line)
                    if first and line.startswith(b'data: {'):
                        event = json.loads(line[6:])
                        if any(choice.get('delta', {}).get('content')
                               for choice in event.get('choices', [])):
                            first.set()
            else:
                raw.extend(response.read())
            result = dict(status=response.status, body=raw.decode(),
                          elapsed_seconds=time.monotonic() - started)
            if label:
                save(self.directory / (label + '-response.json'), result)
            return result
        finally:
            conn.close()

    def health(self):
        result = self.http('/healthz', timeout=10)
        if result['status'] != 200:
            raise RuntimeError('health request failed: ' + str(result))
        return json.loads(result['body'])

    def wait_free(self, expected):
        deadline = time.monotonic() + self.remaining(self.args.cancel_timeout_s)
        while time.monotonic() < deadline:
            value = self.health()
            if value.get('seq_slots_free') == expected:
                if value.get('seq_slots_total') != 1 or not value.get('gpu_healthy'):
                    raise RuntimeError('unexpected slot capacity or unhealthy GPU')
                return value
            if self.service.poll() is not None:
                raise RuntimeError('service exited during contract')
            time.sleep(.05)
        raise TimeoutError(f'seq_slots_free did not become {expected}')

    def body(self, item, **extra):
        return dict(model='qwen3.8-flash-next',
                    messages=[dict(role='user', content=item['prompt'])],
                    max_tokens=item['max_tokens'], temperature=0,
                    stream=False, **extra)

    def compare(self, result, item):
        if result['status'] != 200:
            raise RuntimeError('expected successful completion: ' + str(result))
        data = json.loads(result['body'])
        ref = self.references[item['id']]
        usage, choice = data['usage'], data['choices'][0]
        finish = ref['finish']
        if isinstance(finish, list):
            if len(finish) != 1:
                raise ValueError('invalid frozen reference finish')
            finish = finish[0]
        pairs = dict(prompt_sha256=(hashlib.sha256(item['prompt'].encode()).hexdigest(), ref['prompt_sha256']),
                     actual_input=(usage['prompt_tokens'], ref['actual_input']),
                     actual_output=(usage['completion_tokens'], ref['actual_output']),
                     finish=(choice['finish_reason'], finish),
                     text=(choice['message']['content'], ref['text']))
        mismatches = [name for name, (actual, expected) in pairs.items() if actual != expected]
        # Historical output is retained as a diagnostic. The on/off gate
        # requires the baseline artifact to come from this same binary.
        if mismatches and (self.args.reference_role == 'same_binary' or
                           any(key in mismatches for key in ('prompt_sha256', 'actual_input'))):
            raise RuntimeError('frozen reference mismatch: ' + ', '.join(mismatches))
        return dict(id=item['id'], fixture_id=item['id'], actual_input=usage['prompt_tokens'],
                    actual_output=usage['completion_tokens'], finish=choice['finish_reason'],
                    text=choice['message']['content'],
                    text_sha256=hashlib.sha256(choice['message']['content'].encode()).hexdigest(),
                    prompt_sha256=pairs['prompt_sha256'][0], reference_equal=not mismatches,
                    reference_role=self.args.reference_role,
                    historical_precheck_differences=(mismatches if self.args.reference_role == 'historical_precheck' else []))

    def complete(self, item, label):
        self.phase(label)
        self.service.snapshot(label + ':before')
        result = self.http('/v1/chat/completions', self.body(item), label)
        checked = self.compare(result, item)
        health = self.wait_free(1)
        self.service.snapshot(label + ':after')
        self.record(dict(case=label, passed=True, health_after=health, **checked))
        return checked

    def business(self):
        for index, item in enumerate(self.fixtures, 1):
            checked = self.complete(item, f'business-{index}')
            self.business_results.append(dict(success=True, **checked))
            save(self.directory / 'business-results.json', self.business_results)

    def lifecycle(self, pool):
        p45 = next(item for item in self.fixtures if '44k' in item['id'])
        p8 = next(item for item in self.fixtures if '8k' in item['id'])
        self.phase('fault-first')
        self.service.snapshot('fault-first:before')
        result = self.http('/v1/chat/completions', self.body(p45), 'fault-first')
        if result['status'] != 500 or 'fault injection' not in result['body']:
            raise RuntimeError('expected one-shot residency fault was not observed')
        self.wait_free(1)
        self.service.snapshot('fault-first:after')
        self.record(dict(case='fault-first', passed=True, expected_http=500,
                         limit='failure before expert read/commit, not a partial H2D failure'))
        self.complete(p45, 'fault-recovery')
        a_first = self.complete(p8, 'slot-reuse-A-first')

        self.phase('prefill-cancel')
        rid, token = 'offload-prefill-cancel', secrets.token_hex(32)
        payload = self.body(p45, request_id=rid, cancel_token=token)
        offset = (self.directory / 'server.log').stat().st_size
        self.service.snapshot('prefill-cancel:before')
        pending = pool.submit(self.http, '/v1/chat/completions', payload, 'prefill-original')
        self.wait_free(0)
        time.sleep(2)  # Frozen injection delay; drain position is checked below.
        response = self.http('/v1/requests/cancel', dict(request_id=rid, cancel_token=token),
                             'prefill-cancel', timeout=10)
        if response['status'] != 202:
            raise RuntimeError('prefill cancellation was not accepted')
        original = pending.result(timeout=self.remaining(self.args.cancel_timeout_s))
        if original['status'] != 409 or 'during prefill' not in original['body']:
            raise RuntimeError('prefill request lacks cancellation terminal')
        health = self.wait_free(1)
        with (self.directory / 'server.log').open('rb') as source:
            source.seek(offset)
            delta = source.read().decode(errors='replace')
        match = re.search(r'prefill cancelled seq=\d+ position=(\d+) total=(\d+)', delta)
        if not match or not 0 < int(match[1]) < int(match[2]):
            raise RuntimeError('no bounded partial-prefill drain position')
        self.service.snapshot('prefill-cancel:after')
        self.record(dict(case='prefill-cancel', passed=True, position=int(match[1]),
                         total=int(match[2]), health_after=health,
                         limit='outer prefill cancellation boundary; inner CUDA interruption not claimed'))
        self.complete(p45, 'prefill-recovery-B')
        a_again = self.complete(p8, 'slot-reuse-A-again')
        if a_first != a_again:
            raise RuntimeError('A-B-A slot reuse changed output or usage')
        self.record(dict(case='same-process-A-B-A', passed=True))

        self.phase('decode-cancel')
        rid, token = 'offload-decode-cancel', secrets.token_hex(32)
        payload = self.body(p8, request_id=rid, cancel_token=token)
        payload.update(stream=True, stream_options={'include_usage': True})
        first_token = threading.Event()
        self.service.snapshot('decode-cancel:before')
        pending = pool.submit(self.http, '/v1/chat/completions', payload,
                              'decode-original', first_token)
        first_deadline = time.monotonic() + self.remaining(self.args.request_timeout_s)
        while not first_token.wait(.1):
            if pending.done():
                raise RuntimeError('decode request ended before content: ' + str(pending.result()))
            if time.monotonic() >= first_deadline:
                raise TimeoutError('actual first content event not observed')
        response = self.http('/v1/requests/cancel', dict(request_id=rid, cancel_token=token),
                             'decode-cancel', timeout=10)
        if response['status'] != 202:
            raise RuntimeError('decode cancellation was not accepted')
        original = pending.result(timeout=self.remaining(self.args.cancel_timeout_s))
        events = [json.loads(line[6:]) for line in original['body'].splitlines()
                  if line.startswith('data: {')]
        if original['status'] != 200 or not any('cancelled' in str(e.get('error')) for e in events):
            raise RuntimeError('decode request lacks cancellation error event')
        if any(c.get('finish_reason') in ('length', 'stop') for e in events for c in e.get('choices', [])):
            raise RuntimeError('cancelled decode also reported normal completion')
        health = self.wait_free(1)
        self.service.snapshot('decode-cancel:after')
        self.record(dict(case='decode-cancel', passed=True, actual_content_seen=True,
                         cancellation_error_event=True, health_after=health))
        self.complete(p8, 'decode-recovery')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('business', 'lifecycle', 'all'), default='all')
    for name in ('binary', 'model-dir', 'hot-list', 'fixtures', 'reference', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--expected-binary-sha256', required=True)
    parser.add_argument('--chunk-order', type=int, choices=(0, 1))
    parser.add_argument('--policy-axis', choices=AXES, default='chunk-order')
    parser.add_argument('--partition', type=int, choices=(0, 1), default=0)
    parser.add_argument('--reference-role', choices=('historical_precheck', 'same_binary'),
                        default='same_binary')
    parser.add_argument('--systemd-unit', required=True)
    parser.add_argument('--host-cache-max-bytes', type=int)
    parser.add_argument('--port', type=int, default=8184)
    parser.add_argument('--startup-timeout-s', type=int, default=600)
    parser.add_argument('--request-timeout-s', type=int, default=1800)
    parser.add_argument('--cancel-timeout-s', type=int, default=300)
    parser.add_argument('--contract-timeout-s', type=int, default=7200)
    args = parser.parse_args()
    if args.chunk_order is None:
        args.chunk_order = 0 if args.policy_axis == 'partition' else 1
    try:
        env = contract_environment(args.chunk_order, args.mode,
                                   args.partition, args.policy_axis)
    except ValueError as error:
        parser.error(str(error))
    out = args.output.resolve()
    if not out.is_relative_to(ROOT / '.q4t-work'):
        parser.error('output must be under .q4t-work/')
    if not UNIT_PATTERN.fullmatch(args.systemd_unit):
        parser.error('systemd-unit must be a unique q4t-*.service')
    if any(value <= 0 for value in (args.startup_timeout_s, args.request_timeout_s,
                                    args.cancel_timeout_s, args.contract_timeout_s)):
        parser.error('timeouts must be positive')
    if args.host_cache_max_bytes is not None and args.host_cache_max_bytes <= 0:
        parser.error('host-cache-max-bytes must be positive')
    if sha(args.binary) != args.expected_binary_sha256:
        parser.error('binary does not match expected frozen SHA256')
    if not (args.binary.resolve().parent / 'CMakeCache.txt').is_file():
        parser.error('actual CMakeCache.txt must accompany binary')
    fixtures_path = args.fixtures / 'requests.jsonl'
    fixtures = [json.loads(line) for line in fixtures_path.read_text().splitlines() if line.strip()]
    references = {row['id']: row for row in json.loads(args.reference.read_text())}
    manifest_path = args.fixtures / 'manifest.json'
    manifest = {row['id']: row for row in json.loads(manifest_path.read_text())}
    if len(fixtures) != 6 or len({r['id'] for r in fixtures}) != 6:
        parser.error('expected exactly the frozen six business fixtures')
    for item in fixtures:
        digest = hashlib.sha256(item['prompt'].encode()).hexdigest()
        if (item['id'] not in references or item['id'] not in manifest or
                digest != references[item['id']]['prompt_sha256'] or
                digest != manifest[item['id']]['prompt_sha256'] or item.get('max_tokens') != 128):
            parser.error('fixture/reference/manifest identity mismatch')
    identity = reference_identity(args.model_dir, fixtures_path, manifest_path,
                                   env, args.policy_axis)
    if args.reference_role == 'historical_precheck':
        if args.mode != 'business' or args.chunk_order != 0 or args.partition != 0:
            parser.error('historical_precheck is only for off/business baseline collection')
    else:
        reference_protocol = json.loads((args.reference.parent / 'protocol.json').read_text())
        reference_exit = json.loads((args.reference.parent / 'exit.json').read_text())
        try:
            require_reference_identity(reference_protocol, identity)
        except ValueError as error:
            parser.error(str(error))
        if (reference_protocol['binary_sha256'] != args.expected_binary_sha256 or
                reference_protocol['effective_q4t_environment']['Q4T_MOE_CHUNK_ORDER'] != '0' or
                (args.policy_axis == 'partition' and
                 reference_protocol['effective_q4t_environment'].get('Q4T_MOE_PARTITION') != '0') or
                reference_protocol['mode'] != 'business' or not reference_exit['passed'] or
                reference_protocol['hot_sha256'] != sha(args.hot_list) or
                reference_protocol['fixture_sha256'] != sha(fixtures_path) or
                reference_protocol['source_sha256'].get(str(Path(__file__))) != sha(__file__) or
                len(references) != 6):
            parser.error('same-binary reference must be a complete successful off/business run')
    try:
        require_idle_device()
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        parser.error(str(error))
    out.mkdir(parents=True, exist_ok=False)
    command = [str(args.binary.resolve()), 'serve', '--model-dir', str(args.model_dir.resolve()),
               '--port', str(args.port), '--max-seq', '1', '--max-prefill', '8192',
               '--max-len', '262144', '--max-tokens', '256', '--no-mtp',
               '--request-deadline-ms', '1800000', '--moe-resident-slots', '256',
               '--moe-hot-list', str(args.hot_list.resolve())]
    sources = [Path(__file__), ROOT / 'tools/evalscope/isolated_service.py',
               ROOT / 'tools/evalscope/monitor_memory.py', ROOT / 'tools/evalscope/resource_metrics.py',
               ROOT / 'tools/evalscope/file_cache.py',
               ROOT / 'tools/evalscope/offload_policy.py']
    snapshot = out / 'tool-snapshot'; snapshot.mkdir()
    for source in sources:
        shutil.copy2(source, snapshot / source.name)
    shutil.copy2(args.binary.resolve().parent / 'CMakeCache.txt', out)
    save(out / 'protocol.json', dict(mode=args.mode, binary_sha256=args.expected_binary_sha256,
        **identity,
        argv=command, effective_q4t_environment={k: v for k, v in env.items() if k.startswith('Q4T_')},
        hot_sha256=sha(args.hot_list), reference_sha256=sha(args.reference),
        fixtures=str(args.fixtures.resolve()), reference=str(args.reference.resolve()),
        fixture_path=str(fixtures_path.resolve()), manifest_path=str(manifest_path.resolve()),
        source_sha256={str(source): sha(source) for source in sources},
        systemd_unit=args.systemd_unit, host_cache_max_bytes=args.host_cache_max_bytes,
        swap_max_bytes=0, args={k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
        physical_memory_total='INDETERMINATE', performance_acceptance=False,
        startup_fault='first request-time expert claim only; no partial-H2D injection',
        schedule='one fixed sequence; no adaptive repetition or reference changes'))
    memory = out / 'memory'; memory.mkdir()
    monitor_cmd = [sys.executable, '-B', str(ROOT / 'tools/evalscope/monitor_memory.py'),
                   '--pid-file', str(out / 'server.pid'), '--phase-file', str(out / 'memory-phase.txt'),
                   '--ready-file', str(memory / 'ready'), '--stop-file', str(memory / 'stop'),
                   '--out', str(memory), '--model-dir', str(args.model_dir.resolve()),
                   '--cgroup-path', '/sys/fs/cgroup/system.slice/' + args.systemd_unit,
                   '--interval', '1', '--gpu-interval', '10', '--file-cache-mode', 'endpoints']
    save(out / 'monitor-command.json', monitor_cmd)
    monitor_log = (memory / 'monitor.log').open('w')
    monitor = subprocess.Popen(monitor_cmd, stdout=monitor_log, stderr=subprocess.STDOUT)
    service, runner, pool = None, None, None
    failure, cleanup_errors, service_code, monitor_code = None, [], None, None
    try:
        deadline = time.monotonic() + 60
        while not (memory / 'ready').exists():
            if monitor.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError('monitor did not become ready')
            time.sleep(.1)
        (out / 'memory-phase.txt').write_text('startup\n')
        service = IsolatedService(command, cwd=ROOT, env=env, log_path=out / 'server.log',
                                  unit=args.systemd_unit, memory_max=args.host_cache_max_bytes,
                                  evidence=out / 'isolation')
        (out / 'server.pid').write_text(str(service.pid) + '\n')
        deadline = time.monotonic() + args.startup_timeout_s
        while 'serving on port' not in (out / 'server.log').read_text():
            if service.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError('service did not become ready')
            time.sleep(.5)
        startup = (out / 'server.log').read_text()
        if 'effective_max_len=262144 effective_max_seq=1 effective_max_prefill=8192' not in startup:
            raise RuntimeError('effective service capacity differs from protocol')
        runner = Contracts(args, out, service, fixtures, references)
        save(out / 'health-ready.json', runner.wait_free(1))
        pool = ThreadPoolExecutor(max_workers=2)
        if args.mode != 'business':
            runner.lifecycle(pool)
        if args.mode != 'lifecycle':
            runner.business()
        runtime_log = (out / 'server.log').read_text()
        policy = 'greedy_overlap' if args.chunk_order else 'original'
        activation = f'chunk_order={args.chunk_order} policy={policy}'
        multichunk = [int(value) for value in re.findall(r'\[residency\]\[diag\].*?chunks=(\d+)', runtime_log)
                      if int(value) > 1]
        save(out / 'chunk-path-evidence.json', dict(
            configured_chunk_order=args.chunk_order, activation_observed=activation in runtime_log,
            eligible_multichunk_forwards=len(multichunk),
            actual_non_identity_execution_order='NOT_INSTRUMENTED',
            diagnostic_scope='original partition membership, not executed chunk indices'))
        if activation not in runtime_log or not multichunk:
            raise RuntimeError('chunk-order activation or eligible multichunk path not recorded')
        if args.policy_axis == 'partition':
            path = partition_path_evidence(runtime_log, args.partition)
            save(out / 'partition-path-evidence.json', path)
            if not path['runtime_eligible']:
                raise RuntimeError('partition candidate runtime eligibility failed')
    except BaseException as error:
        failure = repr(error)
        (out / 'failure.txt').write_text(traceback.format_exc())
    finally:
        (out / 'memory-phase.txt').write_text('shutdown\n')
        if service is not None:
            try:
                service.terminate()
                service_code = service.wait(timeout=120)
                service.snapshot('after_main_exit')
            except BaseException as error:
                cleanup_errors.append('service termination: ' + repr(error))
                try:
                    service.kill()
                    service_code = service.wait(timeout=30)
                except BaseException as cleanup:
                    cleanup_errors.append('service kill: ' + repr(cleanup))
        # Observe natural target exit before controller-stop fallback.
        try:
            monitor_code = monitor.wait(timeout=15 if service else .1)
        except subprocess.TimeoutExpired:
            (memory / 'stop').touch()
            try:
                monitor_code = monitor.wait(timeout=30)
            except subprocess.TimeoutExpired:
                monitor.terminate()
                try:
                    monitor_code = monitor.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    monitor.kill(); monitor_code = monitor.wait(timeout=10)
        monitor_log.close()
        if service is not None:
            try:
                service.close()
            except BaseException as error:
                cleanup_errors.append('unit cleanup: ' + repr(error))
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)
        after_sha = sha(args.binary)
        expected_checks = {'business': 6, 'lifecycle': 9, 'all': 15}[args.mode]
        http_passed = (failure is None and runner is not None and
                       len(runner.records) == expected_checks)
        summary_path = memory / 'memory-peak.json'
        memory_summary = json.loads(summary_path.read_text()) if summary_path.is_file() else {}
        coverage_complete = (memory_summary.get('sampling_complete') is True and
                             memory_summary.get('prelaunch_sample_present') is True)
        passed = (http_passed and not cleanup_errors and service_code == 0 and
                  monitor_code == 0 and coverage_complete and
                  after_sha == args.expected_binary_sha256)
        save(out / 'exit.json', dict(passed=passed, failure=failure, cleanup_errors=cleanup_errors,
             service_exit=service_code, monitor_exit=monitor_code, binary_sha256_after=after_sha,
             checks_completed=len(runner.records) if runner else 0,
             expected_checks=expected_checks, http_checks_passed=http_passed,
             resource_sampling_complete=coverage_complete,
             monitor_stop_reason=memory_summary.get('stop_reason'),
             unit_removed=service.closed if service else False,
             reference_role=args.reference_role,
             historical_precheck_equal=(all(r['reference_equal'] for r in runner.business_results)
                                        if runner and runner.business_results and
                                        args.reference_role == 'historical_precheck' else None),
             physical_memory_total='INDETERMINATE', performance_acceptance=False,
             completed_utc=datetime.now(timezone.utc).isoformat()))
    return 0 if passed else 1


if __name__ == '__main__':
    sys.exit(main())
