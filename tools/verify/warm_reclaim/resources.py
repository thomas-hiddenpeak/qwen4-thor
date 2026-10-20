"""Bounded, read-only physical-memory and target-stack observer.

Requires root for kernel stacks. Does not invoke NVML, fault model payloads,
send process signals, or alter kernel/cache state. Run outside the model's
memory cgroup, while the caller holds the whole-stage GPU lease.
"""
import argparse
import errno
import gzip
import hashlib
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'tools/evalscope'))
from file_cache import model_files, observe_files

MAX_BYTES = 256 << 20
FINAL_RESERVE = 8192
SYSTEM_FILES = ('meminfo', 'vmstat', 'buddyinfo', 'pressure/memory',
                'pressure/io', 'pressure/cpu')
CGROUP_FILES = ('memory.current', 'memory.max', 'memory.stat',
                'memory.events', 'memory.pressure', 'memory.swap.current',
                'memory.swap.max')
GONE = (errno.ENOENT, errno.ESRCH)


def stamps():
    return dict(wall=time.time(), monotonic=time.monotonic())


def read_text(path):
    try:
        return dict(value=path.read_text(), error=None, errno=None)
    except OSError as error:
        return dict(value=None, error=str(error), errno=error.errno)


def psi_support(config_path=Path('/proc/config.gz')):
    """Only the running kernel's explicit configuration can waive PSI."""
    result = dict(supported=None, source=str(config_path), setting=None)
    try:
        with gzip.open(config_path, 'rt') as source:
            config = source.read()
        result['config_sha256'] = hashlib.sha256(config.encode()).hexdigest()
        settings = set(config.splitlines())
        if '# CONFIG_PSI is not set' in settings:
            result.update(supported=False, setting='# CONFIG_PSI is not set')
        elif 'CONFIG_PSI=y' in settings:
            result.update(supported=True, setting='CONFIG_PSI=y')
    except (OSError, EOFError, UnicodeError) as error:
        result['error'] = repr(error)
    return result


def classify_pressure(value, support):
    """Retain ENOENT rather than inventing zero pressure on non-PSI kernels."""
    if (value['errno'] == errno.ENOENT and support is not None
            and support.get('supported') is False):
        value = dict(value, status='unsupported',
                     reason='running kernel CONFIG_PSI is not set')
    return value


def parse_stat(value):
    """The comm field can contain spaces and parentheses."""
    fields = value.rsplit(')', 1)[1].split()
    return dict(state=fields[0], starttime_ticks=int(fields[19]))


def cgroup_path(value, root):
    matches = [line[3:] for line in value.splitlines()
               if line.startswith('0::')]
    if len(matches) != 1 or not matches[0].startswith('/'):
        raise ValueError('expected exactly one absolute cgroup-v2 path')
    relative = Path(matches[0].lstrip('/'))
    if '..' in relative.parts:
        raise ValueError('cgroup path traverses outside root')
    result = (root / relative).resolve()
    if not result.is_relative_to(root.resolve()):
        raise ValueError('cgroup path resolves outside root')
    return result


class Evidence:
    """Exclusive creation and one aggregate byte budget for our own files."""

    def __init__(self, folder, limit=MAX_BYTES):
        self.folder = folder
        self.limit = limit
        self.bytes = 0
        self.streams = {}
        folder.mkdir(parents=True, exist_ok=True)
        for name in ('ready.json', 'exit.json', 'resources.jsonl',
                     'cgroup.jsonl'):
            if (folder / name).exists():
                raise FileExistsError(folder / name)
        (folder / 'cache').mkdir(exist_ok=True)
        if any((folder / 'cache').iterdir()):
            raise FileExistsError('cache evidence directory is not empty')

    def write(self, name, value, *, stream=False, final=False):
        payload = (json.dumps(value, separators=(',', ':')) + '\n').encode()
        bound = self.limit if final else self.limit - FINAL_RESERVE
        if self.bytes + len(payload) > bound:
            raise OverflowError('observer output byte budget exhausted')
        if stream:
            if name not in self.streams:
                self.streams[name] = (self.folder / name).open('xb')
            handle = self.streams[name]
            handle.write(payload)
            handle.flush()
        else:
            with (self.folder / name).open('xb') as handle:
                handle.write(payload)
        self.bytes += len(payload)

    def close(self):
        for handle in self.streams.values():
            handle.close()


def target_snapshot(proc_root, binary_identity, groups, cgroup_root):
    targets, failures = [], []
    for proc in proc_root.iterdir():
        if not proc.name.isdecimal():
            continue
        command = read_text(proc / 'cmdline')
        if command['error']:
            if command['errno'] not in GONE:
                failures.append(dict(path=str(proc / 'cmdline'), **command))
            continue
        args = command['value'].rstrip('\0').split('\0')
        model = False
        try:
            identity = (proc / 'exe').stat()
            model = (identity.st_dev, identity.st_ino) == binary_identity
        except OSError as error:
            if error.errno not in GONE:
                failures.append(dict(path=str(proc / 'exe'), error=str(error)))
        query = (any(arg.endswith('/nvml_process_query.py') for arg in args)
                 and '--worker' in args)
        if not (model or query):
            continue
        record = dict(pid=int(proc.name), kind='model' if model else 'nvml',
                      command=args, tasks=[], cgroup=read_text(proc / 'cgroup'))
        targets.append(record)
        try:
            tasks = list((proc / 'task').iterdir())
        except OSError as error:
            record['task_error'] = str(error)
            if error.errno not in GONE:
                failures.append(dict(pid=int(proc.name), error=str(error)))
            continue
        for task in tasks:
            stat = read_text(task / 'stat')
            if stat['error']:
                record['tasks'].append(dict(tid=int(task.name), stat=stat))
                if stat['errno'] not in GONE:
                    failures.append(dict(path=str(task / 'stat'), **stat))
                continue
            info = parse_stat(stat['value'])
            if task.name == proc.name:
                record['starttime_ticks'] = info['starttime_ticks']
            if task.name != proc.name and info['state'] != 'D':
                continue
            data = dict(tid=int(task.name), **info, stat=stat,
                        wchan=read_text(task / 'wchan'),
                        stack=read_text(task / 'stack'))
            record['tasks'].append(data)
            for name in ('wchan', 'stack'):
                if data[name]['error'] and data[name]['errno'] not in GONE:
                    failures.append(dict(path=str(task / name), **data[name]))
        if record['cgroup']['error']:
            if record['cgroup']['errno'] not in GONE:
                failures.append(dict(path=str(proc / 'cgroup'),
                                     **record['cgroup']))
        elif model:
            path = cgroup_path(record['cgroup']['value'], cgroup_root)
            key = str(path)
            if key not in groups:
                groups[key] = dict(path=path, first_seen=stamps(), owners=[])
            owner = dict(pid=record['pid'],
                         starttime_ticks=record.get('starttime_ticks'))
            if owner not in groups[key]['owners']:
                groups[key]['owners'].append(owner)
    return targets, failures


def group_snapshot(groups, psi=None):
    results, failures = [], []
    for group in groups.values():
        path = group['path']
        row = dict(path=str(path), first_seen=group['first_seen'],
                   owners=group['owners'], **stamps())
        if not path.exists():
            row['status'] = 'removed'
        else:
            row['files'] = {name: read_text(path / name)
                            for name in CGROUP_FILES}
            row['files']['memory.pressure'] = classify_pressure(
                row['files']['memory.pressure'], psi)
            errors = {key: val for key, val in row['files'].items()
                      if val['error'] and val.get('status') != 'unsupported'}
            # Owned scopes can disappear between individual control reads.
            row['status'] = ('removed_during_read' if not path.exists()
                             else 'read_error' if errors else 'observed')
            if errors and row['status'] == 'read_error':
                failures.append(dict(path=str(path), errors=errors))
        results.append(row)
    return results, failures


def observe(args):
    evidence = Evidence(args.output)
    start = stamps()
    final = dict(status='FAILED', start=start, truncated=False, failures=[],
                 system_samples=0, cache_samples=0, cgroup_samples=0,
                 skipped_system_intervals=0, skipped_cache_intervals=0)
    result = 2
    try:
        if os.geteuid() != 0:
            raise PermissionError('root required for target kernel stacks')
        if args.stop_file.exists():
            raise FileExistsError('stop file already exists')
        identity = args.binary.stat()
        binary_identity = (identity.st_dev, identity.st_ino)
        paths = model_files(args.model_dir)
        if not paths:
            raise ValueError('model file set is empty')
        psi = psi_support()
        final['psi_support'] = psi
        groups = {}
        next_system = next_cache = time.monotonic()
        deadline = start['monotonic'] + args.max_seconds
        while True:
            now = time.monotonic()
            if now >= deadline:
                final['status'] = 'DEADLINE_EXCEEDED'
                final['failures'].append(
                    'deadline reached before observation of stop file')
                break
            if args.stop_file.exists():
                final.update(status='COMPLETED', reason='stop_file')
                result = 0
                break
            row = stamps()
            system = {name: read_text(Path('/proc') / name)
                      for name in SYSTEM_FILES}
            for name in SYSTEM_FILES:
                if name.startswith('pressure/'):
                    system[name] = classify_pressure(system[name], psi)
            targets, failures = target_snapshot(
                Path('/proc'), binary_identity, groups, Path('/sys/fs/cgroup'))
            failures.extend(dict(path='/proc/' + key, **value)
                            for key, value in system.items() if value['error']
                            and value.get('status') != 'unsupported')
            row.update(system=system, targets=targets, failures=failures,
                       finished=stamps(), scheduled_monotonic=next_system)
            evidence.write('resources.jsonl', row, stream=True)
            final['system_samples'] += 1
            if failures:
                raise RuntimeError('required system/process observations failed')
            if now >= next_cache:
                cache_start = stamps()
                snapshot = observe_files(paths)
                snapshot.update(observer_start=cache_start,
                                observer_finished=stamps(),
                                sequence=final['cache_samples'])
                name = 'cache/{:06d}.json'.format(final['cache_samples'])
                evidence.write(name, snapshot)
                final['cache_samples'] += 1
                if not snapshot['complete_file_set_observed']:
                    raise RuntimeError('model cachestat incomplete: ' + name)
                rows, failures = group_snapshot(groups, psi)
                evidence.write('cgroup.jsonl', dict(
                    **stamps(), groups=rows, failures=failures), stream=True)
                final['cgroup_samples'] += 1
                if failures:
                    raise RuntimeError('required cgroup observations failed')
                elapsed_intervals = max(
                    1, int((time.monotonic() - next_cache) // 1.0) + 1)
                final['skipped_cache_intervals'] += elapsed_intervals - 1
                next_cache += elapsed_intervals
            if final['system_samples'] == 1:
                evidence.write('ready.json', dict(
                    **stamps(), pid=os.getpid(), binary=str(args.binary),
                    binary_dev=binary_identity[0], binary_inode=binary_identity[1],
                    model_dir=str(args.model_dir), model_files=len(paths),
                    psi_support=psi,
                    max_seconds=args.max_seconds, max_bytes=MAX_BYTES,
                    system_interval_seconds=.2, cache_interval_seconds=1,
                    stack_selection='process leader and D-state threads',
                    deadline_monotonic=deadline))
            elapsed_intervals = max(
                1, int((time.monotonic() - next_system) // .2) + 1)
            final['skipped_system_intervals'] += elapsed_intervals - 1
            next_system += elapsed_intervals * .2
            time.sleep(max(0, min(next_system, deadline) - time.monotonic()))
    except OverflowError as error:
        final.update(status='OUTPUT_TRUNCATED', truncated=True)
        final['failures'].append(str(error))
    except BaseException as error:
        final['failures'].append(repr(error))
    finally:
        final.update(finished=stamps(), returncode=result,
                     output_bytes_before_exit=evidence.bytes)
        evidence.write('exit.json', final, final=True)
        evidence.close()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--stop-file', type=Path, required=True)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--model-dir', type=Path, required=True)
    parser.add_argument('--max-seconds', type=float, default=360)
    args = parser.parse_args()
    if not 0 < args.max_seconds <= 360:
        parser.error('--max-seconds must be greater than 0 and at most 360')
    args.binary = args.binary.resolve(strict=True)
    args.model_dir = args.model_dir.resolve(strict=True)
    try:
        return observe(args)
    except BaseException as error:
        print(repr(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
