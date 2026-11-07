"""Best-effort observation I/O, never ownership or recovery authorization.

Only returned I/O/serialization failures are isolated. This does not bound an
uninterruptible kernel write. Fixed channels and backoff bound memory and retry
work; raw error text and payloads are never retained in the fault ledger.
"""
import threading
import time

from tools.evalscope.bounded_log import BoundedLog
from tools.serve_service import package as pkg

CHANNELS = frozenset({'backend_log', 'events', 'status', 'monitor_snapshot',
                      'nvml_log', 'guard_events'})
RETRY_SECONDS = 60


class Observations:
    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.channels = {}

    def snapshot(self):
        with self.lock:
            return {key: dict(value) for key, value in self.channels.items()}

    @property
    def had_gaps(self):
        return any(row['failed_operations'] for row in self.snapshot().values())

    def perform(self, channel, operation, *, size=0):
        # dropped_bytes counts input to unsuccessful/skipped log operations.
        # A partial write may have persisted a prefix: this is a conservative
        # unconfirmed-byte count, not an exact measurement of disk data loss.
        if channel not in CHANNELS:
            raise ValueError('unknown observation channel')
        now = self.clock()
        with self.lock:
            row = self.channels.setdefault(channel, dict(state='AVAILABLE',
                failed_operations=0, skipped_operations=0, dropped_bytes=0,
                first_failure_monotonic=None, last_failure_monotonic=None,
                last_success_monotonic=None, next_attempt_monotonic=0,
                error_type=None, errno=None))
            if now < row['next_attempt_monotonic']:
                row['skipped_operations'] += 1
                row['dropped_bytes'] += size
                return False
        try:
            operation()
        except (OSError, ValueError, TypeError, RuntimeError) as error:
            with self.lock:
                if row['first_failure_monotonic'] is None:
                    row['first_failure_monotonic'] = now
                row.update(state='UNAVAILABLE',
                    failed_operations=row['failed_operations']+1,
                    dropped_bytes=row['dropped_bytes']+size,
                    last_failure_monotonic=now,
                    next_attempt_monotonic=now+RETRY_SECONDS,
                    error_type=type(error).__name__[:64],
                    errno=error.errno if isinstance(error, OSError) else None)
            return False
        with self.lock:
            row.update(state='AVAILABLE', last_success_monotonic=now,
                       next_attempt_monotonic=0)
        return True

    def save(self, path, value, channel):
        return self.perform(channel, lambda: pkg.save(path, value))

    def log(self, path, maximum, channel):
        return Log(self, path, maximum, channel)


class Log:
    def __init__(self, observations, path, maximum, channel):
        self.observations, self.channel = observations, channel
        self.writer = BoundedLog(path, maximum)

    def write(self, text):
        self.observations.perform(self.channel, lambda: self.writer.write(text),
                                  size=len(text.encode('utf-8')))
        return len(text)

    def flush(self):
        pass  # Each successful BoundedLog write has closed its file.
