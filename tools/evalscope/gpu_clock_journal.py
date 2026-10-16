"""Durable diagnostic clock transaction, with injected IO only.

The supervisor must serialize prepare/change/recover under the GPU lease and
arrange independent recovery. This module neither acquires the lease nor writes
sysfs. Use the guard's .recovery-required path as the journal. Never remove an
unreadable journal automatically. Hardware writes are preceded by durable intent
so an independent process can recover after either side of a write syscall.
"""
import json
import os
from pathlib import Path
import tempfile

from gpu_clock_range import steps


def persist(path, value, create=False):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        if create:
            os.link(temporary, path)  # Refuse an existing recovery marker.
        else:
            os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class ClockJournal:
    def __init__(self, path, read, write, identity, available):
        self.path = Path(path)
        self.read, self.write = read, write
        self.identity = identity  # Frozen boot ID and real device identity.
        self.available = sorted(set(available))

    def prepare(self, expected, target):
        expected, target = list(expected), list(target)
        steps(expected, target)
        if any(x not in self.available for x in expected + target):
            raise ValueError('unsupported frequency')
        if target[1] > expected[1]:
            raise ValueError('diagnostic cannot exceed original maximum')
        if list(self.read()) != expected:
            raise RuntimeError('initial range differs from frozen identity')
        persist(self.path, dict(version=1, identity=self.identity,
                               available=self.available, original=expected,
                               target=target, current=expected, pending=None),
                create=True)

    def load(self):
        state = json.loads(self.path.read_text())
        if (state['version'] != 1 or state['identity'] != self.identity or
                state['available'] != self.available):
            raise RuntimeError('recovery identity changed')
        for name in ('original', 'target', 'current'):
            values = state[name]
            steps(values, values)
            if any(x not in self.available for x in values):
                raise ValueError('invalid journal frequency')
        if state['target'][1] > state['original'][1]:
            raise ValueError('invalid journal target')
        pending = state['pending']
        if pending is not None:
            if pending['field'] not in ('min_freq', 'max_freq'):
                raise ValueError('invalid pending field')
            predicted = state['current'].copy()
            predicted[0 if pending['field'] == 'min_freq' else 1] = pending['value']
            steps(predicted, predicted)
            if (predicted != pending['after'] or
                    pending['value'] not in self.available or
                    predicted[1] > state['original'][1]):
                raise ValueError('invalid pending write')
        return state

    def transition(self, restoring=False):
        state = self.load()
        observed = list(self.read())
        allowed = [state['current']]
        if state['pending'] is not None:
            allowed.append(state['pending']['after'])
        if observed not in allowed:
            raise RuntimeError('range changed outside recorded transaction')
        # Resolve whether an interrupted syscall took effect before planning.
        state.update(current=observed, pending=None)
        persist(self.path, state)
        target = state['original'] if restoring else state['target']
        for field, value in steps(observed, target):
            if list(self.read()) != state['current']:
                raise RuntimeError('range changed before write')
            after = state['current'].copy()
            after[0 if field == 'min_freq' else 1] = value
            state['pending'] = dict(field=field, value=value, after=after)
            persist(self.path, state)  # Durable before any side effect.
            self.write(field, value)
            if list(self.read()) != after:
                raise RuntimeError('clock write readback mismatch')
            state.update(current=after, pending=None)
            persist(self.path, state)
        if list(self.read()) != target:
            raise RuntimeError('final clock range mismatch')
        if restoring:
            self.path.unlink()
            sync_directory(self.path.parent)

    def apply(self):
        self.transition()

    def restore(self):
        self.transition(restoring=True)
