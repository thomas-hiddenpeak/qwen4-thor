"""CPU input derived from archived SRV-02d observations, not a mock endpoint."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT/'docs/evidence/serve-pressure-20261103/execution/business'


def metrics(*, version=3):
    # Preserve all archived families/labels; reset event counters for scenarios.
    values = json.loads((ARCHIVE/'final/final-resources.json').read_text())['metrics']
    for name in values:
        family = name.split('{', 1)[0]
        if (family.endswith('_total') and family != 'q4t_seq_slots_total') or family.endswith('_count') or family.endswith('_sum'):
            values[name] = 0
    values['q4t_control_connections'] = 0
    # /metrics itself is a classified non-control connection; retain its 1.
    if version == 4:
        values['q4t_business_connections'] = 0
        values['q4t_observation_connections'] = 1
        for kind in ('unclassified', 'business', 'observation', 'control'):
            values[f'q4t_connection_rejections_total{{kind="{kind}"}}'] = 0
    return values


def text_metrics(values=None, *, version=3):
    values = metrics(version=version) if values is None else values
    return ''.join(f'{name} {int(value) if isinstance(value,float) and value.is_integer() else value}\n'
                   for name, value in values.items())


def configuration(*, version=3):
    envelope = json.loads((ARCHIVE/'model/configuration.json').read_text())
    value = json.loads(envelope['raw'])
    if version == 4:
        value['limits'].update(observation_connections=4,
            connection_handlers=value['limits']['business_connections']+8,
            unclassified_shares_total=True)
    return value


def capabilities(*, version=3):
    # Fixed minimum projection; other capability data are intentionally unused.
    value = dict(object='q4t.capabilities', observability=dict(version=version),
                protocol_source_sha256=configuration()['protocol_source_sha256'])
    if version == 4:
        value['observability']['business_connections_exclude_observation'] = True
        value['http'] = dict(connection_profile='q4t-http-partitions-v1',
            observation_reserved_connections=4, control_reserved_connections=4,
            observation_routes=['GET /metrics','GET /v1/config','GET /v1/capabilities','GET /v1/models'])
    return value


def reply(port, path, *, version=3, **_):
    assert port == 18107
    if path == '/metrics':
        return dict(http_status=200, content_type='text/plain', body=text_metrics(version=version))
    if path in ('/livez', '/readyz'):
        value = dict(status='ready', live=True, ready=True)
    else:
        value = configuration(version=version) if path == '/v1/config' else capabilities(version=version)
    return dict(http_status=200, content_type='application/json', body=json.dumps(value))
