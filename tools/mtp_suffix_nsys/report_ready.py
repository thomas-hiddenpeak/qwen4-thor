"""One bounded Nsight report callback; no collector or target operations."""
import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys
import time

ENVIRONMENT_KEYS = ('NSYS_SESSION_NAME', 'NSYS_CALLBACK_NAME', 'NSYS_REPORT_PATH')
MAX_RECEIPT_BYTES = 65536


def absolute_path(value):
    text = str(value)
    if (not Path(text).is_absolute() or os.path.normpath(text) != text or
            any(c in text for c in ('\0', '\n', '%'))):
        raise ValueError('callback paths must be normalized absolute literal paths')
    return Path(text)


def report_stat(path):
    """Pin the final regular file; report presence does not prove trace coverage."""
    path = absolute_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        current = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_size <= 0 or
                (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)):
            raise ValueError('report must be one nonempty regular file')
        return dict(path=str(path), device=info.st_dev, inode=info.st_ino,
                    bytes=info.st_size, mtime_ns=info.st_mtime_ns)
    finally:
        os.close(fd)


def publish_exclusive(path, value):
    """Publish complete JSON atomically, without replacing an earlier receipt."""
    path = absolute_path(path)
    payload = (json.dumps(value, ensure_ascii=False, sort_keys=True) + '\n').encode()
    if len(payload) > MAX_RECEIPT_BYTES:
        raise ValueError('callback receipt exceeds fixed size bound')
    temporary = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                 os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path, follow_symlinks=False)
    finally:
        temporary.unlink()


def callback(session, nonce, output, report, environment=None):
    environment = os.environ if environment is None else environment
    observed = {key: environment.get(key) for key in ENVIRONMENT_KEYS}
    receipt = dict(schema=1, kind='nsys_after_report_ready', session=session,
        nonce=nonce, expected_report=str(report), environment=observed,
        callback_pid=os.getpid(), observed_monotonic_ns=time.monotonic_ns(),
        report=None, failure=None,
        scope='Run/session/nonce binding; not hostile same-UID authentication or trace completeness')
    try:
        if not re.fullmatch(r'[a-f0-9]{32}', nonce):
            raise ValueError('invalid callback nonce')
        report = absolute_path(report)
        if observed['NSYS_SESSION_NAME'] != session:
            raise ValueError('callback session mismatch')
        # The public help documents this variable, but not its literal value.
        if not isinstance(observed['NSYS_CALLBACK_NAME'], str) or not observed['NSYS_CALLBACK_NAME'].strip():
            raise ValueError('callback name missing')
        if observed['NSYS_REPORT_PATH'] != str(report):
            raise ValueError('callback report path mismatch')
        receipt['report'] = report_stat(report)
    except Exception as error:
        receipt['failure'] = repr(error)
    publish_exclusive(output, receipt)
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session', required=True)
    parser.add_argument('--nonce', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--report', required=True)
    args = parser.parse_args(argv)
    try:
        result = callback(args.session, args.nonce, args.output, args.report)
    except Exception as error:
        print('[report-ready-callback-error] ' + repr(error), file=sys.stderr)
        return 1
    if result['failure']:
        print('[report-ready-callback-error] ' + result['failure'], file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
