"""One read-only process/cgroup snapshot for preparation checkpoints.

Call observe_process(pid, expected_start_ticks=...) before, during and after
preparation. The caller owns scheduling and output. No CUDA/NVML, payload/cache
reads, process control or cgroup writes occur here. Values are non-atomic and
cover only the cgroup hierarchy visible in the observer's mount namespace.
"""
import argparse
import errno
import json
import os
from pathlib import Path, PurePosixPath
import re
import time

MEMORY_FILES = (
    'memory.current', 'memory.max', 'memory.high', 'memory.low', 'memory.min',
    'memory.events', 'memory.stat', 'memory.swap.current', 'memory.swap.max',
)
MAX_READ_BYTES = 1 << 20
DIRECTORY_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW


def stamp():
    return dict(wall=time.time(), monotonic=time.monotonic())


def failure(error):
    code = getattr(error, 'errno', None)
    status = {
        errno.ENOENT: 'UNKNOWN_NOT_FOUND',
        errno.ESRCH: 'UNKNOWN_PROCESS_GONE',
        errno.EACCES: 'UNKNOWN_PERMISSION',
        errno.EPERM: 'UNKNOWN_PERMISSION',
        errno.ELOOP: 'UNKNOWN_UNSAFE_PATH',
        errno.ENOTDIR: 'UNKNOWN_UNSAFE_OR_NOT_DIRECTORY',
    }.get(code, 'UNKNOWN_READ_ERROR')
    return dict(status=status, raw=None, value=None,
                errno=code, error=str(error))


def read_at(directory, name):
    """Open a single fixed filename without following a symlink."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                     dir_fd=directory)
        with os.fdopen(fd, 'rb') as stream:
            data = stream.read(MAX_READ_BYTES + 1)
        if len(data) > MAX_READ_BYTES:
            raise ValueError('file exceeds bounded read size')
        return dict(status='OBSERVED', raw=data.decode(), value=None,
                    errno=None, error=None)
    except (OSError, UnicodeError, ValueError) as error:
        return failure(error)


def start_ticks(record, pid):
    if record['status'] != 'OBSERVED':
        return None
    try:
        raw = record['raw']
        if int(raw.split(' ', 1)[0]) != pid or '(' not in raw:
            raise ValueError('stat PID differs')
        fields = raw.rsplit(')', 1)[1].split()
        value = int(fields[19])
        if value < 0:
            raise ValueError('negative process start ticks')
        record['value'] = value
        return value
    except (ValueError, IndexError) as error:
        record.update(status='UNKNOWN_INVALID_STAT', error=str(error))
        return None


def membership(record):
    if record['status'] != 'OBSERVED':
        return None
    try:
        rows = [line[3:] for line in record['raw'].splitlines()
                if line.startswith('0::')]
        if len(rows) != 1 or not rows[0].startswith('/'):
            raise ValueError('one absolute cgroup-v2 membership required')
        path = rows[0]
        if ('\x00' in path or path.startswith('//') or any(part in ('.', '..')
                                 for part in path.split('/')) or
                (path != '/' and str(PurePosixPath(path)) != path)):
            raise ValueError('noncanonical or traversing membership path')
        record['value'] = path
        return path
    except ValueError as error:
        record.update(status='UNKNOWN_UNSAFE_MEMBERSHIP', error=str(error))
        return None


def memory_value(record, name):
    if record['status'] != 'OBSERVED':
        return record
    try:
        text = record['raw'].strip()
        if name in ('memory.events', 'memory.stat'):
            values = {}
            for line in text.splitlines():
                key, value = line.split()
                if key in values or int(value) < 0:
                    raise ValueError('duplicate/negative memory counter')
                values[key] = int(value)
            if not values:
                raise ValueError('empty memory counters')
            record['value'] = values
        elif text == 'max' and name in (
                'memory.max', 'memory.high', 'memory.swap.max'):
            record['value'] = 'max'
        else:
            value = int(text)
            if value < 0:
                raise ValueError('negative memory value')
            record['value'] = value
    except ValueError as error:
        record.update(status='UNKNOWN_INVALID_VALUE', value=None,
                      error=str(error))
    return record


def open_group(root_fd, parts):
    fd = os.dup(root_fd)
    try:
        for part in parts:
            next_fd = os.open(part, DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def group_snapshot(root_fd, root, parts):
    relative = '/' + '/'.join(parts)
    row = dict(cgroup=relative, path=str(root.joinpath(*parts)),
               started=stamp(), files={})
    try:
        fd = open_group(root_fd, parts)
    except OSError as error:
        row.update(status='UNKNOWN_GROUP', directory=failure(error),
                   finished=stamp())
        return row
    try:
        identity = os.fstat(fd)
        row['identity'] = dict(device=identity.st_dev, inode=identity.st_ino)
        row['files'] = {name: memory_value(read_at(fd, name), name)
                        for name in MEMORY_FILES}
        complete = all(value['status'] == 'OBSERVED'
                       for value in row['files'].values())
        row['status'] = 'OBSERVED' if complete else 'PARTIAL_UNKNOWN'
        try:
            after_fd = open_group(root_fd, parts)
            try:
                after = os.fstat(after_fd)
            finally:
                os.close(after_fd)
            if ((after.st_dev, after.st_ino) !=
                    (identity.st_dev, identity.st_ino)):
                row['status'] = 'UNKNOWN_GROUP_REPLACED'
        except OSError as error:
            row.update(status='UNKNOWN_GROUP_DISAPPEARED', after=failure(error))
    finally:
        os.close(fd)
    row['finished'] = stamp()
    return row


def mount_mapping(proc_root, root):
    """Record the observer's cgroup2 mount; hidden ancestors stay unknown."""
    try:
        fd = os.open(proc_root / 'self',
                     os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            record = read_at(fd, 'mountinfo')
        finally:
            os.close(fd)
        if record['status'] != 'OBSERVED':
            return record
        matches = []
        unescape = lambda text: re.sub(
            r'\\([0-7]{3})', lambda match: chr(int(match[1], 8)), text)
        for line in record['raw'].splitlines():
            before, after = line.split(' - ', 1)
            fields = before.split()
            if (after.split()[0] == 'cgroup2' and
                    unescape(fields[4]) == str(root)):
                matches.append(dict(mount_root=unescape(fields[3]),
                                    mountpoint=unescape(fields[4]), raw=line))
        if len(matches) != 1:
            raise ValueError('exactly one matching cgroup2 mount required')
        return dict(status='OBSERVED', **matches[0],
                    hidden_ancestors_not_observed=True)
    except (OSError, ValueError, IndexError) as error:
        return failure(error)


def observe_process(pid, expected_start_ticks=None, *, proc_root=Path('/proc'),
                    cgroup_root=Path('/sys/fs/cgroup')):
    """Return partial data explicitly; never substitute zeros for unknowns."""
    if type(pid) is not int or pid <= 0:
        raise ValueError('pid must be a positive integer')
    if expected_start_ticks is not None and (
            type(expected_start_ticks) is not int or expected_start_ticks < 0):
        raise ValueError('expected start ticks must be a nonnegative integer')
    proc_root, cgroup_root = Path(proc_root), Path(cgroup_root).absolute()
    result = dict(schema=1, pid=pid, expected_start_ticks=expected_start_ticks,
        observer_pid=os.getpid(), started=stamp(), status='UNKNOWN_PROCESS',
        identity_valid=False, data_complete=False, ancestors=[],
        limitations=['Sequential values are not an atomic memory snapshot.',
            'Only ancestors visible through the observer mount are read.',
            'Missing values are unknown, never zero or unlimited.',
            'This cannot establish a historical worker membership.'])
    try:
        process_fd = os.open(proc_root / str(pid), DIRECTORY_FLAGS)
    except OSError as error:
        result.update(process_error=failure(error), finished=stamp())
        return result
    try:
        before_stat = read_at(process_fd, 'stat')
        ticks = start_ticks(before_stat, pid)
        before_group = read_at(process_fd, 'cgroup')
        path = membership(before_group)
        result.update(start_ticks=ticks, stat_before=before_stat,
                      cgroup_before=before_group)
        expected_matches = (expected_start_ticks is None or
                            ticks == expected_start_ticks)
        if ticks is not None and path is not None and expected_matches:
            mapping = mount_mapping(proc_root, cgroup_root)
            result['mount'] = mapping
            if mapping['status'] == 'OBSERVED' and mapping['mount_root'] == '/':
                try:
                    root_fd = os.open(cgroup_root, DIRECTORY_FLAGS)
                    try:
                        parts = PurePosixPath(path).parts[1:]
                        result['ancestors'] = [group_snapshot(
                            root_fd, cgroup_root, parts[:depth])
                            for depth in range(len(parts), -1, -1)]
                    finally:
                        os.close(root_fd)
                except OSError as error:
                    result['hierarchy_error'] = failure(error)
            else:
                result['hierarchy_error'] = dict(status='UNKNOWN_MOUNT_MAPPING',
                    error='membership cannot map to an exposed root hierarchy')
        after_stat = read_at(process_fd, 'stat')
        after_ticks = start_ticks(after_stat, pid)
        after_group = read_at(process_fd, 'cgroup')
        after_path = membership(after_group)
        valid = (ticks is not None and ticks == after_ticks and path is not None
                 and path == after_path and expected_matches)
        complete = valid and bool(result['ancestors']) and all(
            row['status'] == 'OBSERVED' for row in result['ancestors'])
        result.update(stat_after=after_stat, cgroup_after=after_group,
            identity_valid=valid, data_complete=complete,
            status='OBSERVED' if complete else 'PARTIAL_UNKNOWN' if valid
            else 'UNKNOWN_PROCESS_IDENTITY_OR_MEMBERSHIP')
    finally:
        os.close(process_fd)
    result['finished'] = stamp()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pid', type=int, default=os.getpid())
    parser.add_argument('--expected-start-ticks', type=int)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = observe_process(args.pid, args.expected_start_ticks)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x') as stream:
        json.dump(result, stream, indent=2, allow_nan=False)
        stream.write('\n')
    return 0 if result['data_complete'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
