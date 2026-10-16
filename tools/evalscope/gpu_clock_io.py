"""Restricted GPC sysfs IO for a leased, independently recovered diagnostic.

Only the two GPC range files can be written. No governor, OC protection,
CPU, NVD, EMC, runtime-power or persistence configuration is modified.
Callers must arrange the GPU lease and independent recovery before use.
"""
import json
from pathlib import Path
import subprocess

BASE = Path('/sys/class/devfreq')
GPC = BASE / 'gpu-gpc-0'


def snapshot():
    identity = {'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                'devices': {}}
    for name in ('gpu-gpc-0', 'gpu-nvd-0', 'bwmgr'):
        path = BASE / name
        fields = {'device': str(path.resolve(strict=True)),
                  'governor': (path / 'governor').read_text().strip(),
                  'available': [int(x) for x in
                                (path / 'available_frequencies').read_text().split()]}
        if name != 'gpu-gpc-0':
            fields['range'] = [int((path / field).read_text())
                               for field in ('min_freq', 'max_freq')]
        identity['devices'][name] = fields
    return {'identity': identity,
            'range': [int((GPC / field).read_text())
                      for field in ('min_freq', 'max_freq')],
            'requested_current': int((GPC / 'cur_freq').read_text())}


class GpcIO:
    def __init__(self, identity, original_max, journal_path):
        self.identity = identity
        self.original_max = original_max
        self.journal_path = Path(journal_path)
        self.available = identity['devices']['gpu-gpc-0']['available']
        if original_max not in self.available:
            raise ValueError('unsupported original maximum')

    def read(self):
        observed = snapshot()
        if observed['identity'] != self.identity:
            raise RuntimeError('clock interface identity changed')
        return tuple(observed['range'])

    def write(self, field, value):
        if field not in ('min_freq', 'max_freq'):
            raise ValueError('only GPC min/max writes permitted')
        if type(value) is not int or value not in self.available:
            raise ValueError('unsupported frequency')
        if value > self.original_max:
            raise ValueError('cannot exceed original maximum')
        state = json.loads(self.journal_path.read_text())
        pending = state['pending']
        if (state['identity'] != self.identity or
                state['original'][1] != self.original_max or
                pending is None or pending['field'] != field or
                pending['value'] != value):
            raise RuntimeError('write lacks matching recovery intent')
        current = list(self.read())
        if current != state['current']:
            raise RuntimeError('clock changed after durable write intent')
        after = current.copy()
        after[0 if field == 'min_freq' else 1] = value
        if after != pending['after'] or not 0 < after[0] <= after[1] <= self.original_max:
            raise RuntimeError('invalid predicted range')
        # Fixed executable and path; stdin is a validated decimal integer.
        subprocess.run(['sudo', '-n', '/usr/bin/tee', str(GPC / field)],
                       input=str(value) + '\n', text=True, check=True,
                       capture_output=True, timeout=5)
