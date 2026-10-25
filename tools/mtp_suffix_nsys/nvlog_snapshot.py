"""Bounded NVLOG prefix snapshots, independent of trace qualification.

No collector, process, or GPU API is invoked. A snapshot preserves one twice-read
prefix of a still appendable file. Reconciliation inspects a stable final-file
observation; neither operation proves that all producers exited or no trace was
lost. Callers retain the original log, receipt, and final raw seal separately.
"""
import hashlib
import os
from pathlib import Path
import re
import stat
import time

MAX_BYTES = 64 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024


def _limit(value):
    if type(value) is not int or not 0 < value <= MAX_BYTES:
        raise ValueError('max_bytes must be an integer in 1..64MiB')
    return value


def _path(value):
    path = Path(value)
    if (not path.is_absolute() or os.path.normpath(str(path)) != str(path) or
            path.parent.resolve() != path.parent):
        raise ValueError('path must have a canonical absolute parent')
    return path


def _identity(info):
    return dict(device=info.st_dev, inode=info.st_ino)


def _metadata(info):
    return dict(**_identity(info), bytes=info.st_size, mtime_ns=info.st_mtime_ns,
                mode=stat.S_IMODE(info.st_mode))


def _open_regular(path):
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError('expected a regular non-symlink file')
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = _same_file(fd, path, before)
        return fd, info
    except BaseException:
        os.close(fd)
        raise


def _same_file(fd, path, identity, minimum=0):
    info, current = os.fstat(fd), path.lstat()
    if (not stat.S_ISREG(info.st_mode) or not stat.S_ISREG(current.st_mode) or
            _identity(info) != _identity(identity) or
            _identity(current) != _identity(identity) or info.st_size < minimum or
            current.st_size < minimum):
        raise ValueError('file identity changed or captured prefix shrank')
    return info


def _prefix(fd, length, *, destination_fd=None, collect=False):
    """Read exactly the frozen length with pread; never chase a growing tail."""
    digest, chunks, offset = hashlib.sha256(), [], 0
    while offset < length:
        chunk = os.pread(fd, min(CHUNK_BYTES, length - offset), offset)
        if not chunk:
            raise ValueError('file truncated during prefix read')
        digest.update(chunk)
        if collect:
            chunks.append(chunk)
        if destination_fd is not None:
            written = 0
            while written < len(chunk):
                count = os.write(destination_fd, chunk[written:])
                if count <= 0:
                    raise OSError('snapshot write made no progress')
                written += count
        offset += len(chunk)
    return digest.hexdigest(), b''.join(chunks) if collect else None


def _unlink_owned(path, identity):
    try:
        current = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISREG(current.st_mode) and _identity(current) == _identity(identity):
        path.unlink()


def snapshot(path, destination, max_bytes=MAX_BYTES):
    """Create an exclusive read-only copy and return its JSON-compatible receipt.

    The initial size fixes the capture budget. Tail appends may change mtime and
    size; two identical prefix hashes plus pinned FD/path identity are required.
    Source data is never modified. On failure only our own partial copy is removed.
    Read-only mode is an accidental-write guard, not tamper-proof storage.
    """
    maximum = _limit(max_bytes)
    path, destination = _path(path), _path(destination)
    if path == destination:
        raise ValueError('source and snapshot must be different paths')
    started = time.monotonic_ns()
    source_fd, initial = _open_regular(path)
    copy_fd, copy_identity, success = None, None, False
    try:
        length = initial.st_size
        if not 0 < length <= maximum:
            raise ValueError('source prefix must be nonempty and within the fixed limit')
        copy_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                          os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, 0o600)
        copy_identity = os.fstat(copy_fd)
        first, _ = _prefix(source_fd, length, destination_fd=copy_fd)
        middle = _same_file(source_fd, path, initial, length)
        second, _ = _prefix(source_fd, length)
        final = _same_file(source_fd, path, initial, length)
        if first != second:
            raise ValueError('source captured prefix was rewritten')
        os.fsync(copy_fd)
        os.fchmod(copy_fd, 0o444)
        os.fsync(copy_fd)
        copied = _same_file(copy_fd, destination, copy_identity, length)
        if copied.st_size != length:
            raise ValueError('snapshot length changed while writing')
        # Read back the independent copy, not just the source-side write buffer.
        verify_fd, verify_info = _open_regular(destination)
        try:
            copied_digest, _ = _prefix(verify_fd, length)
            verified = _same_file(verify_fd, destination, verify_info, length)
            if (not _metadata(verified) == _metadata(copied) ==
                    _metadata(destination.lstat()) or copied_digest != first):
                raise ValueError('snapshot readback identity/content changed')
        finally:
            os.close(verify_fd)
        _same_file(source_fd, path, initial, length)
        directory_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        receipt = dict(schema=1, kind='immutable_nvlog_captured_prefix',
            source=dict(path=str(path), identity=_identity(initial),
                        before=_metadata(initial), after_first_read=_metadata(middle),
                        after_second_read=_metadata(final)),
            snapshot=dict(path=str(destination), **_metadata(copied), sha256=first),
            captured_bytes=length, captured_sha256=first, max_bytes=maximum,
            source_prefix_rechecks=2, started_monotonic_ns=started,
            finished_monotonic_ns=time.monotonic_ns(), trace_complete=False,
            qualification='NOT_EVALUATED',
            scope='Two identical pinned prefix reads and independent readback; tail append allowed; no producer-exit or trace-completeness assertion.')
        success = True
        return receipt
    finally:
        if copy_fd is not None:
            os.close(copy_fd)
        os.close(source_fd)
        if not success and copy_identity is not None:
            _unlink_owned(destination, copy_identity)


def _stable_bytes(path, maximum, expected=None):
    fd, initial = _open_regular(path)
    try:
        if not 0 < initial.st_size <= maximum:
            raise ValueError('stable log observation exceeds its fixed limit or is empty')
        digest, data = _prefix(fd, initial.st_size, collect=True)
        second, _ = _prefix(fd, initial.st_size)
        final = _same_file(fd, path, initial, initial.st_size)
        row = dict(path=str(path), **_metadata(final), sha256=digest)
        if (not _metadata(initial) == _metadata(final) == _metadata(path.lstat()) or
                digest != second):
            raise ValueError('final log observation changed during reconciliation')
        if expected is not None and row != expected:
            raise ValueError('immutable snapshot identity/content changed')
        return row, data
    finally:
        os.close(fd)


_LOSS = re.compile(r'(?:CUPTI dropped|Number of incomplete CUPTI events dropped:)\s*[1-9]\d*|'
                   r'Could not parse|Couldn.t allocate CUPTI buffers', re.I)
_HEADER = re.compile(r'^[A-Z]\d\d:\d\d:\d\d\.')


def _records(data):
    records, offset = [], 0
    for number, line in enumerate(data.splitlines(keepends=True), 1):
        records.append(dict(line=number, byte_start=offset, byte_end=offset + len(line),
                            complete=line.endswith(b'\n'),
                            text=line.decode('utf-8', errors='replace').rstrip('\r\n')))
        offset += len(line)
    return records


def _target(records, pid):
    pattern = re.compile(r'\bpid=\s*' + str(pid) + r'\s')
    rows = [row for row in records if row['complete'] and pattern.search(row['text'])]
    # Canonical complete-line digest compares target records, not global byte offsets.
    canonical = ''.join(row['text'] + '\n' for row in rows).encode('utf-8')
    flags = [int(match.group(1)) for row in rows
             if (match := re.search(r'Flushing activity records with flag:\s*(\d+)', row['text']))]
    return dict(complete_line_count=len(rows), complete_lines_sha256=hashlib.sha256(canonical).hexdigest(),
        flush_flags=flags,
        cuda_done=[row['text'] for row in rows if 'ReportCUDADone: DONE.' in row['text']],
        nvtx_terminal=[row['text'] for row in rows if re.search(
            r'NVTXFinish|NVTX profiling (?:stopped|finished)|ReportNVTX.*DONE', row['text'])],
        explicit_loss=[row['text'] for row in rows if _LOSS.search(row['text'])])


def reconcile(receipt, final_path, target_pid, max_bytes=MAX_BYTES):
    """Compare the preserved prefix and one stable final observation, without a gate.

    Added target rows, error messages and positive loss findings remain explicit.
    Equality is evidence about this snapshot only; warnings are never exempted.
    """
    maximum = _limit(max_bytes)
    if (not isinstance(receipt, dict) or receipt.get('schema') != 1 or
            receipt.get('kind') != 'immutable_nvlog_captured_prefix' or
            type(target_pid) is not int or target_pid <= 0):
        raise ValueError('invalid snapshot receipt or target PID')
    length = receipt['captured_bytes']
    captured_limit = _limit(receipt['max_bytes'])
    if type(length) is not int or not 0 < length <= min(maximum, captured_limit):
        raise ValueError('captured length exceeds the reconciliation limit')
    path = _path(final_path)
    if str(path) != receipt['source']['path']:
        raise ValueError('final log must be the original pinned source path')
    copy_path = _path(receipt['snapshot']['path'])
    copied, prefix = _stable_bytes(copy_path, maximum, receipt['snapshot'])
    if (copied['mode'] & 0o222 or copied['bytes'] != length or
            copied['sha256'] != receipt['captured_sha256']):
        raise ValueError('snapshot is writable or disagrees with the captured prefix')
    final, data = _stable_bytes(path, maximum)
    if ({key: final[key] for key in ('device', 'inode')} != receipt['source']['identity'] or
            len(data) < length or hashlib.sha256(data[:length]).hexdigest() != copied['sha256']):
        raise ValueError('final identity or captured prefix changed')
    old_records, records = _records(prefix), _records(data)
    before, after = _target(old_records, target_pid), _target(records, target_pid)
    appended = [row for row in records if row['byte_end'] > length]
    error_rows, continuation, error_header = [], False, None
    for row in records:
        if _HEADER.match(row['text']):
            continuation = bool(re.match(r'^[EF]\d\d:', row['text']))
            error_header = row['line'] if continuation else None
        if continuation and row['byte_end'] > length:
            error_rows.append(dict(row, error_header_line=error_header))
    return dict(schema=1, kind='bounded_nvlog_prefix_reconciliation',
        captured_snapshot=copied, final_observation=final, exact_prefix=True,
        target_pid=target_pid, snapshot_target=before, final_target=after,
        target_records_equal=before == after, appended_bytes=len(data) - length,
        appended_or_boundary_completed_lines=len(appended),
        snapshot_has_partial_last_line=bool(prefix and not prefix.endswith(b'\n')),
        appended_loss=[row for row in appended if _LOSS.search(row['text'])],
        appended_errors=error_rows,
        appended_warnings=[row for row in appended if re.match(r'^W\d\d:', row['text'])],
        final_has_partial_last_line=bool(data and not data.endswith(b'\n')),
        warning_waiver=False, trace_complete=False, qualification='NOT_EVALUATED',
        scope='Exact preserved prefix and stable final observation only; appended diagnostics remain raw evidence, never an automatic audit upgrade or trace qualification.')
