"""Bounded cuFile capability diagnostic; run the whole driver under GPU lease.

This reads one expert range, never writes the checkpoint, and makes no model
performance claim. No retries, module installation, or system configuration
changes. A completed diagnostic can contain unsupported cuFile cases.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time


ROOT = Path(__file__).resolve().parents[3]
BASE = ROOT / '.q4t-work/gds-minimal-20261021'
PRIOR = ROOT / '.q4t-work/ssd-direct-evaluation-20261021/local-io-v1'


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def identity(path):
    stat = path.stat()
    return dict(path=str(path), device=stat.st_dev, inode=stat.st_ino,
                bytes=stat.st_size, mtime_ns=stat.st_mtime_ns)


def snapshot():
    result = dict(monotonic=time.monotonic(), wall=time.time())
    for name in ('/proc/meminfo', '/proc/diskstats', '/proc/modules',
                 '/proc/driver/nvidia/version'):
        try:
            result[name] = Path(name).read_text()
        except OSError as error:
            result[name] = dict(error=repr(error))
    return result


def stop(child):
    if child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=3)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=3)


def run_process(command, directory, environment, timeout):
    start = time.monotonic()
    info = dict(command=command, timeout_seconds=timeout)
    child = None
    with (directory / 'stdout.jsonl').open('w') as stdout, (
            directory / 'stderr.log').open('w') as stderr:
        try:
            child = subprocess.Popen(command, cwd=directory, env=environment,
                                     stdout=stdout, stderr=stderr)
            info['pid'] = child.pid
            with (directory / 'process-samples.jsonl').open('w') as samples:
                while child.poll() is None:
                    if time.monotonic() - start >= timeout:
                        raise TimeoutError('bounded diagnostic deadline')
                    row = dict(monotonic=time.monotonic(), pid=child.pid)
                    for name in ('status', 'io', 'maps'):
                        try:
                            row[name] = Path(f'/proc/{child.pid}/{name}').read_text()
                        except OSError as error:
                            row[name] = dict(error=repr(error))
                    samples.write(json.dumps(row) + '\n')
                    samples.flush()
                    time.sleep(0.1)
            info['returncode'] = child.returncode
        except BaseException as error:
            info['error'] = repr(error)
            raise
        finally:
            cleanup_error = None
            if child is not None:
                try:
                    stop(child)
                except BaseException as error:
                    info['cleanup_error'] = repr(error)
                    cleanup_error = error
                finally:
                    info['returncode'] = child.returncode
            info['seconds'] = time.monotonic() - start
            write(directory / 'exit.json', info)
            if cleanup_error is not None:
                raise cleanup_error
    return info


def config(original, directory, compat, p2p):
    value = copy.deepcopy(original)
    value['logging'].update(dir=str(directory), level='DEBUG',
                            max_file_size_mb=4)
    value['profile'].update(cufile_stats=3, nvtx=False)
    value['execution'].update(max_io_threads=1, max_io_queue_depth=4,
                              parallel_io=False, max_request_parallelism=1)
    props = value['properties']
    for key in ('max_direct_io_size_kb', 'max_device_cache_size_kb',
                'per_buffer_cache_size_kb'):
        props.pop(key, None)
    props.update(allow_compat_mode=compat, force_compat_mode=False,
                 use_pci_p2pdma=p2p,
                 max_device_pinned_mem_size_kb=32768, io_batchsize=4,
                 gpu_bounce_buffer_slab_config=dict(
                     slab_size_kb=[1024, 4096], slab_count=[2, 2]),
                 posix_pool_slab_size_kb=[4, 1024, 4096],
                 posix_pool_slab_count=[4, 2, 2])
    value['block']['nvme']['use_pci_p2pdma'] = p2p
    return value


def classify(events, result, returncode):
    """Permit only explicit unsupported paths to continue the fixed matrix."""
    if (result.get('cleanup_ok') is not True or
            result.get('logging_ok') is not True or result.get('signal') != 0):
        raise RuntimeError('probe cleanup, logging or signal failure')
    if result.get('status') == 'PASS':
        if (returncode != 0 or result.get('data_match') is not True or
                result.get('file_unchanged') is not True or
                result.get('read_attempted') is not True or
                result.get('returned_bytes') != result.get('requested_bytes')):
            raise RuntimeError('inconsistent successful probe')
        return 'EXACT_BYTES_PASS_PATH_REQUIRES_REVIEW'
    if result.get('status') != 'FAILED' or returncode != 1:
        raise RuntimeError('unknown probe outcome')
    expected_cufile = {5001, 5007, 5008, 5009, 5034}
    expected = []
    for event in events:
        if event.get('event') != 'api' or event.get('phase') != 'work':
            continue
        name = event['name']
        if name == 'cuFileRead':
            value = event['returned_bytes']
            if value < -1 and -value in expected_cufile:
                expected.append(event)
            elif value == -1 and event.get('errno_raw') in (19, 38, 95):
                expected.append(event)
            else:
                # A partial read or a later comparison/truth error is fatal.
                raise RuntimeError('read or subsequent verification failed')
        elif name.startswith('cuFile'):
            if event.get('rc') in expected_cufile:
                expected.append(event)
            elif event.get('rc') != 0:
                raise RuntimeError('unexpected cuFile work error')
        elif name.startswith(('cu', 'cuda')) and event.get('rc') != 0:
            raise RuntimeError('CUDA work error')
        elif event.get('rc', 0) < 0:
            raise RuntimeError('POSIX work error')
    if len(expected) != 1:
        raise RuntimeError('failure lacks a single explicit unsupported result')
    return 'UNSUPPORTED_OBSERVED'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--probe', type=Path, required=True)
    args = parser.parse_args()
    output, probe = args.output.resolve(), args.probe.resolve()
    if not output.is_relative_to(BASE) or not probe.is_relative_to(BASE):
        parser.error('output and probe must be under this frozen stage')
    output.mkdir(parents=True, exist_ok=False)
    # Check the previous read-only census before using its exact file range.
    seal = json.loads((PRIOR / 'seal.json').read_text())
    assert sha(PRIOR / 'header-alignment.json') == seal[
        'header-alignment.json']['sha256']
    census = json.loads((PRIOR / 'header-alignment.json').read_text())
    sample = census['headers'][0]
    model = Path(sample['path'])
    before_file = identity(model)
    for key, expected in (('device', 'device'), ('inode', 'inode'),
                          ('bytes', 'file_size'), ('mtime_ns', 'mtime_ns')):
        assert before_file[key] == sample[expected], key
    region = sample['experts'][0]['regions'][0]
    assert region['region'] == 'weight'
    raw_offset, raw_bytes = region['file_offset'], region['length']
    aligned_offset = raw_offset // 4096 * 4096
    aligned_bytes = ((raw_offset + raw_bytes + 4095) // 4096 * 4096
                     - aligned_offset)
    assert 0 < aligned_bytes <= 8 * 1024**2
    system_config = Path('/etc/cufile.json')
    original_text = system_config.read_text()
    original = json.loads(re.sub(r'//[^\n]*', '', original_text))
    (output / 'system-cufile.jsonc').write_text(original_text)
    protected = [system_config, ROOT / 'build/q4t',
                 ROOT / 'build/q4t.release.json']
    protected_before = {str(path): sha(path) for path in protected}
    sources = {str(path.relative_to(ROOT)): sha(path) for path in (
        Path(__file__).resolve(), Path(__file__).with_name('probe.cpp'), probe)}
    modes = [('strict-current', False, False),
             ('strict-request-p2pdma', False, True),
             ('compat-control', True, False)]
    plan = dict(task='cuFile capability and exact-byte diagnostic',
                model=before_file, source_sha256=sources,
                aligned_offset=aligned_offset, aligned_bytes=aligned_bytes,
                raw_offset=raw_offset, raw_bytes=raw_bytes,
                modes=[dict(name=n, allow_compat_mode=c, request_p2pdma=p)
                       for n, c, p in modes],
                conditional_case='one unaligned read in first successful mode',
                max_probe_processes=4, cuFile_reads_per_probe=1, retries=0,
                max_bytes_per_read=8 * 1024**2, probe_timeout_seconds=30,
                app_device_bytes_max=8 * 1024**2,
                app_host_validation_bytes_max=16 * 1024**2,
                gpu_bounce_slab_budget_bytes=10 * 1024**2,
                posix_slab_budget_bytes=10 * 1024**2 + 16384,
                memory_limit_note='buffer budgets exclude context, library '
                                  'metadata, file cache and OS; not total RAM cap',
                cache='inherited; no eviction or prewarm; pread truth after GDS',
                gdscheck_runs=0, model_loads=0, http_requests=0,
                performance_acceptance=False,
                system_configuration_change=False,
                protected_before=protected_before)
    write(output / 'plan.json', plan)
    write(output / 'before.json', snapshot())
    state = dict(status='RUNNING', cases=[], started=time.time())
    successful_modes = []

    def execute(name, compat, p2p, offset, length):
        directory = output / name
        directory.mkdir(exist_ok=False)
        cfg = directory / 'cufile.json'
        write(cfg, config(original, directory, compat, p2p))
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(('CUFILE_', 'GDS_', 'LD_'))}
        env['CUFILE_ENV_PATH_JSON'] = str(cfg)
        command = [str(probe), '--path', str(model), '--offset', str(offset),
                   '--bytes', str(length), '--hold-seconds', '3']
        info = run_process(command, directory, env, timeout=30)
        events = []
        unparsed = []
        for line in (directory / 'stdout.jsonl').read_text().splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                unparsed.append(line)
        finals = [event for event in events if event.get('event') == 'result']
        if len(finals) != 1:
            raise RuntimeError(f'{name}: missing or duplicate probe result')
        result = finals[0]
        row = dict(name=name, config_sha256=sha(cfg), process=info,
                   result=result, unparsed_stdout=unparsed)
        state['cases'].append(row)
        write(output / 'run.json', state)
        row['outcome'] = classify(events, result, info['returncode'])
        configurations = {e['name']: e for e in events
                          if e.get('event') == 'configuration'}
        if not compat and result['read_attempted']:
            for field in ('allow_compat_mode', 'force_compat_mode'):
                value = configurations.get(field, {})
                if value.get('known') is not True or value.get('value') is not False:
                    raise RuntimeError('strict mode effective configuration unproven')
        write(output / 'run.json', state)
        return result

    try:
        for name, compat, p2p in modes:
            result = execute(name, compat, p2p, aligned_offset, aligned_bytes)
            if result.get('status') == 'PASS':
                successful_modes.append((name, compat, p2p))
        if successful_modes:
            name, compat, p2p = successful_modes[0]
            execute(name + '-unaligned', compat, p2p, raw_offset, raw_bytes)
        state['status'] = 'DIAGNOSTIC_COMPLETED'
    except BaseException as error:
        state.update(status='STOPPED', error=repr(error))
        raise
    finally:
        state['finished'] = time.time()
        state['model_identity_after'] = identity(model)
        state['protected_after'] = {str(path): sha(path) for path in protected}
        state['identities_unchanged'] = (
            state['model_identity_after'] == before_file and
            state['protected_after'] == protected_before)
        if not state['identities_unchanged']:
            state['status'] = 'IDENTITY_MISMATCH'
        write(output / 'after.json', snapshot())
        write(output / 'run.json', state)
        files = {str(path.relative_to(output)): sha(path)
                 for path in sorted(output.rglob('*')) if path.is_file()
                 and path.name != 'files.json'}
        write(output / 'files.json', files)
    if not state['identities_unchanged']:
        raise RuntimeError('protected identities changed')
    print(json.dumps(dict(status=state['status'], cases=len(state['cases']),
                          output=str(output)), ensure_ascii=False))


if __name__ == '__main__':
    def interrupted(_signal, _frame):
        raise KeyboardInterrupt('diagnostic interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    main()
