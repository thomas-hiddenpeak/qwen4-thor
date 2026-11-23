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

# The raw duplex transcript can contain the same long PCM/event text in several
# observed requests and replies. Keep every occurrence recoverable in new records.
TEXT_FORMAT = 'q4t-duplex-shared-strings-v1'
TEXT_REF = '$q4t_duplex_text'
TEXT_BYTES = 8 << 20
MIN_TEXT = 1024


def _visit_text(value, string, reference):
    if type(value) is dict:
        if TEXT_REF in value:
            ledger.require(set(value) == {TEXT_REF}, 'invalid duplex text reference')
            return reference(value[TEXT_REF])
        return {k:_visit_text(v, string, reference) for k,v in value.items()}
    if type(value) is list:
        return [_visit_text(v, string, reference) for v in value]
    return string(value) if type(value) is str and len(value) >= MIN_TEXT else value


def pack_text(value):
    raw = ledger.canonical(value)
    ledger.require(0 < len(raw) <= TEXT_BYTES, 'duplex transcript size')
    counts = {}

    def count(text):
        counts[text] = counts.get(text, 0) + 1

    def collision(_):
        raise ValueError('reserved duplex text reference in original evidence')

    _visit_text(value, count, collision)
    strings = sorted(text for text,n in counts.items() if n > 1)
    lookup = {text:i for i,text in enumerate(strings)}
    def replace(text):
        return {TEXT_REF:lookup[text]} if text in lookup else text
    return dict(format=TEXT_FORMAT, bytes=len(raw), sha256=ledger.digest(value),
                strings=strings, witness=_visit_text(value, replace, collision))


def unpack_text(value):
    ledger.keys(value, ('format','bytes','sha256','strings','witness'))
    strings = value['strings']
    ledger.require(value['format'] == TEXT_FORMAT and type(value['bytes']) is int and
        0 < value['bytes'] <= TEXT_BYTES and type(value['sha256']) is str and
        ledger.HASH.fullmatch(value['sha256']) and type(strings) is list and
        len(strings) <= TEXT_BYTES // (2 * MIN_TEXT) and
        all(type(s) is str and len(s) >= MIN_TEXT for s in strings) and
        strings == sorted(set(strings)), 'invalid shared duplex transcript')
    # Budget the exact serialized expansion before committing to a complete
    # serialization. Resolving each reference only borrows an immutable string.
    size = len(ledger.canonical(value['witness']))
    ledger.require(size <= value['bytes'], 'duplex template exceeds declared size')
    gains = [len(ledger.canonical(s)) - len(ledger.canonical({TEXT_REF:i}))
             for i,s in enumerate(strings)]
    uses = [0] * len(strings)
    def resolve(i):
        nonlocal size
        ledger.require(type(i) is int and 0 <= i < len(strings), 'invalid duplex text index')
        size += gains[i]
        ledger.require(size <= value['bytes'], 'duplex text expansion exceeds bound')
        uses[i] += 1
        return strings[i]
    restored = _visit_text(value['witness'], lambda s:s, resolve)
    ledger.require(size == value['bytes'] and all(n >= 2 for n in uses) and
                   ledger.digest(restored) == value['sha256'], 'duplex transcript identity differs')
    return restored
