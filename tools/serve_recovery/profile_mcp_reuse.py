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
    ledger.require(chat.pop('mcp') == CAPABILITY and chat['pending_execution'] == ['tracing'],
                   'new MCP capability differs from reviewed source')
    chat['pending_execution'] = ['mcp', 'tracing']
    return result


def validate(value):
    from tools.serve_recovery import profile_evidence as e, profile_mcp_evidence as m
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
    return reuse


def attach(value):
    result = copy.deepcopy(value)
    name = value['profile']
    result['reused_business'] = dict(format=FORMAT, review_sha256=ledger.digest(REVIEW),
        source=copy.deepcopy(SOURCES[name]), observed=copy.deepcopy(OBSERVED[name]),
        keys=sorted(SOURCES[name]['business']), fresh_keys=['realtime_mcp'])
    validate(result)
    return result
