"""One-shot compute PID query with an externally supervised deadline.

No CUDA context, cached result, CLI fallback, or error-to-empty conversion.
The worker reports each API boundary so a timeout identifies the pending call.
"""
import argparse
import json
import math
from pathlib import Path
import secrets
import subprocess
import sys
import time


def worker(request_id):
    def emit(**fields):
        print(json.dumps(dict(request_id=request_id,
                              monotonic=time.monotonic(), **fields)), flush=True)

    def call(name, function, *args):
        emit(event='begin', api=name)
        start = time.monotonic()
        value = function(*args)
        emit(event='end', api=name, seconds=time.monotonic() - start)
        return value

    initialized = False
    failure = None
    pids = set()
    memory = []
    count = None
    try:
        emit(event='begin', api='import_pynvml')
        import pynvml as nvml
        emit(event='end', api='import_pynvml')
        call('init', nvml.nvmlInit)
        initialized = True
        count = call('device_count', nvml.nvmlDeviceGetCount)
        if count <= 0:
            raise RuntimeError('no visible NVML devices')
        for index in range(count):
            handle = call(f'handle[{index}]', nvml.nvmlDeviceGetHandleByIndex,
                          index)
            processes = call(f'compute_v3[{index}]',
                             nvml.nvmlDeviceGetComputeRunningProcesses_v3,
                             handle)
            pids.update(int(process.pid) for process in processes)
            for process in processes:
                used = getattr(process, 'usedGpuMemory', None)
                # NVML_VALUE_NOT_AVAILABLE and absent fields are unknown.
                if type(used) is not int or used < 0 or used >= 2**63:
                    used = None
                memory.append(dict(device=index, pid=int(process.pid),
                                   used_bytes=used))
    except Exception as error:
        failure = dict(type=type(error).__name__, message=str(error),
                       nvml_code=getattr(error, 'value', None))
    finally:
        if initialized:
            try:
                call('shutdown', nvml.nvmlShutdown)
            except Exception as error:
                failure = failure or dict(type=type(error).__name__,
                                          message=str(error))
    emit(event='result', status='ERROR' if failure else 'OK', error=failure,
         device_count=count, pids=None if failure else sorted(pids),
         process_memory=None if failure else memory)
    return 1 if failure else 0


def supervise(command, request_id, timeout):
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('timeout must be finite and positive')
    start = time.monotonic()
    result = dict(status='ERROR', pids=None, request_id=request_id)
    process = subprocess.Popen(command, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    result['worker_pid'] = process.pid
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()  # Only the direct, owned query worker; no descendants.
        try:
            stdout, stderr = process.communicate(timeout=1)
            result['status'] = 'TIMEOUT'
        except subprocess.TimeoutExpired as error:
            # Do not block indefinitely waiting on an uninterruptible driver.
            result.update(status='CLEANUP_FAILED', error='worker not reaped')
            stdout = error.output or ''
            stderr = error.stderr or ''
            process.stdout.close()
            process.stderr.close()
        result.update(stdout=stdout.decode(errors='replace')
                      if isinstance(stdout, bytes) else stdout,
                      stderr=stderr.decode(errors='replace')
                      if isinstance(stderr, bytes) else stderr,
                      seconds=time.monotonic() - start,
                      returncode=process.returncode)
        return result
    except BaseException:
        process.kill()
        try:
            process.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            process.stdout.close()
            process.stderr.close()
        raise
    result.update(stdout=stdout, stderr=stderr, returncode=process.returncode,
                  seconds=time.monotonic() - start)
    try:
        events = [json.loads(line) for line in stdout.splitlines()]
        if not events or any(e.get('request_id') != request_id for e in events):
            raise ValueError('missing or mismatched request identity')
        finals = [e for e in events if e.get('event') == 'result']
        if len(finals) != 1 or finals[0] is not events[-1]:
            raise ValueError('missing, duplicate or nonterminal result')
        final = finals[0]
        if final['status'] == 'ERROR':
            result['error'] = final.get('error')
            return result
        pids = final['pids']
        if (process.returncode != 0 or final['status'] != 'OK' or
                type(final['device_count']) is not int or
                final['device_count'] <= 0 or not isinstance(pids, list) or
                any(type(pid) is not int or pid <= 0 for pid in pids)):
            raise ValueError('invalid successful result')
        memory = final.get('process_memory')
        if memory is not None:
            seen = set()
            for row in memory:
                key = (row['device'], row['pid'])
                used = row['used_bytes']
                if (type(row['device']) is not int or
                        not 0 <= row['device'] < final['device_count'] or
                        type(row['pid']) is not int or row['pid'] not in pids or
                        key in seen or (used is not None and
                        (type(used) is not int or not 0 <= used < 2**63))):
                    raise ValueError('invalid process memory record')
                seen.add(key)
            if {row['pid'] for row in memory} != set(pids):
                raise ValueError('incomplete process memory records')
        result['process_memory'] = memory
        result.update(status='OK', pids=sorted(set(pids)),
                      device_count=final['device_count'])
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        result.update(status='PROTOCOL_ERROR', error=str(error))
    return result


def query(timeout=5):
    request_id = secrets.token_hex(16)
    return supervise([sys.executable, '-B', str(Path(__file__).resolve()),
                      '--worker', request_id], request_id, timeout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker', help=argparse.SUPPRESS)
    parser.add_argument('--timeout', type=float, default=5)
    args = parser.parse_args()
    if args.worker:
        return worker(args.worker)
    result = query(args.timeout)
    print(json.dumps(result), flush=True)
    return 0 if result['status'] == 'OK' else 1


if __name__ == '__main__':
    sys.exit(main())
