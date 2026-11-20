"""Source-bound native trace business; retained model failures stay separate."""
from tools.serve_recovery import ledger, profile_mcp_codec as codec
from tools.serve_recovery.profile_trace_source import REVIEW, OBSERVED
from tools.serve_realtime import tracing_checks

FORMAT = 'q4t-profile-realtime-tracing-v1'
CAPABILITY = dict(profile='q4t-realtime-tracing-v1', enabled=True,
    destination='native', endpoint='/v1/realtime/traces',
    configuration_immutable_after_enable=True, external_collector_required=False,
    capture_content=False, maximum_spans=256, record_storage_bytes=4 << 20,
    configuration_storage_bytes=64 << 10, queue_bytes=32 << 20,
    retention_seconds=604800, delete_revokes_active_capture=True,
    readiness_dependency=False, model_restart_dependency=False)


def business(value):
    ledger.keys(value, ('format', 'profile', 'review_sha256', 'witness'))
    name = value['profile']
    ledger.require(value['format'] == FORMAT and name in OBSERVED and
        value['review_sha256'] == ledger.digest(REVIEW), 'unknown trace source review')
    witness = codec.unpack(value['witness'])
    ledger.keys(witness, ('session', 'remote', 'before', 'after', 'source'))
    ledger.require(ledger.digest(witness) == OBSERVED[name]['witness_sha256'] and
        witness['session']['mode'] == name, 'actual trace witness identity differs')
    result = tracing_checks.observe(witness['session'], witness['session']['events'],
        witness['remote'], witness['source'], witness['before'], witness['after'])
    ledger.require(ledger.digest(result) == OBSERVED[name]['observation_sha256'],
                   'actual trace observations or quality changed')
    return value


def attach(name, witness):
    value = dict(format=FORMAT, profile=name, review_sha256=ledger.digest(REVIEW),
                 witness=codec.pack(witness))
    business(value)
    return value
