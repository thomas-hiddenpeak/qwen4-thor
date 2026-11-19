"""Fixed reviewed source digests without recursively copying old certificates.

Every current business validator still runs. Only the exact accepted v18
record's untouched fields may be carried forward; new audio is actual work.
"""
import copy

from tools.serve_recovery import ledger
from tools.serve_recovery.profile_speech_source import REVIEW, SOURCES

FORMAT = 'q4t-speech-source-reuse-v1'


def old_capabilities(value):
    result = copy.deepcopy(value)
    result['protocol_source_sha256'] = REVIEW['previous_protocol']
    chat = result['realtime']['conversation']
    del chat['speech_output']
    chat['output_modalities'] = ['text']
    chat['pending_execution'] = ['audio', 'mcp', 'tracing']
    return result


def validate(value):
    from tools.serve_recovery import profile_evidence as evidence
    reuse = value['reused_business']; source = SOURCES[value['profile']]
    ledger.keys(reuse, ('format', 'review', 'source', 'keys', 'partial_keys'))
    ledger.require(reuse['format'] == FORMAT and value['format'] == evidence.SPEECH_FORMAT and
        ledger.canonical(reuse['review']) == ledger.canonical(REVIEW) and
        ledger.canonical(reuse['source']) == ledger.canonical(source),
        'unknown accepted speech source review')
    identity = value['identity']
    ledger.require(identity['binary_sha256'] == REVIEW['candidate_binary'] and
        identity['protocol_sha256'] == REVIEW['candidate_protocol'] and
        ledger.digest(dict(identity, binary_sha256=REVIEW['previous_binary'],
            protocol_sha256=REVIEW['previous_protocol'])) == source['identity'] and
        ledger.digest(value['capacity']) == source['capacity'], 'engine/backend/capacity drift')
    ledger.require(evidence.speech_enabled(value['capabilities']) and
        ledger.digest(old_capabilities(value['capabilities'])) == source['capabilities'],
        'source reuse cannot conceal another capability change')
    required = set(source['business'])
    ledger.require(reuse['keys'] == sorted(required - {'tts'}) and reuse['partial_keys'] == ['tts'] and
        set(value['business']) == required | {'realtime_speech'},
        'changed scope cannot reuse old evidence')
    for key in required - {'tts'}:
        ledger.require(ledger.digest(value['business'][key]) == source['business'][key],
                       'retained business drift: ' + key)
    tts = value['business']['tts']
    ledger.require(set(tts) == set(source['tts']) | {'chat_response', 'health'},
                   'shared Chat audio work missing')
    for key, digest in source['tts'].items():
        ledger.require(ledger.digest(tts[key]) == digest, 'standalone TTS changed: ' + key)
    return reuse


def attach(value):
    result = copy.deepcopy(value); source = SOURCES[value['profile']]
    result['reused_business'] = dict(format=FORMAT, review=copy.deepcopy(REVIEW),
        source=copy.deepcopy(source), keys=sorted(set(source['business']) - {'tts'}),
        partial_keys=['tts'])
    validate(result)
    return result
