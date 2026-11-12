"""Read-only model identity for operator CLI; never requests gateway secrets."""
from pathlib import Path

from tools.serve_recovery import control, journal, ledger
from tools.serve_service import package as engine

IDENTITY = ('guardian_pid', 'guardian_start_ticks', 'controller_pid',
            'controller_start_ticks', 'controller_generation', 'model_pid',
            'model_start_ticks', 'model_generation')


def observe(desc, manifest):
    ledger.require(manifest.get('ingress_mode') == 'native' and manifest['version'] >= 8,
                   'native model management profile required')
    value = control.request(desc['service_id'], 'status', timeout=1)
    ledger.require(all(ledger.integer(value.get(k), minimum=1) for k in IDENTITY) and
        value.get('model_ready') is True and value.get('stop_requested') is False,
        'model management unavailable')
    pid, start = value['model_pid'], value['model_start_ticks']
    parent, parent_start = value['controller_pid'], value['controller_start_ticks']
    ledger.require(pid >= 2 and parent >= 2 and journal.process_start(pid) == start and
        journal.process_start(parent) == parent_start, 'model management identity changed')
    fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    metadata = Path(desc['package'])/'engine/manifest.json'
    ledger.require(int(fields[1]) == parent and engine.sha(metadata) == manifest['engine_manifest_sha256'],
                   'model management release changed')
    expected = engine.read(metadata)['files']['q4t']
    ledger.require(engine.sha(f'/proc/{pid}/exe') == expected and journal.process_start(pid) == start,
                   'model management executable changed')
    return value


def recheck(desc, initial):
    current = control.request(desc['service_id'], 'status', timeout=1)
    ledger.require(current.get('model_ready') is True and current.get('stop_requested') is False and
        all(current.get(k) == initial[k] for k in IDENTITY), 'model management generation changed')
    return current
