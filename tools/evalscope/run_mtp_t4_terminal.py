"""Execute a separately frozen nine-request T4 terminal-control protocol."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time

OWNED_OUTPUT = None


def sha(path):
    with Path(path).open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def save(path, value):
    with path.open('x', encoding='utf-8') as target:
        json.dump(value, target, indent=2, ensure_ascii=False)
        target.write('\n')


def binding_paths(manifest):
    bindings = manifest['bindings']
    require(isinstance(bindings, dict), 'bindings must be uniquely named')
    required = {
        'driver', 'terminal_contract', 'contract_test', 'wrapper', 'link_config',
        'transport', 'failure_contract', 'recovery_contract', 'acceptance_mode',
        'response_identity', 'variant_binary', 'fixture', 'build_manifest',
        'mtp_header', 'mtp_source', 'model_header', 'model_source',
        'scheduler_source', 'generation_source', 'policy_header',
        'checkpoint_config', 'checkpoint_generation_config'}
    require(required <= set(bindings), 'missing required identity bindings')
    paths = {}
    for name, binding in bindings.items():
        path = Path(binding['path']).resolve()
        require(path.is_file() and path.stat().st_size == binding['bytes'] and
                sha(path) == binding['sha256'], 'identity changed: ' + name)
        paths[name] = path
    require(paths['driver'] == Path(__file__).resolve(), 'wrong frozen driver')
    return paths


def validate_command(command, binary):
    require(isinstance(command, list) and len(command) >= 2 and
            command[0] == str(binary) and command[1] == 'serve',
            'wrong executable/command')
    values = {'--host': '127.0.0.1', '--max-seq': '1',
              '--max-len': '208896', '--max-prefill': '8192',
              '--max-tokens': '256', '--mtp-verifier': 't4'}
    for flag, expected in values.items():
        require(command.count(flag) == 1 and
                command.index(flag) + 1 < len(command) and
                command[command.index(flag) + 1] == expected and
                not any(item.startswith(flag + '=') for item in command),
                'wrong frozen option: ' + flag)
    require(command.count('--mtp') == 1 and '--no-mtp' not in command and
            command.count('--port') == 1 and command.count('--model-dir') == 1,
            'ambiguous MTP/port/checkpoint configuration')
    port = int(command[command.index('--port') + 1])
    require(1 <= port <= 65535, 'invalid port')
    return port


def snapshot(port, output, label, deadline, transport, counters):
    """Same health/cleanup contract, bounded by the protocol deadline too."""
    end = min(deadline, time.monotonic() + 120)
    attempt = 0
    while True:
        remaining = end - time.monotonic()
        require(remaining > 0, 'cleanup/overall request deadline exceeded')
        tag = f'{label}-{attempt:04d}'
        health = transport.request(port, output, tag + '-health', '/healthz',
                                   timeout=min(10, remaining))
        require(health['status'] == 200, 'health endpoint failed')
        fields = json.loads(health['body'])
        require(fields.get('gpu_healthy') is True and
                fields.get('seq_slots_total') == 1,
                'unhealthy GPU or incorrect capacity')
        remaining = end - time.monotonic()
        require(remaining > 0, 'cleanup/overall request deadline exceeded')
        metrics = transport.request(port, output, tag + '-metrics', '/metrics',
                                    timeout=min(10, remaining))
        require(metrics['status'] == 200, 'metrics endpoint failed')
        gauges = {}
        for name in ('q4t_active_chats', 'q4t_request_body_bytes',
                     'q4t_seq_slots_free', 'q4t_gpu_healthy'):
            found = re.findall(r'^' + name + r' ([0-9]+)$', metrics['body'],
                               re.MULTILINE)
            require(len(found) == 1, 'missing/duplicate cleanup gauge')
            gauges[name] = int(found[0])
        if (fields.get('seq_slots_free') == 1 and
                gauges == {'q4t_active_chats': 0, 'q4t_request_body_bytes': 0,
                           'q4t_seq_slots_free': 1, 'q4t_gpu_healthy': 1}):
            values = counters.counters(metrics['body'])
            save(output / (label + '-snapshot.json'), {
                'health': fields, 'gauges': gauges,
                'counters': dict(zip(counters.COUNTERS, values)),
                'attempt': attempt})
            return values
        attempt += 1
        time.sleep(min(.05, max(0, end - time.monotonic())))


def main():
    global OWNED_OUTPUT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    require(manifest.get('status') == 'frozen' and
            manifest.get('protocol') == 't4_terminal_v1' and
            manifest.get('generation_requests') == 9,
            'protocol must be separately frozen')
    root = Path(manifest['root']).resolve()
    output = Path(manifest['output']).resolve()
    require(any(output.is_relative_to(root / name)
                for name in ('build', '.q4t-work')), 'output outside build/work')
    output.mkdir(parents=True, exist_ok=False)
    OWNED_OUTPUT = output
    shutil.copy2(args.manifest, output / 'frozen-manifest.json')
    paths = binding_paths(manifest)
    require(type(manifest['startup_seconds']) is int and
            0 < manifest['startup_seconds'] <= 300, 'unbounded startup')
    require(manifest.get('request_group_seconds') == 1500,
            'wrong overall request deadline')
    fixture = [json.loads(line) for line in paths['fixture'].read_text().splitlines()]
    require(len(fixture) == 1 and isinstance(fixture[0]['prompt'], str),
            'fixture must contain exactly the existing one prompt')
    prompt = fixture[0]['prompt']
    require(hashlib.sha256(prompt.encode()).hexdigest() ==
            'a72fade2611451e1f33ca088f0554474ec9d9032efccf04cc5581a2c25be2d98',
            'prompt differs from frozen M1 1K fixture')
    command = manifest['variant_argv']
    port = validate_command(command, paths['variant_binary'])
    checkpoint = Path(manifest['checkpoint_directory']).resolve()
    require(checkpoint.is_dir() and
            Path(command[command.index('--model-dir') + 1]).resolve() ==
            checkpoint and paths['checkpoint_config'].parent == checkpoint and
            paths['checkpoint_generation_config'].parent == checkpoint,
            'checkpoint differs from metadata identity')
    sources = output / 'frozen-sources'
    sources.mkdir()
    for name in ('driver', 'terminal_contract', 'contract_test', 'wrapper',
                 'link_config', 'transport', 'failure_contract',
                 'recovery_contract', 'acceptance_mode', 'response_identity'):
        shutil.copy2(paths[name], sources / paths[name].name)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(sources))
    import run_mtp_failure_recovery as transport
    import mtp_failure_recovery_contract as counters
    import mtp_t4_terminal_contract as terminal
    import acceptance_mode as acceptance
    for module_name in ('run_mtp_failure_recovery',
                        'mtp_failure_recovery_contract',
                        'request_cancellation_contract',
                        'mtp_t4_terminal_contract', 'acceptance_mode',
                        'response_identity'):
        module_path = Path(sys.modules[module_name].__file__).resolve()
        require(module_path == sources / (module_name + '.py'),
                'import escaped bound source archive: ' + module_name)
    # The transport APIs used here receive explicit paths. Bind its historical
    # module-level root as well, so future helper additions cannot infer the
    # repository from the archived copy's temporary directory depth.
    transport.ROOT = root
    require(manifest.get('plan') == [list(row) for row in terminal.PLAN],
            'driver and frozen request plan differ')
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith('Q4T_') and key != 'LD_PRELOAD'}
    temporary = output / 'tmp'
    temporary.mkdir()
    environment.update(PYTHONDONTWRITEBYTECODE='1', TMPDIR=str(temporary))
    save(output / 'launch.json', {
        'manifest_sha256': sha(args.manifest),
        'removed_environment_names': sorted(set(os.environ) - set(environment)),
        'effective_runtime_environment': {
            key: value for key, value in environment.items() if key in {
                'PATH', 'LD_LIBRARY_PATH', 'CUDA_VISIBLE_DEVICES',
                'CUDA_DEVICE_ORDER', 'CUDA_MODULE_LOADING', 'OMP_NUM_THREADS',
                'PYTHONDONTWRITEBYTECODE', 'TMPDIR'}},
        'other_inherited_environment_names': sorted(key for key in environment
                                                   if key not in {
            'PATH', 'LD_LIBRARY_PATH', 'CUDA_VISIBLE_DEVICES',
            'CUDA_DEVICE_ORDER', 'CUDA_MODULE_LOADING', 'OMP_NUM_THREADS',
            'PYTHONDONTWRITEBYTECODE', 'TMPDIR'}),
        'generation_requests_maximum': 9,
        'request_group_seconds': 1500,
        'checkpoint_payload_attested': False})
    save(output / 'server-command.json', {'argv': command})
    records = []
    failure = validation = server = None
    forced = False
    cleanup_failures = []
    remaining_process_group = None
    log_path = output / 'server.log'
    started = time.monotonic()
    deadline = started + manifest['request_group_seconds']
    try:
        with log_path.open('x') as log:
            server = subprocess.Popen(command, cwd=root, env=environment,
                                      stdout=log, stderr=subprocess.STDOUT,
                                      start_new_session=True)
            save(output / 'server-process.json', {'pid': server.pid,
                                                'pgid': server.pid})
            end = min(deadline, time.monotonic() + manifest['startup_seconds'])
            while True:
                require(server.poll() is None, 'server exited at startup')
                text = transport.read_log(log_path)
                if 'serving on port' in text:
                    break
                require(time.monotonic() < end, 'startup deadline exceeded')
                time.sleep(min(.25, max(0, end - time.monotonic())))
            terminal.require_variant(text)
            startup = acceptance.startup_evidence(text, True, 't4')
            save(output / 'startup-evidence.json', startup)
            require(startup['passed'], 'startup mode/capacity mismatch')
            before = previous = snapshot(port, output, 'before', deadline,
                                         transport, counters)
            for label, stop_row, cap, stream in terminal.PLAN:
                require(server.poll() is None, 'server exited between requests')
                body = {'model': 'qwen3.8-flash-next', 'prompt': prompt,
                        'max_tokens': cap, 'temperature': 0, 'seed': 20260920,
                        'stream': stream, 'request_id': label,
                        'cancel_token': secrets.token_hex(32)}
                if stream:
                    body['stream_options'] = {'include_usage': True}
                remaining = deadline - time.monotonic()
                require(remaining > 0, 'overall request deadline exceeded')
                response = transport.request(
                    port, output, label, '/v1/chat/completions', body,
                    timeout=min(120, remaining))
                result = terminal.completed_result(response, label, stop_row,
                                                   cap, stream)
                save(output / (label + '-result.json'), result)
                current = snapshot(port, output, label + '-after', deadline,
                                   transport, counters)
                counters.require_counter_delta(previous, current, True)
                previous = current
                records.append({'request_id': label, 'stop_row': stop_row,
                                'max_tokens': cap, 'stream': stream,
                                'passed': True})
            require(tuple(b - a for a, b in zip(before, previous)) ==
                    (9, 9, 0, 0), 'wrong total counters')
    except BaseException as error:
        failure = repr(error)
    finally:
        if server is not None:
            try:
                if server.poll() is None:
                    server.terminate()
                    try:
                        server.wait(timeout=60)
                    except subprocess.TimeoutExpired:
                        forced = True
                        os.killpg(server.pid, signal.SIGKILL)
                        server.wait(timeout=10)
                        failure = failure or 'server required forced termination'
            except BaseException as error:
                cleanup_failures.append(repr(error))
            try:
                os.killpg(server.pid, 0)
                remaining_process_group = True
                forced = True
                os.killpg(server.pid, signal.SIGKILL)
                try:
                    server.wait(timeout=10)
                except BaseException as error:
                    cleanup_failures.append(repr(error))
            except ProcessLookupError:
                remaining_process_group = False
            except BaseException as error:
                cleanup_failures.append(repr(error))
            if cleanup_failures or remaining_process_group:
                failure = failure or 'process cleanup did not complete normally'
        if server is not None and server.returncode != 0:
            failure = failure or f'server exit {server.returncode}'
        if failure is None:
            try:
                validation = terminal.validate_log(transport.read_log(log_path))
                save(output / 'request-evidence.json', validation)
            except BaseException as error:
                failure = repr(error)
        summary = {'passed': failure is None and len(records) == 9 and
                              validation is not None,
                   'records': records, 'failure': failure,
                   'server_exit': None if server is None else server.returncode,
                   'forced_cleanup': forced,
                   'cleanup_failures': cleanup_failures,
                   'process_group_present_after_normal_cleanup':
                       remaining_process_group,
                   'elapsed_seconds': time.monotonic() - started,
                   'generation_requests_maximum': 9,
                   'production_binary_http': False,
                   'scope': 'real T4 plus controlled host selection and '
                            'ordinary tail; not natural quality or numerical '
                            'admission and not performance acceptance'}
        save(output / 'summary.json', summary)
    require(summary['passed'], 'group failed; preserve outputs, no resampling')


if __name__ == '__main__':
    try:
        main()
    except BaseException as error:
        # Preserve setup/import/identity failures too once this invocation
        # owns a new output directory. Never overwrite an earlier summary.
        if OWNED_OUTPUT is not None and not (
                OWNED_OUTPUT / 'summary.json').exists():
            save(OWNED_OUTPUT / 'summary.json', {
                'passed': False, 'failure': repr(error),
                'phase': 'unhandled_setup_or_finalization',
                'production_binary_http': False})
        raise
