"""Independent host-memory tripwire, not a reservation or physical RAM cap."""
from pathlib import Path
import threading
import time


def parse(raw):
    if len(raw) > 16384 or not raw.endswith('\n'):
        raise ValueError('incomplete memory observation')
    fields = {}
    for line in raw.splitlines():
        key, _, rest = line.partition(':')
        if key not in ('MemTotal', 'MemAvailable'):
            continue
        parts = rest.split()
        if (key in fields or len(parts) != 2 or parts[1] != 'kB' or
                not parts[0].isascii() or not parts[0].isdigit()):
            raise ValueError('invalid memory observation')
        fields[key] = int(parts[0])*1024
    if (len(fields) != 2 or not fields['MemTotal'] or
            not 0 <= fields['MemAvailable'] <= fields['MemTotal']):
        raise ValueError('missing or contradictory memory observation')
    return dict(total_bytes=fields['MemTotal'],
                available_bytes=fields['MemAvailable'])


def read():
    # Fixed kernel source. No environment/CLI override of observation inputs.
    with Path('/proc/meminfo').open(encoding='ascii') as stream:
        return parse(stream.read(16385))


class Watchdog:
    """Latch confirmed critical pressure, not a runtime observation gap.

    The independent thread can send SIGTERM even while the HTTP probe blocks.
    Supervision subsequently drains/reaps the whole owned group. Reader and
    clock arguments are internal CPU test seams, never package configuration.
    """
    def __init__(self, settings, notify, *, reader=read, now=time.monotonic):
        self.cfg, self.notify = settings, notify
        self.reader, self.now = reader, now
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.tripped = threading.Event()
        self.owner = None
        self.signalled_owner = None
        self.low_since = self.unknown_since = None
        self.state = dict(state='UNKNOWN', samples=0, failures=0,
            floor_bytes=settings['critical_memory_mib']*1024**2,
            reason=None, signal_sent=False)
        self.thread = None

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def _signal(self):
        # Serialize attachment with trip notification; notify uses a pidfd and
        # boot/start identity, never a bare historical PID or process name.
        if self.owner is not None and self.owner != self.signalled_owner:
            self.signalled_owner = self.owner
            try:
                self.state['signal_sent'] = self.notify(self.owner)
            except Exception as error:
                self.state['signal_error'] = repr(error)

    def attach(self, owner):
        with self.lock:
            self.owner = owner
            if self.tripped.is_set():
                self._signal()

    def sample(self, *, startup=False):
        with self.lock:
            if self.tripped.is_set():
                return
            now = self.now()
            self.state.update(samples=self.state['samples']+1,
                              sampled_monotonic=now)
            try:
                value = self.reader()
                total, available = value['total_bytes'], value['available_bytes']
                if (type(total) is not int or type(available) is not int or
                        total <= 0 or not 0 <= available <= total):
                    raise ValueError('invalid memory sample')
            except Exception:
                self.state.update(state='UNKNOWN', total_bytes=None,
                    available_bytes=None, failures=self.state['failures']+1,
                    reason='memory_observation_unavailable')
                self.low_since = None
                if self.unknown_since is None:
                    self.unknown_since = now
                # New allocation requires a reliable startup sample. During
                # service an unavailable auxiliary reader cannot kill the
                # healthy model. Native admission independently checks RAM for
                # new work; automatic profile selection must wait for evidence.
                if not startup:
                    return
                reason = 'memory_observation_unavailable'
            else:
                self.unknown_since = None
                low = available < self.state['floor_bytes']
                self.state.update(state='LOW' if low else 'OPEN', reason=None,
                                  **value)
                if not low:
                    self.low_since = None
                elif self.low_since is None:
                    self.low_since = now
                reason = ('critical_memory_pressure' if low and (startup or
                    now-self.low_since >= self.cfg['memory_low_seconds'])
                    else None)
            if reason:
                self.state.update(state='TRIPPED', reason=reason,
                                  tripped_monotonic=now)
                self.tripped.set()
                self._signal()

    def check(self):
        if self.tripped.is_set():
            raise RuntimeError(self.snapshot()['reason'])

    def start(self):
        self.sample(startup=True)
        self.check()
        def poll():
            while not self.done.wait(self.cfg['memory_poll_seconds']):
                self.sample()
                if self.tripped.is_set():
                    break
        self.thread = threading.Thread(target=poll, name='q4t-memory-watchdog',
                                       daemon=True)
        self.thread.start()

    def close(self):
        self.done.set()
        if self.thread is not None:
            self.thread.join(2)
            if self.thread.is_alive():
                raise RuntimeError('memory observation thread did not exit')
