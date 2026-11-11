"""Explicit, resumable host-entry transfer after the normal package switch.

No deployment, model launch, stop, ledger reset or source overwrite occurs here.
Both immutable installations are retained. A reverse transfer uses a fresh
request after the ordinary rollback has selected the retained package.
"""
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path

from tools.serve_recovery import device_domain, host_artifact as artifact
from tools.serve_recovery import host_install as host, host_systemd as adapter, ledger

FORMAT = 'q4t-native-host-handoff-v1'
MARKER = 'host-handoff.json'
BOUND = 'host-handoff-bound.json'


def endpoint(directory, value):
    return dict(directory=str(Path(directory).absolute()), binding=adapter.binding(value))


def identity(value):
    return {key: value[key] for key in ('format', 'request_id', 'source', 'target', 'intent')}


def record_path(target, request):
    ledger.require(ledger.identifier(request), 'host handoff request UUID required')
    return Path(target)/'admin'/('handoff-'+request+'.json')


def validate(value):
    ledger.keys(value, ('format', 'request_id', 'source', 'target', 'intent', 'phase'))
    ledger.require(value['format'] == FORMAT and ledger.identifier(value['request_id']) and
        value['phase'] in ('prepared', 'complete'), 'invalid host handoff transaction')
    for role in ('source', 'target'):
        entry = value[role]; ledger.keys(entry, ('directory', 'binding'))
        ledger.require(type(entry['directory']) is str and 0 < len(entry['directory']) <= 4096 and
            Path(entry['directory']).is_absolute(), 'absolute host handoff directory required')
        binding = entry['binding']
        host.validate(dict(binding, format=host.FORMAT, phase='ready', enabled=False, revision=0))
    ledger.require(value['source']['directory'] != value['target']['directory'] and
        value['source']['binding']['installation_id'] != value['target']['binding']['installation_id'] and
        all(value['source']['binding'][key] == value['target']['binding'][key]
            for key in ('service_id', 'runtime_uid', 'runtime_gid', 'state', 'resource')),
        'host handoff service, state or resource identity differs')
    ledger.keys(value['intent'], ('native_enabled', 'trigger_enabled', 'source_trigger', 'target_trigger'))
    ledger.require(all(type(value['intent'][key]) is bool for key in value['intent']) and
        (not value['intent']['source_trigger'] or value['intent']['target_trigger']) and
        (not value['intent']['trigger_enabled'] or
            value['intent']['source_trigger'] and value['intent']['native_enabled']),
        'invalid host handoff admission intent')
    return value


def marker(value, role):
    return dict(format=FORMAT, request_id=value['request_id'], role=role,
        target=value['target']['directory'],
        identity_sha256=hashlib.sha256(ledger.canonical(identity(value))).hexdigest())


def read_selection(path, directory, installed):
    chosen = device_domain.read_record(path)
    ledger.keys(chosen, ('format', 'request_id', 'role', 'target', 'identity_sha256'))
    ledger.require(chosen['format'] == FORMAT and chosen['role'] in ('source', 'target') and
        type(chosen['target']) is str and 0 < len(chosen['target']) <= 4096 and
        Path(chosen['target']).is_absolute(), 'invalid host handoff marker')
    transaction = record_path(chosen['target'], chosen['request_id'])
    device_domain.secure_directory(transaction.parent, private=True)
    value = validate(device_domain.read_record(transaction))
    ledger.require(chosen == marker(value, chosen['role']) and
        value[chosen['role']] == endpoint(directory, installed), 'host handoff permanent binding differs')
    return value, chosen['role']


def selected(directory, installed):
    path = Path(directory)/'admin'/MARKER
    if not path.exists():
        ledger.require(not any(os.path.lexists(Path(directory)/'admin'/name) for name in
            (MARKER+'.pending', BOUND, BOUND+'.pending')),
                       'host handoff marker publication needs resume')
        # Completed transfers leave permanent per-installation history. A
        # removed marker is never treated as a fresh admission authority.
        ledger.require(not list((Path(directory)/'admin').glob('handoff-*.json')),
                       'host handoff marker is missing')
        return None
    ledger.require((Path(directory)/'admin'/BOUND).exists(), 'permanent host handoff binding is missing')
    read_selection(Path(directory)/'admin'/BOUND, directory, installed)
    return read_selection(path, directory, installed)


def admission(directory, installed, *, transaction=None):
    chosen = selected(directory, installed)
    if chosen is None: return None
    value, role = chosen
    if transaction is not None:
        ledger.require(identity(value) == identity(transaction) and role == 'target' and
                       value['phase'] == 'prepared', 'host handoff activation authority differs')
        return None
    if value['phase'] != 'complete': return 'host_handoff_pending'
    return 'host_entry_retired' if role == 'source' else None


def require_admission(directory, installed, *, transaction=None):
    reason = admission(directory, installed, transaction=transaction)
    ledger.require(reason is None, reason or 'host handoff admission denied')


def trigger(directory, installed):
    phase = host.trigger_phase(directory, installed)
    ledger.require(phase in ('absent', 'removed', 'registered'), 'host trigger needs reconciliation')
    if phase != 'registered': return None
    value = adapter.load(directory, installed); adapter.loaded_match(value)
    return value


def install_marker(value, role, installed):
    directory = Path(value[role]['directory']); path = directory/'admin'/MARKER
    expected = marker(value, role)
    permanent = directory/'admin'/BOUND
    if path.exists() and device_domain.read_record(path) == expected:
        ledger.require(permanent.exists(), 'permanent host handoff binding is missing')
        return
    if path.exists():
        previous = selected(directory, installed)
        ledger.require(previous[0]['phase'] == 'complete', 'another host handoff is pending')
    elif permanent.exists():
        ledger.require(device_domain.read_record(permanent) == expected and value['phase'] == 'prepared',
                       'published host handoff marker is missing')
    else:
        device_domain.write_record(permanent, expected)
    # A partial first marker is repairable only from this already durable
    # transaction with both original installations held exclusively.
    device_domain.write_record(path, expected)


def transfer(source, target, request_id):
    source, target = Path(source).absolute(), Path(target).absolute()
    ledger.require(source != target, 'distinct host installations required')
    path = record_path(target, request_id)
    with ExitStack() as locks:
        for directory in sorted((source, target)):
            locks.enter_context(host.exclusive(directory))
        old, new = host.load(source), host.load(target)
        for directory, installed in ((source, old), (target, new)):
            artifact.verify(directory, installed['artifact'])
        if path.exists():
            value = validate(device_domain.read_record(path))
            ledger.require(value['source'] == endpoint(source, old) and
                value['target'] == endpoint(target, new), 'host handoff replay identity differs')
            if value['phase'] == 'complete':
                return dict(complete=True, replayed=True, model_actions=False, core_intent_changed=False)
        else:
            require_admission(source, old)
            previous = selected(target, new)
            ledger.require(previous is None or previous[0]['phase'] == 'complete',
                           'target host handoff is pending')
            ledger.require(old['phase'] == new['phase'] == 'ready' and not new['enabled'],
                           'handoff requires ready entries and a disabled target')
            source_trigger, target_trigger = trigger(source, old), trigger(target, new)
            ledger.require(target_trigger is None or not target_trigger['enabled'],
                           'target trigger must be disabled before handoff')
            value = validate(dict(format=FORMAT, request_id=request_id,
                source=endpoint(source, old), target=endpoint(target, new), phase='prepared',
                intent=dict(native_enabled=old['enabled'],
                    trigger_enabled=bool(source_trigger and source_trigger['enabled']),
                    source_trigger=source_trigger is not None, target_trigger=target_trigger is not None)))
            # Verify the selected target before publishing any transfer gate.
            # This checker drops all privileges and never starts a model.
            host.check_resource(new); host.check_runtime(target, new)
            device_domain.write_record(path, value)
        ledger.require(old['phase'] == new['phase'] == 'ready', 'handoff installation is being removed')
        install_marker(value, 'source', old); install_marker(value, 'target', new)
        for directory, installed in ((source, old), (target, new)):
            chosen = selected(directory, installed)
            ledger.require(identity(chosen[0]) == identity(value), 'host handoff selection changed')
        # Recheck every external binding before touching either enabled flag.
        source_trigger, target_trigger = trigger(source, old), trigger(target, new)
        ledger.require((source_trigger is not None) == value['intent']['source_trigger'] and
            (target_trigger is not None) == value['intent']['target_trigger'], 'host trigger selection changed')
        host.check_resource(new); host.check_runtime(target, new)
        host.configure_locked(source, old, enabled=False)
        if source_trigger is not None:
            adapter.configure_locked(source, host.load(source), source_trigger, enabled=False)
        host.configure_locked(target, new, enabled=value['intent']['native_enabled'], transaction=value)
        if target_trigger is not None:
            adapter.configure_locked(target, host.load(target), target_trigger,
                enabled=value['intent']['trigger_enabled'], transaction=value)
        device_domain.write_record(path, dict(value, phase='complete'))
        return dict(complete=True, replayed=False, model_actions=False, core_intent_changed=False,
                    source_retired=True, native_enabled=value['intent']['native_enabled'],
                    trigger_enabled=value['intent']['trigger_enabled'])


def status(directory):
    with host.exclusive(directory) as directory:
        value = selected(directory, host.load(directory))
        return dict(selected=value is not None, transaction=None if value is None else value[0],
                    role=None if value is None else value[1], model_actions=False)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='operation', required=True)
    command = commands.add_parser('transfer')
    for name in ('source', 'target'): command.add_argument('--'+name, type=Path, required=True)
    command.add_argument('--request-id', required=True)
    command = commands.add_parser('status'); command.add_argument('--directory', type=Path, required=True)
    args = parser.parse_args(argv)
    value = status(args.directory) if args.operation == 'status' else transfer(args.source, args.target, args.request_id)
    print(json.dumps(value, ensure_ascii=False, sort_keys=True)); return 0
