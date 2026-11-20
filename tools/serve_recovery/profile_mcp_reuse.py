"""Retain only exact prior leaves beside the actual MCP evidence."""
import copy
from tools.serve_recovery import ledger
from tools.serve_recovery.profile_mcp_source import REVIEW, OBSERVED, SOURCES

FORMAT = 'q4t-mcp-profile-source-reuse-v1'


def old_capabilities(value):
    from tools.serve_recovery.profile_mcp_evidence import CAPABILITY
    result = copy.deepcopy(value)
    result['protocol_source_sha256'] = REVIEW['previous_protocol']
    chat = result['realtime']['conversation']
    ledger.require(ledger.canonical(chat.pop('mcp')) == ledger.canonical(CAPABILITY) and
                   chat['pending_execution'] == ['tracing'],
                   'new MCP capability differs from reviewed source')
    chat['pending_execution'] = ['mcp', 'tracing']
    return result


def validate(value):
    from tools.serve_recovery import profile_evidence as e, profile_mcp_evidence as m
    original = value
    if 'binding_guard' in value['reused_business']:
        value = binding_guard_base(value)
    name = value['profile']
    source = SOURCES[name]
    reuse = value['reused_business']
    ledger.keys(reuse, ('format', 'review_sha256', 'source', 'observed', 'keys', 'fresh_keys'))
    ledger.require(value['format'] == e.MCP_FORMAT and reuse['format'] == FORMAT and
        reuse['review_sha256'] == ledger.digest(REVIEW) and reuse['source'] == source and
        reuse['observed'] == OBSERVED[name], 'unknown MCP evidence reuse source')
    identity = value['identity']
    ledger.require(identity['binary_sha256'] == REVIEW['candidate_binary'] and
        identity['protocol_sha256'] == REVIEW['candidate_protocol'] and
        ledger.digest(dict(identity, binary_sha256=REVIEW['previous_binary'],
            protocol_sha256=REVIEW['previous_protocol'])) == source['identity'] and
        ledger.digest(value['capacity']) == source['capacity'],
        'MCP qualification changed backend, model or capacity')
    ledger.require(value['capabilities']['protocol_source_sha256'] == REVIEW['candidate_protocol'] and
        ledger.digest(old_capabilities(value['capabilities'])) == source['capabilities'],
        'MCP reuse concealed another capability change')
    keys = set(source['business'])
    ledger.require(reuse['keys'] == sorted(keys) and reuse['fresh_keys'] == ['realtime_mcp'] and
        set(value['business']) == keys | {'realtime_mcp'}, 'MCP evidence coverage incomplete')
    for key in keys:
        ledger.require(ledger.digest(value['business'][key]) == source['business'][key],
                       'retained business changed: ' + key)
    ledger.require(value['business']['realtime_mcp']['profile'] == name,
                   'MCP profile owner changed')
    m.business(value['business']['realtime_mcp'])
    return original['reused_business']


def attach(value):
    result = copy.deepcopy(value)
    name = value['profile']
    result['reused_business'] = dict(format=FORMAT, review_sha256=ledger.digest(REVIEW),
        source=copy.deepcopy(SOURCES[name]), observed=copy.deepcopy(OBSERVED[name]),
        keys=sorted(SOURCES[name]['business']), fresh_keys=['realtime_mcp'])
    validate(result)
    return result


def binding_guard_base(value):
    """Recognize one reviewed allocation fix, without re-signing old evidence.

    The supplemental witness is the exact, independently checked real SDK
    read/approval/answer and accounting. Original business leaves, including
    failures, still pass the preceding source-bound verifier unchanged.
    """
    from tools.serve_recovery import profile_mcp_codec as codec
    from tools.serve_recovery.profile_mcp_source import BINDING_GUARD
    result = copy.deepcopy(value)
    supplement = result['reused_business'].pop('binding_guard')
    ledger.keys(supplement, ('review_sha256', 'witness'))
    name = result['profile']
    ledger.require(name in BINDING_GUARD['profiles'] and
        supplement['review_sha256'] == ledger.digest(BINDING_GUARD),
        'unknown MCP allocation guard review')
    source = BINDING_GUARD['profiles'][name]
    witness = codec.unpack(supplement['witness'])
    ledger.require(ledger.digest(witness) == source['witness_sha256'] and
        ledger.digest(result['configuration']) == source['configuration_sha256'] and
        ledger.digest(result['startup_memory']) == source['startup_memory_sha256'],
        'allocation guard actual business or observations changed')
    identity = result['identity']
    ledger.require(identity['binary_sha256'] == BINDING_GUARD['binary_sha256'] and
        identity['protocol_sha256'] == BINDING_GUARD['protocol_sha256'] and
        result['capabilities']['protocol_source_sha256'] == BINDING_GUARD['protocol_sha256'],
        'allocation guard engine or protocol differs')
    identity.update(binary_sha256=REVIEW['candidate_binary'],
                    protocol_sha256=REVIEW['candidate_protocol'])
    result['capabilities']['protocol_source_sha256'] = REVIEW['candidate_protocol']
    return result


def attach_binding_guard(value, witness):
    from tools.serve_recovery import profile_mcp_codec as codec
    from tools.serve_recovery.profile_mcp_source import BINDING_GUARD
    result = copy.deepcopy(value)
    ledger.require('binding_guard' not in result['reused_business'],
                   'allocation guard already attached')
    result['reused_business']['binding_guard'] = dict(
        review_sha256=ledger.digest(BINDING_GUARD), witness=codec.pack(witness))
    validate(result)
    return result
