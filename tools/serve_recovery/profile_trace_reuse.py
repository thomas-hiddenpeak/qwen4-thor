"""Reconstruct the exact previous qualification beside fresh trace evidence.

Only prior metadata is compressed. All original business leaves remain intact,
including failed answers. The reconstructed canonical record must match the
accepted previous file SHA, and still passes its original source-bound verifier.
"""
import copy
import hashlib
from tools.serve_recovery import ledger, profile_mcp_codec as codec
from tools.serve_recovery.profile_trace_source import REVIEW, SOURCES, OBSERVED

FORMAT = 'q4t-tracing-profile-source-reuse-v1'
PREVIOUS = ('format', 'configuration', 'startup_memory', 'environment', 'reused_business')


def old_capabilities(value):
    from tools.serve_recovery.profile_trace_evidence import CAPABILITY
    result = copy.deepcopy(value)
    result['protocol_source_sha256'] = REVIEW['previous_protocol']
    chat = result['realtime']['conversation']
    ledger.require(ledger.canonical(chat.pop('tracing')) == ledger.canonical(CAPABILITY) and
                   chat['pending_execution'] == [], 'trace capability delta differs')
    chat['pending_execution'] = ['tracing']
    return result


def previous_record(value):
    from tools.serve_recovery import profile_evidence as e
    reuse = value['reused_business']
    ledger.keys(reuse, ('format', 'review_sha256', 'previous'))
    name = value['profile']
    ledger.require(name in SOURCES and value['format'] == e.TRACING_FORMAT and
        reuse['format'] == FORMAT and reuse['review_sha256'] == ledger.digest(REVIEW),
        'unknown trace reuse source')
    prior = codec.unpack(reuse['previous']); ledger.keys(prior, PREVIOUS)
    identity = value['identity']
    ledger.require(identity['binary_sha256'] == REVIEW['candidate_binary'] and
        identity['protocol_sha256'] == REVIEW['candidate_protocol'] and
        value['capabilities']['protocol_source_sha256'] == REVIEW['candidate_protocol'],
        'trace candidate identity differs')
    original = copy.deepcopy(value)
    original.update(prior)
    original['identity'].update(binary_sha256=REVIEW['previous_binary'],
                                protocol_sha256=REVIEW['previous_protocol'])
    original['capabilities'] = old_capabilities(value['capabilities'])
    fresh = original['business'].pop('realtime_tracing')
    ledger.require(fresh['profile'] == name and hashlib.sha256(
        ledger.canonical(original) + b'\n').hexdigest() == SOURCES[name]['record_sha256'],
        'previous accepted qualification changed or was incompletely retained')
    return original


def validate(value):
    from tools.serve_recovery import profile_mcp_reuse, profile_trace_evidence
    original = previous_record(value)
    profile_mcp_reuse.validate(original)
    profile_trace_evidence.business(value['business']['realtime_tracing'])
    observed = OBSERVED[value['profile']]
    for field in ('configuration', 'startup_memory', 'environment', 'capabilities'):
        ledger.require(ledger.digest(value[field]) == observed[field + '_sha256'],
                       'actual trace profile observations changed: ' + field)
    return value['reused_business']


def attach(value, prior):
    result = copy.deepcopy(value)
    result['reused_business'] = dict(format=FORMAT, review_sha256=ledger.digest(REVIEW),
        previous=codec.pack({key: prior[key] for key in PREVIOUS}))
    validate(result)
    return result
