"""Root-owned association of a device fence with an existing resource leaf.

This module grants no GPU access. Actual filter attachment and one-use device
admission remain in device_domain and the native helper. The association is
permanent so deleting administrative history cannot make teardown appear safe.
"""
from contextlib import contextmanager
import os
from pathlib import Path

from tools.serve_recovery import access_store, device_domain, device_store, ledger
from tools.serve_recovery import resource_install as resources, resource_kernel as kernel, store

CONFIG_FORMAT = 'q4t-device-resource-domain-v2'
FORMAT = 'q4t-resource-device-association-v1'
ASSOCIATION = 'device-association.json'
PERMANENT = 'device-association-bound.json'
FENCE = 'resource-fence.json'


def validate_selection(value):
    ledger.keys(value, ('directory', 'domain_id', 'policy'))
    ledger.require(type(value['directory']) is str and 0 < len(value['directory']) <= 4096 and
        Path(value['directory']).is_absolute() and ledger.identifier(value['domain_id']),
        'invalid device resource selection')
    selected = value['policy']['service']
    ledger.require(value['policy'] == kernel.policy(selected['memory_high_bytes']//1024**2,
        selected['memory_max_bytes']//1024**2, selected['pids_max']), 'device resource policy differs')
    return value


def selection(directory, service_id, uid, gid):
    with resources.exclusive(directory) as directory:
        value = resources.load(directory)
        ledger.require(value['phase'] == 'ready' and value['service_id'] == service_id and
            value['runtime_uid'] == uid and value['runtime_gid'] == gid,
            'device resource service or runtime identity differs')
        ledger.require(resources.read_public(directory) == resources.public_value(value),
                       'device resource publication differs')
        with kernel.Scope(value['record']) as scope:
            ledger.require(scope.empty(), 'device resource selection requires an empty service leaf')
        return dict(directory=str(directory), domain_id=value['domain_id'], policy=value['policy'])


def association(directory, config):
    ledger.require(config['format'] == CONFIG_FORMAT, 'resource-bound device format required')
    return dict(format=FORMAT, device_directory=str(Path(directory).absolute()),
        configuration_sha256=ledger.digest(config), configuration=config)


def matches(config, value):
    ledger.require(all(value[name] == config[name] for name in
        ('service_id', 'runtime_uid', 'runtime_gid')) and
        value['domain_id'] == config['resource']['domain_id'] and
        value['policy'] == config['resource']['policy'], 'original device resource installation differs')


def lifecycle(directory, config):
    value = device_domain.read_record(Path(directory)/FENCE)
    ledger.keys(value, ('format', 'configuration_sha256', 'phase', 'boot_id', 'revision', 'initialized'))
    ledger.require(value['format'] == FORMAT and value['configuration_sha256'] == ledger.digest(config) and
        value['phase'] in ('idle', 'arming', 'armed', 'disarming') and
        ledger.identifier(value['boot_id']) and ledger.integer(value['revision']) and
        type(value['initialized']) is bool,
        'device resource fence history differs')
    return value


def initialize(directory, config):
    """Called only with the device admin lock, before any resource-bound arm."""
    expected = association(directory, config)
    with resources.exclusive(config['resource']['directory']) as resource_directory:
        current = resources.load(resource_directory); matches(config, current)
        ledger.require(current['phase'] == 'ready', 'device resource installation must be ready')
        path = resource_directory/'admin'/ASSOCIATION
        permanent = resource_directory/'admin'/PERMANENT
        if not (Path(directory)/FENCE).exists():
            ledger.require(not any(os.path.lexists(p) for p in (path, permanent,
                permanent.with_name(PERMANENT+'.pending'),
                path.with_name(ASSOCIATION+'.published'),
                path.with_name(ASSOCIATION+'.published.pending'))) and
                not any(os.path.lexists(Path(directory)/name) for name in
                    ('fence.json', 'qualification.json', 'removal.json')),
                'published device fence history is missing')
            binding = access_store.binding(directory, config['service_id'])
            with device_store.open_bound(binding) as journal:
                policy = journal.head['state']['policy']
                ledger.require(policy['total'] == 0 and policy['qualification'] is None,
                               'used device budget cannot bootstrap resource history')
            device_domain.write_record(Path(directory)/FENCE, dict(format=FORMAT,
                configuration_sha256=ledger.digest(config), phase='idle',
                boot_id=store.stamp().boot_id, revision=0, initialized=False))
        state = lifecycle(directory, config)
        published = path.with_name(ASSOCIATION+'.published')
        for selected in (permanent, path, published):
            if selected.exists():
                ledger.require(device_domain.read_record(selected) == expected,
                               'resource already belongs to another device installation')
            else:
                ledger.require(not state['initialized'], 'published resource device association is missing')
                device_domain.write_record(selected, expected)
        from tools.serve_recovery import cold_requests
        cold_requests.initialize(directory, config, first=not state['initialized'])
        if not state['initialized']:
            ledger.require(state['phase'] == 'idle' and state['revision'] == 0,
                           'unpublished resource fence history was already used')
            device_domain.write_record(Path(directory)/FENCE,
                dict(state, initialized=True, revision=1))
        return expected


def check(directory, config, resource_directory, value):
    matches(config, value)
    expected = association(directory, config)
    for name in (ASSOCIATION, PERMANENT, ASSOCIATION+'.published'):
        ledger.require(device_domain.read_record(Path(resource_directory)/'admin'/name) == expected,
                       'device resource permanent association differs')
    ledger.require(lifecycle(directory, config)['initialized'], 'finish device resource association first')


def change(directory, config, phase):
    ledger.require(phase in ('idle', 'arming', 'armed', 'disarming'), 'invalid device fence phase')
    value = lifecycle(directory, config)
    ledger.require(value['initialized'], 'finish device resource association first')
    if value['phase'] == phase and value['boot_id'] == store.stamp().boot_id: return value
    value = dict(value, phase=phase, boot_id=store.stamp().boot_id, revision=value['revision']+1)
    device_domain.write_record(Path(directory)/FENCE, value)
    return value


@contextmanager
def scope(directory, config):
    with resources.exclusive(config['resource']['directory']) as resource_directory:
        value = resources.load(resource_directory); check(directory, config, resource_directory, value)
        ledger.require(value['phase'] == 'ready' and
            resources.read_public(resource_directory) == resources.public_value(value),
            'device resource entry has been revoked')
        with kernel.Scope(value['record']) as held:
            ledger.require(held.empty(), 'original service resource leaf is populated')
            group = held.root/'service'
            device_domain.empty(group)
            yield group, held.fds['service']


def removal_allowed(resource_directory, value):
    """Resource admin lock held; failure never mutates resource admission."""
    admin = Path(resource_directory)/'admin'
    names = (ASSOCIATION, PERMANENT, ASSOCIATION+'.published')
    if not any(os.path.lexists(admin/name) or os.path.lexists(admin/(name+'.pending')) for name in names):
        return
    selected = device_domain.read_record(admin/ASSOCIATION)
    ledger.keys(selected, ('format', 'device_directory', 'configuration_sha256', 'configuration'))
    config = device_domain.configuration(selected['configuration'])
    with device_domain.exclusive(selected['device_directory']) as directory:
        ledger.require(device_domain.read_record(directory/'configuration.json') == config,
                       'associated device configuration changed')
        check(directory, config, resource_directory, value)
        current = lifecycle(directory, config)
        ledger.require(current['phase'] == 'idle', 'disarm the selected device fence before resource removal')
        _, pins = device_domain.names(config)
        ledger.require(not pins.exists(), 'device fence pin directory remains')
        binding = access_store.binding(directory, config['service_id'])
        with device_store.open_bound(binding) as journal:
            ledger.require(journal.head['state']['policy']['qualification'] is None,
                           'device qualification has not been withdrawn')
