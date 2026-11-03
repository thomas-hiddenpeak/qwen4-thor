"""CPU input derived from archived SRV-02d observations, not a mock endpoint."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT/'docs/evidence/serve-pressure-20261103/execution/business'


def metrics():
    # Preserve all archived families/labels; reset event counters for scenarios.
    values = json.loads((ARCHIVE/'final/final-resources.json').read_text())['metrics']
    for name in values:
        family = name.split('{', 1)[0]
        if (family.endswith('_total') and family != 'q4t_seq_slots_total') or family.endswith('_count') or family.endswith('_sum'):
            values[name] = 0
    values['q4t_control_connections'] = 0
    # The archived sample still observed one closing business connection.
    # CPU idle scenarios explicitly start with it closed.
    values['q4t_business_connections'] = 0
    return values


def text_metrics(values=None):
    values = metrics() if values is None else values
    return ''.join(f'{name} {int(value) if isinstance(value,float) and value.is_integer() else value}\n'
                   for name, value in values.items())


def configuration():
    envelope = json.loads((ARCHIVE/'model/configuration.json').read_text())
    return json.loads(envelope['raw'])


def capabilities():
    # Fixed minimum projection; other capability data are intentionally unused.
    return dict(object='q4t.capabilities', observability=dict(version=3),
                protocol_source_sha256=configuration()['protocol_source_sha256'])


def reply(port, path, **_):
    assert port == 18107
    if path == '/metrics':
        return dict(http_status=200, content_type='text/plain', body=text_metrics())
    if path in ('/livez', '/readyz'):
        value = dict(status='ready', live=True, ready=True)
    else:
        value = configuration() if path == '/v1/config' else capabilities()
    return dict(http_status=200, content_type='application/json', body=json.dumps(value))
