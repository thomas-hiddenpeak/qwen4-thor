"""Execute ONLY a separately frozen nine-T4 plus one-sequential HTTP manifest.

Reuses existing HTTP/SSE and health/metrics contracts, with no warmup,
tokenization probe, model probe or resample.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import sys
import time


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


def group_members(group):
    """Inspect the private process group without relying on a stale PID file."""
    members = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            fields = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
        except (FileNotFoundError, ProcessLookupError):
            continue
        if int(fields[2]) == group:
            members.append({'pid': int(entry.name), 'state': fields[0],
                            'start_ticks': int(fields[19])})
    return sorted(members, key=lambda row: row['pid'])


def cleanup_server(server):
    """Always return cleanup evidence, including timeout and residual failures."""
    evidence = {'pid': None if server is None else server.pid,
                'process_group': None if server is None else server.pid,
                'forced': False, 'errors': [], 'remaining': [], 'exit': None}
    if server is None:
        return evidence

    def send(sig):
        try:
            os.killpg(server.pid, sig)
        except ProcessLookupError:
            pass
        except BaseException as error:
            evidence['errors'].append(repr(error))

    def wait(seconds):
        deadline = time.monotonic() + seconds
        while True:
            server.poll()  # Reap the leader before checking /proc for it.
            evidence['remaining'] = group_members(server.pid)
            if server.returncode is not None and not evidence['remaining']:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(min(.05, max(0, deadline - time.monotonic())))

    try:
        send(signal.SIGTERM)
        if not wait(60):
            evidence['forced'] = True
            send(signal.SIGKILL)
            if not wait(10):
                evidence['errors'].append('process group survived SIGKILL')
    except BaseException as error:
        evidence['errors'].append(repr(error))
        evidence['forced'] = True
        send(signal.SIGKILL)
        try:
            server.wait(timeout=10)
            evidence['remaining'] = group_members(server.pid)
        except BaseException as cleanup_error:
            evidence['errors'].append(repr(cleanup_error))
    evidence['exit'] = server.returncode
    evidence['passed'] = (not evidence['forced'] and not evidence['errors'] and
                          not evidence['remaining'] and server.returncode == 0)
    return evidence


def binding_paths(manifest):
    bindings = manifest['bindings']
    require(isinstance(bindings, dict), 'bindings must be uniquely named')
    required = {
        'driver', 'fault_contract', 'wrapper', 'position_test', 'link_config',
        'transport', 'failure_contract', 'recovery_contract', 'acceptance_mode',
        'response_identity', 'variant_binary', 'production_binary', 'fixture',
        'sequential_reference', 'source_command', 'checkpoint_metadata',
        'build_manifest', 'position_helper', 'mtp_header', 'mtp_source',
        'model_header', 'model_source', 'scheduler_source', 'generation_source'}
    require(required <= set(bindings), 'missing required identity binding')
    paths = {}
    for name, binding in bindings.items():
        path = Path(binding['path']).resolve()
        require(path.is_file() and path.stat().st_size == binding['bytes'] and
                sha(path) == binding['sha256'], 'identity changed: ' + name)
        paths[name] = path
    require(paths['driver'] == Path(__file__).resolve(), 'wrong frozen driver')
    return paths


def validate_command(command, binary, verifier):
    require(isinstance(command, list) and len(command) >= 2 and
            command[0] == str(binary) and command[1] == 'serve',
            'wrong executable/command')
    values = {'--host': '127.0.0.1', '--max-seq': '1',
              '--max-len': '208896', '--max-prefill': '8192',
              '--max-tokens': '256', '--mtp-verifier': verifier}
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


def run_group(root, output, command, verifier, plan, prompt, environment,
              startup_seconds, transport, recovery, fault, acceptance,
              reference=None):
    output.mkdir(exist_ok=False)
    port = validate_command(command, Path(command[0]), verifier)
    save(output / 'server-command.json', {'argv': command})
    records = []
    failure = validation = server = None
    cleanup = None
    log_path = output / 'server.log'
    try:
        with log_path.open('x') as log:
            server = subprocess.Popen(command, cwd=root, env=environment,
                                      stdout=log, stderr=subprocess.STDOUT,
                                      start_new_session=True)
            save(output / 'server-process.json', {
                'pid': server.pid, 'process_group': os.getpgid(server.pid)})
            require(os.getpgid(server.pid) == server.pid,
                    'server lacks its own process group')
            end = time.monotonic() + startup_seconds
            while True:
                require(server.poll() is None, 'server exited at startup')
                text = transport.read_log(log_path)
                if 'serving on port' in text:
                    break
                require(time.monotonic() < end, 'startup deadline exceeded')
                time.sleep(min(.25, max(0, end - time.monotonic())))
            if verifier == 't4':
                fault.require_variant(text)
            else:
                require('[q4t][t4_fault_' not in text and
                        '[q4t][fault_' not in text,
                        'sequential check used an injected binary')
            startup = acceptance.startup_evidence(text, True, verifier)
            save(output / 'startup-evidence.json', startup)
            require(startup['passed'], 'startup mode/capacity mismatch')
            before = previous = transport.snapshot(port, output, 'before')
            control = None
            for label, kind in plan:
                require(server.poll() is None, 'server exited between requests')
                record = {'request_id': label, 'kind': kind, 'passed': False}
                records.append(record)
                body = {'model': 'qwen3.8-flash-next', 'prompt': prompt,
                        'max_tokens': 256, 'temperature': 0, 'seed': 20260920,
                        'stream': True, 'stream_options': {'include_usage': True},
                        'request_id': label, 'cancel_token': secrets.token_hex(32)}
                response = transport.request(port, output, label,
                                             '/v1/chat/completions', body)
                if kind == 'success':
                    # This requires exactly 1024+256 tokens and finish=length.
                    # A natural early EOS is coverage failure, never resampled.
                    result = recovery.completed_result(response, label)
                    if control is None:
                        control = result
                    else:
                        recovery.require_recovery(result, control)
                    if reference is not None:
                        expected_usage = {
                            'prompt_tokens': reference['actual_input'],
                            'completion_tokens': reference['actual_output'],
                            'total_tokens': reference['actual_input'] +
                                            reference['actual_output']}
                        require(result['text'] == reference['text'] and
                                result['finish'] == reference['finish'] and
                                result['usage'] == [expected_usage],
                                'sequential response differs from frozen M1')
                        if reference['observed_usage_messages']:
                            require(result['usage'] ==
                                    reference['observed_usage_messages'],
                                    'observed historical raw usage differs')
                        save(output / 'reference-comparison.json', {
                            'text_equal': True, 'finish_equal': True,
                            'token_counts_equal': True,
                            'historical_usage_source': reference['usage_source'],
                            'historical_raw_usage_observed': bool(
                                reference['observed_usage_messages']),
                            'new_raw_usage_observed': result['usage'],
                            'total_tokens_in_reference_derived_not_raw': True})
                else:
                    result = fault.failed_result(response, label)
                save(output / (label + '-result.json'), result)
                current = transport.snapshot(port, output, label + '-after')
                fault.require_counter_delta(previous, current, kind == 'success')
                previous = current
                record['passed'] = True
            successes = sum(kind == 'success' for _, kind in plan)
            require(tuple(b - a for a, b in zip(before, previous)) ==
                    (len(plan), successes, len(plan) - successes, 0),
                    'wrong total request counters')
    except BaseException as error:
        failure = repr(error)
    finally:
        cleanup = cleanup_server(server)
        save(output / 'cleanup.json', cleanup)
        if not cleanup.get('passed', False):
            failure = failure or 'server cleanup failed: ' + repr(cleanup)
        if failure is None:
            try:
                text = transport.read_log(log_path)
                if verifier == 't4':
                    validation = fault.validate_log(text)
                else:
                    paths = recovery.terminal_paths(text)
                    require(list(paths) == [plan[0][0]],
                            'extra/missing sequential generation path')
                    path = paths[plan[0][0]]
                    require(path.get('requested_mtp') == '1' and
                            path.get('path') == 'mtp_sequential_b1' and
                            path.get('fallback') == 'none' and
                            not acceptance.sequential_success_errors(
                                path, 256, ['length']),
                            'shared-path check did not run real sequential MTP')
                    validation = {'passed': True, 'paths': paths}
                save(output / 'request-evidence.json', validation)
            except BaseException as error:
                failure = repr(error)
        summary = {'passed': failure is None and len(records) == len(plan) and
                              all(row['passed'] for row in records) and
                              validation is not None,
                   'records': records, 'failure': failure,
                   'server_exit': None if server is None else server.returncode,
                   'forced_cleanup': cleanup['forced'], 'cleanup': cleanup,
                   'verifier': verifier,
                   'production_binary_http': verifier == 'sequential'}
        save(output / 'summary.json', summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    require(manifest.get('status') == 'frozen' and
            manifest.get('protocol') == 't4_completion_v1' and
            manifest.get('generation_requests') == 10 and
            manifest.get('plan') == [list(row) for row in (
                ('t4-control', 'success'),
                ('t4-upload', 'verify_sequence_upload'),
                ('t4-upload-recovery', 'success'),
                ('t4-verify', 'verify_return'),
                ('t4-verify-recovery', 'success'),
                ('t4-restore', 'natural_restore'),
                ('t4-restore-recovery', 'success'),
                ('t4-extend', 'extend_attention'),
                ('t4-extend-recovery', 'success'))], 'protocol is not frozen')
    paths = binding_paths(manifest)
    root = Path(manifest['root']).resolve()
    output = Path(manifest['output']).resolve()
    require(any(output.is_relative_to(root / name)
                for name in ('build', '.q4t-work')), 'output outside build/work')
    require(0 < manifest['startup_seconds'] <= 300, 'unbounded startup')
    fixture = [json.loads(line) for line in paths['fixture'].read_text().splitlines()]
    require(len(fixture) == 1 and isinstance(fixture[0]['prompt'], str),
            'fixture must contain exactly the existing one prompt')
    prompt = fixture[0]['prompt']
    require(hashlib.sha256(prompt.encode()).hexdigest() ==
            'a72fade2611451e1f33ca088f0554474ec9d9032efccf04cc5581a2c25be2d98',
            'prompt differs from the frozen M1 1K fixture')
    reference = json.loads(paths['sequential_reference'].read_text())
    require(len(reference) == 1 and reference[0]['success'] and
            reference[0]['actual_input'] == 1024 and
            reference[0]['actual_output'] == 256 and
            reference[0]['finish'] == ['length'] and
            reference[0]['prompt_sha256'] == hashlib.sha256(prompt.encode()).hexdigest(),
            'reference is not the frozen M1 sequential 1K/256 success')
    require(reference[0]['mode'] in ('default', 'explicit'),
            'reference is not an M1 sequential mode')
    validate_command(manifest['variant_argv'], paths['variant_binary'], 't4')
    validate_command(manifest['sequential_argv'], paths['production_binary'],
                     'sequential')
    checkpoint = Path(manifest['checkpoint_directory']).resolve()
    require(checkpoint.is_dir(), 'checkpoint directory is missing')
    for command in (manifest['variant_argv'], manifest['sequential_argv']):
        require(Path(command[command.index('--model-dir') + 1]).resolve() ==
                checkpoint, 'command checkpoint differs from frozen identity')
    output.mkdir(parents=True, exist_ok=False)
    try:
        run_frozen(args, manifest, paths, root, output, prompt, reference)
    except BaseException as error:
        # Pre-launch archive/import errors still leave an explicit failed
        # artifact; a completed group summary is never overwritten.
        if not (output / 'summary.json').exists():
            save(output / 'summary.json', {
                'passed': False, 'failure': repr(error),
                'generation_requests_maximum': 10,
                'scope': 'frozen driver failure; inspect preserved raw groups'})
        raise


def run_frozen(args, manifest, paths, root, output, prompt, reference):
    shutil.copy2(args.manifest, output / 'frozen-manifest.json')
    sources = output / 'frozen-sources'
    sources.mkdir()
    for name in ('driver', 'fault_contract', 'wrapper', 'position_test',
                 'link_config', 'transport', 'failure_contract',
                 'recovery_contract', 'acceptance_mode', 'response_identity'):
        shutil.copy2(paths[name], sources / paths[name].name)
    # Import only the bound and archived harness modules. No .pyc production
    # edits occur even if this script was launched without -B.
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(sources))
    import run_mtp_failure_recovery as transport
    import request_cancellation_contract as recovery
    import mtp_t4_completion_contract as fault
    import acceptance_mode as acceptance
    import mtp_failure_recovery_contract as failure_contract
    import response_identity
    for module in (transport, recovery, fault, acceptance, failure_contract,
                   response_identity):
        require(Path(module.__file__).resolve() ==
                sources / Path(module.__file__).name,
                'harness import escaped frozen-sources: ' + module.__name__)
    # The archived transport's standalone main() derives a different ROOT.
    # This driver calls only its path-explicit request/snapshot/read_log API;
    # bind ROOT as well so no future helper can inherit the archive directory.
    transport.ROOT = root
    require(manifest['plan'] == [list(row) for row in fault.PLAN],
            'driver/contract plans differ')
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
        'generation_requests_maximum': 10,
        'checkpoint_payload_attested': False})
    groups = []
    variant = run_group(root, output / 't4-variant', manifest['variant_argv'],
                        't4', fault.PLAN, prompt, environment,
                        manifest['startup_seconds'], transport, recovery, fault,
                        acceptance)
    groups.append(variant)
    if variant['passed']:
        groups.append(run_group(
            root, output / 'sequential', manifest['sequential_argv'],
            'sequential', (('completion-sequential', 'success'),), prompt,
            environment, manifest['startup_seconds'], transport, recovery,
            fault, acceptance, reference[0]))
    passed = len(groups) == 2 and all(group['passed'] for group in groups)
    save(output / 'summary.json', {
        'passed': passed, 'groups': groups, 'generation_requests_maximum': 10,
        'scope': 'completion/position integration and logical T4 failure '
                 'recovery; not full T4 numerical admission or performance'})
    require(passed, 'frozen group failed; preserve outputs, do not resample')


if __name__ == '__main__':
    main()
