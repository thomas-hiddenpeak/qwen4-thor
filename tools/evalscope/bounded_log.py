"""Optional two-generation log for long-lived lease monitors.

Validation keeps the existing complete log unless a limit is explicit.
Rotation is retention, not a claim of lossless historical evidence.
"""
import contextlib
import os
from pathlib import Path


class BoundedLog:
    def __init__(self, path, maximum):
        if type(maximum) is not int or maximum <= 0:
            raise ValueError('positive byte limit required')
        self.path, self.maximum = Path(path), maximum

    def write(self, text):
        data = text.encode('utf-8')
        while data:
            size = self.path.stat().st_size if self.path.exists() else 0
            if size == self.maximum:
                os.replace(self.path, self.path.with_name(self.path.name+'.previous'))
                size = 0
            if size > self.maximum:
                raise RuntimeError('existing log exceeds fixed limit')
            chunk, data = data[:self.maximum-size], data[self.maximum-size:]
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT |
                         os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'ab') as stream:
                stream.write(chunk)
        return len(text)

    def flush(self):
        # Each write has already closed its file. This matches the guard's
        # flush visibility; neither the old nor new stream promises fsync.
        pass


@contextlib.contextmanager
def open_log(path, mode, maximum=None):
    if maximum is None:
        with Path(path).open(mode) as stream:
            yield stream
    else:
        if mode not in ('a', 'w'):
            raise ValueError('unsupported log mode')
        if mode == 'w' and Path(path).exists():
            raise FileExistsError(path)
        yield BoundedLog(path, maximum)
