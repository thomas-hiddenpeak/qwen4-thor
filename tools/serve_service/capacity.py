"""Explicit managed capacity, shared by preparation, execution and observation."""
from tools.serve_service.package import PROFILE, require, same_json

NAME = 'text-managed-capacity-v1'
BOUNDS = dict(max_seq=(1, 8192), max_len=(1, 2**31-1),
    max_prefill=(1, 8192), max_tokens=(1, 2**31-1), mtp_k=(1, 16),
    max_inflight_requests=(1, 4096), max_queued_requests=(0, 4096),
    min_available_memory_mib=(0, 1048576),
    memory_recovery_margin_mib=(1, 1048576),
    startup_memory_headroom_mib=(0, 1048576),
    request_deadline_ms=(1000, 10800000))


def validate(value):
    require(type(value) is dict and value.keys() ==
        {'name', 'host', 'mtp_enabled', *BOUNDS}, 'invalid managed capacity fields')
    require(value['name'] == NAME and value['host'] == '127.0.0.1',
            'invalid managed capacity identity')
    require(type(value['mtp_enabled']) is bool, 'MTP switch must be boolean')
    for key, (low, high) in BOUNDS.items():
        require(type(value[key]) is int and low <= value[key] <= high,
                'invalid managed capacity: '+key)
    require(value['max_seq'] <= value['max_prefill'],
            'max-seq exceeds effective max-prefill forward rows')
    require(value['max_queued_requests'] <= value['max_inflight_requests'],
            'max-queued-requests exceeds max-inflight-requests')
    return dict(value)


def normalize(value):
    """Versioned user input; omissions inherit the documented managed defaults.

    Context/prefill/admission are explicit positive capacities. This contract
    requests exact effective capacities; a startup budget clipping a request
    cannot silently pass the readiness identity check.
    """
    require(type(value) is dict and value.get('version') == 1 and
        type(value.get('version')) is int and
        value.keys() <= {'version', 'mtp_enabled', *BOUNDS},
        'invalid capacity configuration')
    defaults = dict(PROFILE, name=NAME, mtp_enabled=True,
                    request_deadline_ms=1200000)
    return validate(defaults | {k: v for k, v in value.items() if k != 'version'})


def check_clients(profile, clients):
    from tools.serve_gateway import client_policy
    client_policy.validate(clients)
    require(not clients.get('fair_queue', False) or (
        profile['max_queued_requests'] >= len(clients['clients']) and
        sum(c['max_inflight'] for c in clients['clients']) <=
            profile['max_inflight_requests']),
        'client fairness exceeds global capacity')


def observation(profile, observed):
    """Additional fields were not part of historical supervision contracts."""
    validate(profile)
    expected = dict(default_output_tokens=profile['max_tokens'],
        request_deadline_ms=profile['request_deadline_ms'],
        mtp_k=profile['mtp_k'] if profile['mtp_enabled'] else None,
        startup_memory_headroom_mib=profile['startup_memory_headroom_mib'])
    require(all(k in observed and same_json(observed[k], v)
                for k, v in expected.items()), 'effective capacity differs')
