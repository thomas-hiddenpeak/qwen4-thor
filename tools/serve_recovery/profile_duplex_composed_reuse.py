"""Reuse exact unaffected evidence, with explicit identities for every new leaf."""
import copy
from tools.serve_recovery import ledger, profile_duplex_reuse as prior
from tools.serve_recovery.profile_duplex_composed_source import OBSERVED, REVIEW

FORMAT = 'q4t-duplex-composed-source-reuse-v1'
INPUTS = ('realtime', 'semantic_vad')


def validate(value):
    from tools.serve_recovery import profile_evidence as e
    name = value['profile']
    source, observed = prior.SOURCES[name], OBSERVED[name]
    reuse = value['reused_business']
    ledger.keys(reuse, ('format', 'review', 'source', 'observed', 'keys',
                       'reviewed_input_keys', 'composed_keys'))
    ledger.require(value['format'] == e.DUPLEX_FORMAT and
        reuse['format'] == FORMAT and
        ledger.canonical(reuse['review']) == ledger.canonical(REVIEW) and
        reuse['source'] == source and reuse['observed'] == observed,
        'unknown composed reuse source or review')
    identity = value['identity']
    ledger.require(identity['binary_sha256'] == REVIEW['candidate_binary'] and
        identity['protocol_sha256'] == REVIEW['candidate_protocol'] and
        ledger.digest(dict(identity,
            binary_sha256=prior.REVIEW['previous_binary'],
            protocol_sha256=prior.REVIEW['previous_protocol'])) ==
        source['identity'] and ledger.digest(value['capacity']) ==
        source['capacity'], 'composed reuse changed backend, model or capacity')
    ledger.require(e.duplex_enabled(value['capabilities']) and
        value['capabilities']['protocol_source_sha256'] ==
        REVIEW['candidate_protocol'] and
        ledger.digest(prior.old_capabilities(value['capabilities'])) ==
        source['capabilities'], 'composed reuse concealed capability change')
    keys = set(source['business']) - set(INPUTS)
    ledger.require(reuse['keys'] == sorted(keys) and
        reuse['reviewed_input_keys'] == list(INPUTS) and
        reuse['composed_keys'] == ['realtime_duplex'] and
        set(value['business']) == set(source['business']) | {'realtime_duplex'},
        'composed scope incomplete')
    for key in keys:
        ledger.require(ledger.digest(value['business'][key]) ==
            source['business'][key], 'retained business changed: ' + key)
    for key in INPUTS:
        ledger.require(ledger.digest(value['business'][key]) ==
            observed['input_digests'][key], 'reviewed input changed: ' + key)
    from tools.serve_recovery import profile_duplex_composed as composed
    duplex = value['business']['realtime_duplex']
    ledger.require(duplex['profile'] == name, 'composed profile owner differs')
    composed.business(duplex)
    return reuse


def attach(value):
    result = copy.deepcopy(value)
    name = value['profile']
    source = prior.SOURCES[name]
    result['reused_business'] = dict(format=FORMAT, review=copy.deepcopy(REVIEW),
        source=copy.deepcopy(source), observed=copy.deepcopy(OBSERVED[name]),
        keys=sorted(set(source['business']) - set(INPUTS)),
        reviewed_input_keys=list(INPUTS), composed_keys=['realtime_duplex'])
    validate(result)
    return result
