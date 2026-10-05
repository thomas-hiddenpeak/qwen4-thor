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
import signal
import subprocess
import sys
import time

from file_cache import model_files, observe_files
from monitor_memory import find_pids
from offload_policy import (REQUEST_AXES, RUN_AXES, DIAGNOSTIC_SCOPE, RUN_TOOLS,
                            policy_environment)
from run_acceptance import LENGTHS, parse_lengths, performance_plan

ROOT = Path(__file__).resolve().parents[2]


def parse_binary_switch(value):
    if value not in ('0', '1'):
        raise argparse.ArgumentTypeError('must be exactly 0 or 1')
    return int(value)


def save(path, value):
    with path.open('x') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')


def process_group_state(pgid):
    """Observe only the newly owned group; zombies cannot issue more work."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return {'live_pids': [], 'zombie_pids': [], 'errors': [], 'absent': True}
    except OSError as error:
        return {'live_pids': [], 'zombie_pids': [], 'errors': [str(error)],
                'absent': None}
    live, zombies, errors = [], [], []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            fields = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
            if int(fields[2]) == pgid:
                (zombies if fields[0] in ('Z', 'X') else live).append(int(entry.name))
        except FileNotFoundError:
            continue
        except (OSError, ValueError, IndexError) as error:
            errors.append({'pid': entry.name, 'error': str(error)})
    return {'live_pids': sorted(live), 'zombie_pids': sorted(zombies),
            'errors': errors, 'absent': False}


def run_owned_runner(command, *, cwd, env, log, timeout, evidence,
                     term_grace=5, kill_grace=5):
    """Bound the runner and its evalscope descendants, separately from q4t.

    q4t belongs to its independently owned systemd unit. Its exact-unit cleanup
    remains the caller's responsibility. Never signal the wrapper's own group.
    """
    if evidence.exists():
        raise FileExistsError('runner process-group evidence already exists')
    proc = None
    failure = None
    record = {'started_t': time.time(), 'timeout_s': timeout, 'signals': [],
              'runner_pid': None, 'pgid': None, 'runner_reaped': False,
              'cleanup_complete': False}
    try:
        proc = subprocess.Popen(command, cwd=cwd, env=env, stdout=log,
                                stderr=subprocess.STDOUT, start_new_session=True)
        record.update(runner_pid=proc.pid, pgid=proc.pid)
        returncode = proc.wait(timeout=timeout)
    except BaseException as error:
        failure = f'{type(error).__name__}: {error}'
        raise
    finally:
        record['failure'] = failure
        if proc is not None:
            pgid = proc.pid
            if pgid == os.getpgrp():
                raise RuntimeError('refusing to clean the wrapper process group')
            state = process_group_state(pgid)
            record['before_cleanup'] = state
            unexpected_children = failure is None and bool(state['live_pids'])
            for sig, grace in ((signal.SIGTERM, term_grace),
                               (signal.SIGKILL, kill_grace)):
                if not state['live_pids'] and not state['errors']:
                    break
                try:
                    os.killpg(pgid, sig)
                    record['signals'].append({'signal': sig.name, 't': time.time()})
                except ProcessLookupError:
                    pass
                except OSError as error:
                    record.setdefault('signal_errors', []).append(str(error))
                deadline = time.monotonic() + grace
                while True:
                    proc.poll()  # Reap our direct child when it has exited.
                    state = process_group_state(pgid)
                    if (not state['live_pids'] and not state['errors'] or
                            time.monotonic() >= deadline):
                        break
                    time.sleep(.05)
            try:
                proc.wait(timeout=kill_grace)
                record['runner_reaped'] = True
            except subprocess.TimeoutExpired:
                pass
            record['returncode'] = proc.returncode
            record['after_cleanup'] = process_group_state(pgid)
            final = record['after_cleanup']
            record['cleanup_complete'] = (record['runner_reaped'] and
                not final['live_pids'] and not final['errors'] and
                not record.get('signal_errors'))
            record['descendant_reaping'] = ('orphan zombies observed; adopted '
                'parent must reap; no live group member' if final['zombie_pids']
                else 'no remaining group members observed')
            record['unexpected_live_descendants_after_runner_exit'] = unexpected_children
        record['ended_t'] = time.time()
        save(evidence, record)
        if failure is None and (not record['cleanup_complete'] or unexpected_children):
            raise RuntimeError('runner left live descendants or group cleanup is unknown')
    return returncode


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


def prepare_cold_payload(paths, out, max_rounds=1):
    """At most two pre-service advice rounds; preserve every observation."""
    if type(max_rounds) is not int or max_rounds not in (1, 2):
        raise ValueError('cold advice rounds must be one or two')
    rounds = []
    for number in range(1, max_rounds + 1):
        errors = clear_target_cache(paths)
        after = observe_files(paths)
        residual = payload_residual(after)
        current = {'round': number, 'advice_errors': errors,
                   'payload_resident_bytes': residual,
                   'cold_payload_established': not errors and residual == 0}
        rounds.append(current)
        if max_rounds > 1:
            save(out / f'cache-advice-round-{number:02d}.json',
                 {**current, 'observation': after})
        if current['cold_payload_established'] or errors:
            break
    save(out / 'cache-after-advice.json', after)
    gate = {'advice_errors': errors, 'payload_resident_bytes': residual,
            'cold_payload_established': not errors and residual == 0,
            'metadata_caches_not_exclusively_charged': True}
    if max_rounds > 1:
        gate.update(max_advice_rounds=max_rounds, rounds=rounds,
                    pre_service_only=True, inference_retry=False)
    return gate


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


def experiment_environment(chunk_order, inherited, partition=0,
                           policy_axis='chunk-order', phase_diagnostics=False,
                           request_partition=0, decode_partition_log_quiet=0):
    env = {key: value for key, value in inherited.items()
           if not key.startswith('Q4T_')}
    env.update(policy_environment(chunk_order, partition, policy_axis,
                                  phase_diagnostics, request_partition,
                                  decode_partition_log_quiet))
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
    if getattr(args, 'phase_diagnostics', False):
        command += ['--phase-diagnostics']
    if getattr(args, 'causality_sequence', None) is not None:
        command += ['--causality-sequence', str(out / 'causality-sequence.json'),
                    '--causality-sequence-sha256', args.causality_sequence_sha256]
    elif getattr(args, 'request_policy_sequence', False):
        command += ['--request-policy-sequence']
    elif plan:
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
    ap.add_argument('--cold-advice-rounds', type=int, choices=(1, 2), default=1,
                    help='Bounded pre-service cache preparation; every round is '
                         'retained and final payload residency must be zero')
    ap.add_argument('--chunk-order', type=int, choices=(0, 1), default=0,
                    help='Explicit Q4T_MOE_CHUNK_ORDER; off and on use the same binary')
    ap.add_argument('--policy-axis', choices=RUN_AXES, default='chunk-order')
    ap.add_argument('--partition', type=int, choices=(0, 1), default=0,
                    help='Partition axis requires chunk-order=0 for both runs')
    ap.add_argument('--request-partition', type=int, choices=(0, 1), default=0)
    ap.add_argument('--decode-partition-log-quiet', type=parse_binary_switch,
                    choices=(0, 1),
                    default=0, help='Explicit quiet bit for request-partition-log '
                    'axis; leaves prefill and forward diagnostics unchanged')
    ap.add_argument('--request-policy-sequence', action='store_true',
                    help='Frozen 7-position x 3-round request policy challenge')
    ap.add_argument('--causality-sequence', type=Path,
                    help='Frozen four-request predecessor diagnosis JSON')
    ap.add_argument('--causality-sequence-sha256',
                    help='Required exact bytes SHA256 of causality JSON')
    ap.add_argument('--phase-diagnostics', action='store_true',
                    help='Enable phase/cache snapshots and residency timing; '
                    'quality or fixed 1K/4K/8K diagnosis only, never acceptance')
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
    causality = args.causality_sequence is not None
    sequence_content = None
    try:
        env = experiment_environment(args.chunk_order, os.environ,
                                     args.partition, args.policy_axis,
                                     args.phase_diagnostics, args.request_partition,
                                     args.decode_partition_log_quiet)
        if causality != (args.causality_sequence_sha256 is not None):
            raise ValueError('causality sequence and SHA256 must be supplied together')
        if causality and (args.mode != 'performance' or
                args.policy_axis != 'request-partition-log' or
                args.request_policy_sequence or args.perf_lengths is not None
                or args.target_total or args.perf_repeats != 3 or
                args.phase_diagnostics):
            raise ValueError('causality sequence requires uninstrumented '
                             'request-partition-log without other selections')
        if args.request_policy_sequence and (
                args.policy_axis not in REQUEST_AXES or
                args.mode != 'performance' or args.perf_lengths is not None or
                args.target_total or args.perf_repeats != 3):
            raise ValueError('request sequence requires its own axis and fixed selection')
        if args.cold_advice_rounds != 1 and not args.clear_model_cache:
            raise ValueError('cold advice rounds require explicit cache preparation')
        plan = selected_performance_plan(args.mode, args.perf_lengths,
                                         args.perf_repeats, args.target_total)
        if args.request_policy_sequence:
            from request_policy_protocol import sequence_plan
            plan = sequence_plan()
        if causality:
            from causality_protocol import (check_causality_environment,
                                            read_causality_sequence)
            plan, sequence_content = read_causality_sequence(
                args.causality_sequence, args.causality_sequence_sha256)
            check_causality_environment(plan, env)
        if args.phase_diagnostics and plan and (
                plan['lengths'] != [1024, 4096, 8192] or args.target_total):
            raise ValueError('phase diagnosis requires ordered 1024,4096,8192')
    except (ValueError, OSError) as error:
        ap.error(str(error))
    diagnostic_scope = (plan['diagnostic_scope'] if causality else
                        DIAGNOSTIC_SCOPE if args.phase_diagnostics else None)
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
    if causality:
        (out / 'causality-sequence.json').write_bytes(sequence_content)
    unit = f'q4t-ram-{time.time_ns()}-{os.getpid()}.service'
    paths = model_files(args.model_dir)
    tool_dir = out / 'tools'
    tool_dir.mkdir()
    tool_hashes = {}
    tool_names = RUN_TOOLS + ('offload_policy.py',)
    if args.policy_axis in REQUEST_AXES:
        tool_names += ('request_policy_protocol.py',)
    if causality:
        tool_names += ('causality_protocol.py',)
    for name in tool_names:
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
        'policy_axis': args.policy_axis, 'partition': args.partition,
        **({'request_partition': args.request_partition,
            'request_policy_sequence': args.request_policy_sequence,
            'cold_advice_rounds': args.cold_advice_rounds}
           if args.policy_axis in REQUEST_AXES else {}),
        **({'decode_partition_log_quiet': args.decode_partition_log_quiet}
           if args.policy_axis == 'request-partition-log' else {}),
        **({'causality_sequence_path': str(args.causality_sequence.resolve()),
            'causality_sequence_sha256': args.causality_sequence_sha256,
            'causality_group_id': plan['group_id'],
            'causality_phase_plan_sha256': plan['phase_plan_sha256']}
           if causality else {}),
        'phase_diagnostics': args.phase_diagnostics,
        'diagnostic_scope': diagnostic_scope,
        'performance_acceptance': False,
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
        'runner_timeout_scope': 'owned runner session/process group; TERM then '
                                'KILL with bounded waits; q4t cleaned by exact unit',
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
        gate = prepare_cold_payload(paths, out, args.cold_advice_rounds)
        save(out / 'cache-gate.json', gate)
        if not gate['cold_payload_established']:
            save(out / 'wrapper-exit.json', {
                'runner_rc': None, 'monitor_rc': None,
                'failure': 'cold payload gate failed; no service started',
                'cleanup_failed': False, 'performance_qualification': False,
                'diagnostic_scope': diagnostic_scope,
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
                runner_rc = run_owned_runner(command, cwd=ROOT, env=env, log=log,
                    timeout=args.runner_timeout_s,
                    evidence=out / 'runner-process-group.json')
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
                'diagnostic_scope': diagnostic_scope,
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
