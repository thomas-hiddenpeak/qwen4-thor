"""Keep the accepted tracing-era record recoverable alongside real Live business."""
import copy
import hashlib

from tools.serve_recovery import ledger, profile_mcp_codec as codec

FORMAT = 'q4t-live-profile-source-reuse-v1'
RECORD_FORMAT = 'q4t-profile-business-v23'
BETA_FORMAT = 'q4t-live-beta-profile-source-reuse-v1'
BETA_RECORD_FORMAT = 'q4t-profile-business-v24'
BETA_REVIEWED = {
    'primary': 'e2178b021652317a2d67189b73982edc3a7a1b70c4c2716929a448a5d579f07a',
    'light': '19299d0067ef28685380c4bf080526c2463e5f083ae9868c98bf6c869a92cd0c',
}
METADATA = ('format', 'configuration', 'startup_memory', 'environment', 'reused_business')
# Receipts are canonical profile record bytes including their terminating newline.
REVIEWED = {
    'primary': {
        'previous': '6255bf549874162c3e846206528a706af463f4c5facfffe0d087918e349858a5',
        'selected': 'd10054a6a8dae78f324ec37f510e32761c00568f0957d16927bbea583a587a0f'
    },
    'light': {
        'previous': '5da10f635a7ea8e289de5f114017237fa51dabcb90abadba433c7d5ef0337bf6',
        'selected': '16337b914b6b7a7b4bb476b0185b7dd9340be77eae44253d0c40f52e612aef98'
    }
}


# The SDK ran through this native ingress. The acceptance hash is carried in
# both reviewed profile records. An engine capability cannot qualify a gateway.
ACCEPTED_INGRESS = {
    '29c31bf9ef841fe729d2fc3085888153489d398e40c7508a64a726c5a431d019':
        'b7a1967ba8540dc51ae7187577052bb1273d789d85ed8ab5a2d1cf5b02f21caf',
    '1e744c78e3c8fc9600add6ac99c800ff58c69bb4a6acf1218104fc7bd85205d6':
        'b7a1967ba8540dc51ae7187577052bb1273d789d85ed8ab5a2d1cf5b02f21caf',
}


def verify_ingress(live, digest):
    expected = ACCEPTED_INGRESS.get(live['acceptance_sha256'])
    ledger.require(expected is not None and digest == expected,
                   'installed ingress differs from the qualified Live business route')


def record_hash(value):
    return hashlib.sha256(ledger.canonical(value) + b'\n').hexdigest()


def format_for(capabilities):
    if 'beta_query_selector' in capabilities.get('responses', {}):
        ledger.require(capabilities['responses']['beta_query_selector'] == 'beta=true',
                       'unknown qualified Responses Beta selector')
        return BETA_RECORD_FORMAT
    return RECORD_FORMAT


def attach(value, prior):
    """Store only the fields needed to recover the exact accepted old record."""
    old = prior['identity']
    new = value['identity']
    previous = {k: prior[k] for k in METADATA}
    previous['engine'] = {k: old[k] for k in ('binary_sha256', 'protocol_sha256')}
    previous['ctimes'] = {n: row['ctime_ns'] for n, row in old['model_assets'].items()
                          if row['ctime_ns'] != new['model_assets'][n]['ctime_ns']}
    before, after = prior['capabilities'], value['capabilities']
    previous['capabilities'] = {k: v for k, v in before.items()
                                if k not in after or ledger.canonical(v) != ledger.canonical(after[k])}
    previous['added'] = sorted(after.keys() - before.keys())
    revision = value['format'] == BETA_RECORD_FORMAT
    if revision:
        ledger.require(prior['format'] == RECORD_FORMAT, 'Beta requires the accepted Live profile')
        previous['live'] = prior['business']['live']
    value['reused_business'] = dict(format=BETA_FORMAT if revision else FORMAT,
                                    previous=codec.pack(previous))
    ledger.require(record_hash(previous_record(value)) == record_hash(prior),
                   'Live qualification cannot reconstruct its accepted source')


def previous_record(value):
    reuse = value['reused_business']
    ledger.keys(reuse, ('format', 'previous'))
    revision = value['format'] == BETA_RECORD_FORMAT
    ledger.require(reuse['format'] == (BETA_FORMAT if revision else FORMAT),
                   'incorrect Live qualification source')
    previous = codec.unpack(reuse['previous'])
    fields = (*METADATA, 'engine', 'ctimes', 'capabilities', 'added')
    ledger.keys(previous, (*fields, 'live') if revision else fields)
    original = copy.deepcopy(value)
    original.update({k: previous[k] for k in METADATA})
    original['identity'].update(previous['engine'])
    for name, ctime in previous['ctimes'].items():
        original['identity']['model_assets'][name]['ctime_ns'] = ctime
    for name in previous['added']:
        del original['capabilities'][name]
    original['capabilities'].update(previous['capabilities'])
    if revision:
        original['business']['live'] = previous['live']
    else:
        del original['business']['live']
    return original


def validate(value):
    from tools.serve_recovery import profile_trace_reuse
    if value['format'] == BETA_RECORD_FORMAT:
        name = value['profile']
        ledger.require(record_hash(value) == BETA_REVIEWED.get(name),
                       'Beta qualification differs from reviewed business and run identities')
        prior = previous_record(value)
        ledger.require(prior['format'] == RECORD_FORMAT and
                       record_hash(prior) == REVIEWED[name]['selected'],
                       'Beta qualification lost its accepted Live source')
        validate(prior)
        return value['reused_business']
    expected = REVIEWED.get(value['profile'])
    ledger.require(expected and value['format'] == RECORD_FORMAT and
                   record_hash(value) == expected['selected'],
                   'Live qualification differs from reviewed business and run identities')
    previous = previous_record(value)
    ledger.require(record_hash(previous) == expected['previous'],
                   'Live qualification lost historical business or its original failures')
    profile_trace_reuse.validate(previous)
    return value['reused_business']
