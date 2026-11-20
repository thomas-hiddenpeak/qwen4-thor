"""Only the fixed reviewed non-input work can accompany fresh duplex evidence."""
import copy
from tools.serve_recovery import ledger
from tools.serve_recovery.profile_duplex_source import REVIEW, SOURCES

FORMAT = 'q4t-duplex-source-reuse-v1'
FRESH = ('realtime', 'realtime_duplex', 'semantic_vad')


def old_capabilities(value):
    result=copy.deepcopy(value)
    result['protocol_source_sha256']=REVIEW['previous_protocol']
    chat=result['realtime']['conversation']
    audio=chat.pop('input_audio')
    ledger.require(audio['configured'] is True, 'qualified native ASR input required')
    chat['session_host_reservation_bytes']-=audio['additional_host_reservation_bytes']
    chat['pending_execution']=['audio_input','mcp','tracing']
    return result


def validate(value):
    from tools.serve_recovery import profile_evidence as evidence
    source=SOURCES[value['profile']];reuse=value['reused_business']
    ledger.keys(reuse,('format','review','source','keys','fresh_keys'))
    ledger.require(value['format']==evidence.DUPLEX_FORMAT and reuse['format']==FORMAT and
        ledger.canonical(reuse['review'])==ledger.canonical(REVIEW) and
        ledger.canonical(reuse['source'])==ledger.canonical(source),
        'unknown duplex source or review')
    identity=value['identity']
    ledger.require(identity['binary_sha256']==REVIEW['candidate_binary'] and
        identity['protocol_sha256']==REVIEW['candidate_protocol'] and
        ledger.digest(dict(identity,binary_sha256=REVIEW['previous_binary'],
            protocol_sha256=REVIEW['previous_protocol']))==source['identity'] and
        ledger.digest(value['capacity'])==source['capacity'],
        'duplex reuse changed backend, model or capacity')
    ledger.require(evidence.duplex_enabled(value['capabilities']) and
        ledger.digest(old_capabilities(value['capabilities']))==source['capabilities'],
        'duplex reuse concealed another capability change')
    keys=set(source['business'])-set(FRESH)
    ledger.require(reuse['keys']==sorted(keys) and reuse['fresh_keys']==sorted(FRESH) and
        set(value['business'])==set(source['business'])|{'realtime_duplex'},
        'duplex fresh/retained scope incomplete')
    for key in keys:
        ledger.require(ledger.digest(value['business'][key])==source['business'][key],
                       'retained duplex source changed: '+key)
    return reuse


def attach(value):
    result=copy.deepcopy(value);source=SOURCES[value['profile']]
    result['reused_business']=dict(format=FORMAT,review=copy.deepcopy(REVIEW),
        source=copy.deepcopy(source),keys=sorted(set(source['business'])-set(FRESH)),
        fresh_keys=sorted(FRESH))
    validate(result);return result
