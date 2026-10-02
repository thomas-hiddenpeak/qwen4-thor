"""One immutable HTTP run with explicit host/cache limits and cache evidence.

This explores resource behavior. A partial run never qualifies performance.
CUDA allocations are not fully charged by this Thor driver's memory cgroup.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from file_cache import model_files, observe_files
from monitor_memory import find_pids
from run_acceptance import LENGTHS, parse_lengths, performance_plan

ROOT = Path(__file__).resolve().parents[2]


def save(path, value):
    with path.open('x') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')


def clear_target_cache(paths):
    """Read-only descriptors, targeted eviction advice, no model writes."""
    errors = []
    for path in paths:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
            try:
                os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
            finally:
                os.close(fd)
        except OSError as error:
            errors.append({'path': str(path), 'error': str(error)})
    return errors


def payload_residual(observation):
    if not observation['complete_file_set_observed']:
        return None
    return sum(row['resident_bytes'] for row in observation['files']
               if Path(row['path']).suffix in ('.safetensors', '.bin'))


def selected_performance_plan(mode, lengths=None, repeats=3, target_total=0):
    """Keep the default bounded run; an explicit target tier emits 257 tokens."""
    if repeats != 3:
        raise ValueError('this frozen protocol requires exactly three repeats')
    if mode != 'performance':
        if lengths is not None or target_total:
            raise ValueError('performance selection requires performance mode')
        return None
    selected = parse_lengths(lengths if lengths is not None else '45056',
                             '--perf-lengths')
    if not set(selected).issubset(set(LENGTHS) | {261887}):
        raise ValueError('only the fixed five tiers and target 261887 are supported')
    target = 261887 in selected
    if target_total not in (0, 262144) or (target_total and not target):
        raise ValueError('target-total requires selected 261887 and total 262144')
    plan = performance_plan(selected, repeats, [261887] if target else [],
                            262144 if target else 0, 262144, True)
    plan['partial_offload_matrix'] = not plan['full_offload_matrix_requested']
    return plan


def experiment_environment(chunk_order, inherited):
    if chunk_order not in (0, 1):
        raise ValueError('chunk-order must be zero or one')
    env = {key: value for key, value in inherited.items()
           if not key.startswith('Q4T_')}
    env.update({'Q4T_MOE_L2_SLOTS': '16', 'Q4T_MOE_MIRROR_K': '8',
                'Q4T_MOE_MAX_OPEN_SHARDS': '200', 'Q4T_MOE_EVICT_WEIGHT': '0',
                'Q4T_MOE_PREAD_MERGE': '1', 'Q4T_MOE_INLINE_MISS_LIMIT': '1',
                'Q4T_MOE_CHUNK_ORDER': str(chunk_order)})
    return env


def runner_command(args, out, unit, plan):
    command = [sys.executable, str(ROOT / 'tools/evalscope/run_acceptance.py'),
               '--mode', args.mode, '--binary', str(args.binary.resolve()),
               '--model-dir', str(args.model_dir.resolve()),
               '--fixtures', str(args.fixtures.resolve()), '--output', str(out / 'http'),
               '--port', str(args.port), '--max-len', '262144',
               '--moe-resident-slots', '256', '--moe-hot-list', str(args.hot_list.resolve()),
               '--startup-timeout', '600', '--request-deadline-ms', '1800000',
               '--allow-unqualified-binary', '--systemd-unit', unit]
    if plan:
        command += ['--perf-lengths', ','.join(map(str, plan['lengths'])),
                    '--perf-repeats', str(plan['repeats'])]
        if 261887 in plan['lengths']:
            command += ['--extra-lengths', '261887', '--target-total', '262144']
    if args.reference:
        command += ['--reference', str(args.reference.resolve())]
    if args.host_cache_max_bytes:
        command += ['--host-cache-max-bytes', str(args.host_cache_max_bytes)]
    return command


def monitor_command(args, out, unit):
    mon, http = out / 'memory', out / 'http'
    return [sys.executable, str(ROOT / 'tools/evalscope/monitor_memory.py'),
            '--model-dir', str(args.model_dir.resolve()),
            '--pid-file', str(http / 'server.pid'),
            '--phase-file', str(http / 'memory-phase.txt'),
            '--cgroup-path', '/system.slice/' + unit,
            '--ready-file', str(mon / 'ready'), '--stop-file', str(mon / 'stop'),
            '--out', str(mon), '--interval', str(args.monitor_interval),
            '--gpu-interval', str(args.gpu_interval),
            '--file-cache-mode', args.file_cache_mode]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--output', required=True, type=Path)
    ap.add_argument('--binary', required=True, type=Path)
    ap.add_argument('--model-dir', required=True, type=Path)
    ap.add_argument('--hot-list', required=True, type=Path)
    ap.add_argument('--fixtures', required=True, type=Path)
    ap.add_argument('--mode', choices=('quality', 'performance'), required=True)
    ap.add_argument('--reference', type=Path)
    ap.add_argument('--host-cache-max-bytes', type=int)
    ap.add_argument('--clear-model-cache', action='store_true')
    ap.add_argument('--chunk-order', type=int, choices=(0, 1), default=0,
                    help='Explicit Q4T_MOE_CHUNK_ORDER; off and on use the same binary')
    ap.add_argument('--perf-lengths', help='CSV selection; default bounded 45056. '
                    '261887 automatically requests 257 output tokens.')
    ap.add_argument('--perf-repeats', type=int, default=3,
                    help='Frozen protocol requires exactly 3 requests per tier')
    ap.add_argument('--target-total', type=int, default=0,
                    help='Optional explicit 262144 check for target tier 261887')
    ap.add_argument('--monitor-interval', type=float, default=1)
    ap.add_argument('--gpu-interval', type=float, default=10)
    ap.add_argument('--file-cache-mode', choices=('every_sample', 'endpoints'),
                    default='endpoints')
    ap.add_argument('--runner-timeout-s', type=int, default=7200,
                    help='Overall bound; explicitly increase for a frozen full matrix')
    ap.add_argument('--port', type=int, default=8172)
    args = ap.parse_args()
    try:
        plan = selected_performance_plan(args.mode, args.perf_lengths,
                                         args.perf_repeats, args.target_total)
    except ValueError as error:
        ap.error(str(error))
    if (args.runner_timeout_s <= 0 or any(not math.isfinite(value) or value <= 0
            for value in (args.monitor_interval, args.gpu_interval))):
        ap.error('monitor intervals and runner timeout must be finite and positive')
    out = args.output.resolve()
    if not any(out.is_relative_to(ROOT / part) for part in ('build', '.q4t-work')):
        ap.error('output must be under build/ or .q4t-work/')
    if args.host_cache_max_bytes is not None and args.host_cache_max_bytes <= 0:
        ap.error('host/cache maximum must be positive')
    if not args.binary.is_file() or not os.access(args.binary, os.X_OK):
        ap.error('binary must exist and be executable')
    if not (args.binary.resolve().parent / 'CMakeCache.txt').is_file():
        ap.error('actual CMakeCache.txt must accompany binary')
    needed = ([args.fixtures / 'manifest.json', args.fixtures / 'requests.jsonl']
              if args.mode == 'quality' else
              [args.fixtures / f'context-{length}/requests.jsonl'
               for length in plan['lengths']])
    needed += [args.hot_list, args.model_dir / 'config.json',
               args.model_dir / 'model.safetensors.index.json']
    if args.reference:
        needed.append(args.reference)
    for path in needed:
        if not path.is_file() or not path.stat().st_size:
            ap.error('required evidence/config file unavailable: ' + str(path))
    if not isinstance(json.loads(args.hot_list.read_text()), dict):
        ap.error('hot list must be a JSON object')
    if find_pids('q4t'):
        ap.error('another q4t process is active')
    gpu = subprocess.run(['nvidia-smi', '--query-compute-apps=pid',
                          '--format=csv,noheader,nounits'],
                         capture_output=True, text=True, timeout=10)
    if gpu.returncode or gpu.stdout.strip():
        ap.error('GPU compute state unavailable or occupied')
    out.mkdir(parents=True, exist_ok=False)
    unit = f'q4t-ram-{time.time_ns()}-{os.getpid()}.service'
    env = experiment_environment(args.chunk_order, os.environ)
    paths = model_files(args.model_dir)
    tool_dir = out / 'tools'
    tool_dir.mkdir()
    tool_hashes = {}
    for name in ('run_budget_experiment.py', 'run_acceptance.py',
                 'isolated_service.py', 'monitor_memory.py', 'file_cache.py',
                 'resource_metrics.py', 'memory_accounting.py'):
        source = ROOT / 'tools/evalscope' / name
        shutil.copyfile(source, tool_dir / name)
        tool_hashes[name] = hashlib.sha256(source.read_bytes()).hexdigest()
    save(out / 'protocol.json', {
        'unit': unit, 'mode': args.mode, 'binary': str(args.binary.resolve()),
        'tool_sha256': tool_hashes,
        'fixtures': str(args.fixtures.resolve()),
        'fixture_sha256': {str(path.resolve().relative_to(args.fixtures.resolve())):
                            hashlib.sha256(path.read_bytes()).hexdigest()
                           for path in needed
                           if path.resolve().is_relative_to(args.fixtures.resolve())},
        'binary_sha256': hashlib.sha256(args.binary.read_bytes()).hexdigest(),
        'input_config_sha256': {str(path.resolve()): hashlib.sha256(path.read_bytes()).hexdigest()
                                for path in needed},
        'host_cache_max_bytes': args.host_cache_max_bytes, 'swap_max_bytes': 0,
        'chunk_order': args.chunk_order,
        'monitor': {'interval_seconds': args.monitor_interval,
                    'gpu_interval_seconds': args.gpu_interval,
                    'file_cache_mode': args.file_cache_mode,
                    'live_file_cache_observation': ('NOT_SAMPLED' if
                        args.file_cache_mode == 'endpoints' else 'PER_SAMPLE')},
        'total_physical_RAM_limit': None,
        'cache_protocol': 'targeted cold start; no generated warmup; no clearing '
                          'between requests' if args.clear_model_cache else
                          'inherited cache; not a cold or constrained-cache claim',
        'cold_payload_max_resident_bytes': 0,
        'lengths': plan['lengths'] if plan else None,
        'repeats': plan['repeats'] if plan else None,
        'output_tokens': (256 if all(r['max_tokens'] == 256 for r in plan['requests'])
                          else None) if plan else 32,
        'output_tokens_by_length': ({str(r['input_tokens']): r['max_tokens']
                                    for r in plan['requests']} if plan else None),
        'performance_plan': plan,
        'max_len': 262144, 'max_seq': 1, 'max_prefill': 8192,
        'startup_timeout_s': 600, 'request_deadline_ms': 1800000,
        'runner_timeout_s': args.runner_timeout_s,
        'client_lifecycle': 'separate evalscope client per request in bounded mode',
        'effective_environment': {key: value for key, value in env.items()
                                  if key.startswith('Q4T_')},
        'model_files': [{'path': str(path), 'size': path.stat().st_size,
                         'inode': path.stat().st_ino, 'device': path.stat().st_dev,
                         'mtime_ns': path.stat().st_mtime_ns}
                        for path in paths],
    })
    save(out / 'cache-before.json', observe_files(paths))
    if args.clear_model_cache:
        errors = clear_target_cache(paths)
        after = observe_files(paths)
        save(out / 'cache-after-advice.json', after)
        residual = payload_residual(after)
        gate = {'advice_errors': errors, 'payload_resident_bytes': residual,
                'cold_payload_established': not errors and residual == 0,
                'metadata_caches_not_exclusively_charged': True}
        save(out / 'cache-gate.json', gate)
        if not gate['cold_payload_established']:
            save(out / 'wrapper-exit.json', {
                'runner_rc': None, 'monitor_rc': None,
                'failure': 'cold payload gate failed; no service started',
                'cleanup_failed': False, 'performance_qualification': False,
                'performance_scope': plan['scope'] if plan else None,
                'partial_offload_matrix': plan['partial_offload_matrix'] if plan else None,
                'full_offload_matrix_completed': False,
            })
            raise RuntimeError('cold payload gate failed; evidence retained, no retry')
    http, mon = out / 'http', out / 'memory'
    mon.mkdir()
    command = runner_command(args, out, unit, plan)
    save(out / 'runner-command.json', command)
    monitor_argv = monitor_command(args, out, unit)
    save(out / 'monitor-command.json', monitor_argv)
    runner_rc = monitor_rc = None
    failure = None
    cleanup_failed = False
    started = time.time()
    with (mon / 'monitor.log').open('x') as monitor_log:
        monitor = subprocess.Popen(monitor_argv, stdout=monitor_log,
            stderr=subprocess.STDOUT, cwd=ROOT)
        try:
            for _ in range(100):
                if (mon / 'ready').exists():
                    break
                if monitor.poll() is not None:
                    raise RuntimeError('monitor exited before ready')
                time.sleep(0.1)
            else:
                raise RuntimeError('monitor not ready')
            with (out / 'runner.log').open('x') as log:
                runner_rc = subprocess.run(command, cwd=ROOT, env=env,
                                           stdout=log, stderr=subprocess.STDOUT,
                                           timeout=args.runner_timeout_s).returncode
        except BaseException as error:
            failure = f'{type(error).__name__}: {error}'
            raise
        finally:
            # The service's final cgroup counters are also saved by the
            # runner before its unit disappears. Never stop unrelated units.
            from isolated_service import unit_properties
            try:
                props = unit_properties(unit)
                if props.get('LoadState') != 'not-found':
                    subprocess.run(['sudo', '-n', 'systemctl', 'stop', unit],
                                   capture_output=True, timeout=40)
                    subprocess.run(['sudo', '-n', 'systemctl', 'reset-failed', unit],
                                   capture_output=True, timeout=15)
                props = unit_properties(unit)
                cleanup_failed = props.get('LoadState') != 'not-found'
            except Exception as error:
                props = {'unknown': str(error)}
                cleanup_failed = True
            try:
                monitor_rc = monitor.wait(timeout=15)
            except subprocess.TimeoutExpired:
                (mon / 'stop').touch()
                try:
                    monitor_rc = monitor.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    monitor.terminate()
                    monitor_rc = monitor.wait(timeout=5)
            runner_exit = None
            if (http / 'exit.json').is_file():
                try:
                    runner_exit = json.loads((http / 'exit.json').read_text())
                except (ValueError, OSError) as error:
                    failure = failure or 'runner exit evidence unreadable: ' + str(error)
            if (runner_rc == 0 and (not runner_exit or
                    runner_exit.get('http_output_checks_passed') is not True)):
                failure = failure or 'runner exit evidence missing or failed'
                runner_rc = 1
            clean = runner_rc == monitor_rc == 0 and not cleanup_failed and not failure
            save(out / 'wrapper-exit.json', {
                'runner_rc': runner_rc, 'monitor_rc': monitor_rc, 'failure': failure,
                'started_t': started, 'ended_t': time.time(),
                'unit_after_cleanup': props, 'cleanup_failed': cleanup_failed,
                'performance_qualification': False,
                'performance_scope': plan['scope'] if plan else None,
                'partial_performance_matrix': plan['partial'] if plan else None,
                'partial_offload_matrix': plan['partial_offload_matrix'] if plan else None,
                'full_offload_matrix_completed': bool(clean and plan and
                    plan['full_offload_matrix_requested'] and runner_exit and
                    runner_exit.get('full_offload_matrix_completed') is True),
            })
    return runner_rc or monitor_rc or int(cleanup_failed)


if __name__ == '__main__':
    raise SystemExit(main())
