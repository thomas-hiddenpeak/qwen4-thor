"""Conditional 20-request observation using the accepted HTTP/monitor driver."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import threading

from lifecycle import audit as trace_audit
from owned_collector import Collector

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    'pwrite_original', ROOT/'tools/session_preallocate/run_stage.py')
pre = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pre)
reuse, legacy, save = pre.reuse, pre.legacy, pre.save


def validate(plan):
    if (plan['http_requests'], plan['http_loads'], len(plan['steps'])) not in ((20, 2, 2), (10, 1, 1)):
        raise ValueError('fixed HTTP scope')
    if plan['http_requests'] == 10 and any(s['head'] != 'subset' for s in plan['steps']):
        raise ValueError('subset-only supplemental scope')
    if time.time() >= plan['task_deadline_unix']:
        raise TimeoutError('absolute deadline')
    for path in plan['required_gates']:
        if not json.loads(Path(path).read_text())['passed']:
            raise RuntimeError('CPU gate failed')


def custom_step(plan, name):
    legacy.require_guard()
    out = Path(plan['output'])/name
    marker = Path(plan['output'])/'trace-env-abort.json'
    original_popen = subprocess.Popen
    original_eval = pre.seed.old.eval_one
    trace = Path(plan['trace_directory'])
    io_boundaries = []
    boundary_lock = threading.Lock()

    def env_abort(reason):
        save(marker, dict(status='ENV_ABORT', reason=reason, step=name,
                          recorded_at=time.time()))
        raise legacy.EnvironmentAbort(reason)

    def check_collector():
        if (trace/'exit.json').exists():
            env_abort('trace collector exited during HTTP')
        try:
            os.kill(json.loads((trace/'ready.json').read_text())['pid'], 0)
        except ProcessLookupError:
            env_abort('trace collector disappeared')

    def launch(argv, *args, **kwargs):
        if argv[:2] != [plan['candidate'], 'serve']:
            return original_popen(argv, *args, **kwargs)
        check_collector()
        load_deadline = min(time.time()+30, plan['task_deadline_unix'])
        wrapped = [sys.executable, '-B', str(ROOT/'tools/session_pwrite_trace/target.py'),
                   str(trace), str(load_deadline), *argv]
        save(out/'trace-launch.json', dict(argv=wrapped))
        return original_popen(wrapped, *args, **kwargs)

    def evaluate(value, case, folder, env, check, extra):
        def checked():
            check()
            check_collector()
        begin = time.monotonic_ns()
        row = original_eval(value, case, folder, env, checked, extra)
        checked()
        with boundary_lock:
            io_boundaries.append(dict(folder=str(folder), start_ns=begin,
                                      end_ns=time.monotonic_ns(),
                                      response_id=row['response_id']))
            save(out/'http-trace-boundaries.json', io_boundaries)
        return row

    subprocess.Popen = launch
    pre.seed.old.eval_one = evaluate
    try:
        pre.custom_step(plan, name)
    finally:
        subprocess.Popen = original_popen
        pre.seed.old.eval_one = original_eval
    pre.audit(out, 10)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--custom-step')
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text())
    validate(plan)
    reuse.validate = validate
    reuse.custom_step = custom_step
    reuse.admission.audit_server = pre.audit
    old_monitor = legacy.require_monitor

    def monitor(*args, **kwargs):
        old_monitor(*args, **kwargs)
        marker = Path(plan['output'])/'trace-env-abort.json'
        if marker.exists(): raise legacy.EnvironmentAbort(marker.read_text())

    legacy.require_monitor = monitor
    preparation = None
    collector = None
    collector_log = None
    def stop_collector():
        if collector is None or collector.poll() is not None: return
        (trace/'stop').touch()
        try:
            collector.wait(timeout=30)
        except subprocess.TimeoutExpired:
            collector.terminate()
            collector.wait(timeout=10)
            raise legacy.EnvironmentAbort('trace final flush timeout')
        if collector.returncode:
            raise legacy.EnvironmentAbort('trace final flush failed')

    try:
        if not args.custom_step:
            legacy.require_guard()
            legacy.verify_files(plan)
            spec = importlib.util.spec_from_file_location(
                'pwrite_io', ROOT/'tools/prefix_seed/precondition.py')
            io = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(io)
            if plan.get('completed_preparation'):
                reused = json.loads(Path(plan['completed_preparation']).read_text())
                released = json.loads(Path(plan['completed_preparation_release']).read_text())
                if (reused['bytes'] != 64*1024**3 or reused['failure'] is not None
                        or not released['passed'] or Path(reused['path']).exists()):
                    raise RuntimeError('completed preparation identity/state')
                save(args.plan.parent/'io-preparation-reuse.json',
                     dict(source=plan['completed_preparation'], fresh_write_bytes=0,
                          recorded_at=time.time(), source_finished=reused['finished']))
            else:
                preparation = io.prepare(args.plan.parent, plan['task_deadline_unix'])
            with (args.plan.parent/'smart-http-before.json').open('x') as output:
                subprocess.run(['sudo', '-n', '/usr/sbin/nvme', 'smart-log', '/dev/nvme0', '-o', 'json'],
                    stdout=output, check=True, timeout=10)
            trace = Path(plan['trace_directory'])
            trace.mkdir()
            collector_log = (trace/'collector.log').open('x')
            collector = Collector(plan['trace_object'], trace,
                                  plan['task_deadline_unix'], collector_log)
            loading_deadline = min(time.time()+30, plan['task_deadline_unix'])
            while not (trace/'ready.json').exists():
                if collector.poll() is not None:
                    raise legacy.EnvironmentAbort('collector pre-monitor load failed')
                if time.time() >= loading_deadline:
                    raise legacy.EnvironmentAbort('collector pre-monitor deadline')
                time.sleep(.01)
            save(args.plan.parent/'trace-pre-monitor.json',
                 dict(ready=json.loads((trace/'ready.json').read_text()),
                      before_resource_monitor=True, recorded_at=time.time()))
            original_verify = legacy.verify_files
            verifications = 0

            def verify(value):
                global verifications
                original_verify(value)
                verifications += 1
                if verifications == 2:
                    stop_collector()

            legacy.verify_files = verify
        reuse.main()
    finally:
        try:
            stop_collector()
        finally:
            if collector_log: collector_log.close()
            if preparation is not None:
                io.release(preparation, args.plan.parent)
        if not args.custom_step and collector is not None:
            with (args.plan.parent/'smart-http-after.json').open('x') as output:
                subprocess.run(['sudo', '-n', '/usr/sbin/nvme', 'smart-log', '/dev/nvme0', '-o', 'json'],
                    stdout=output, check=True, timeout=10)

    if collector is not None:
        if collector.returncode:
            raise legacy.EnvironmentAbort('trace collector failed')
        result = trace_audit(trace)
        save(trace/'lifecycle.json', result)
        total = 0
        startup = []
        for step in plan['steps']:
            out = Path(plan['output'])/step['name']
            rows = json.loads((out/'timing-audit.json').read_text())['records']
            expected = sum(int(r['disk_bytes']) for r in rows)
            events = [json.loads(line) for line in (out/'events.jsonl').read_text().splitlines()]
            pid = next(e['pid'] for e in events if e['event'] == 'server_started')
            boundary = min(r['start_ns'] for r in
                json.loads((out/'http-trace-boundaries.json').read_text()))
            calls = [c for c in result['calls'] if c['task'] >> 32 == pid]
            probes = [c for c in calls if c['end_ns'] < boundary]
            if len(probes) != 2 or any(c['written'] != 4096 or c['offset'] != 0
                                       for c in probes):
                raise RuntimeError('two cache initialization I/O probes')
            measured = [c for c in calls if c['end_ns'] >= boundary]
            if expected != sum(c['written'] for c in measured):
                raise RuntimeError('HTTP/trace aggregate byte mismatch')
            if sum(int(r['pwrite_calls']) for r in rows) != len(measured):
                raise RuntimeError('HTTP/trace syscall count mismatch')
            total += expected
            startup.extend(probes)
        save(args.plan.parent/'http-trace-gate.json', dict(passed=True,
             calls=len(result['calls']), logical_write_bytes=total,
             initialization_probes=startup, http_requests=plan['http_requests'],
             restores=2*len(plan['steps'])))
