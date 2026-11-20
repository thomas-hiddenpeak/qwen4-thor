"""Keep exact failed answers; qualify only targeted new caller tasks separately."""
import copy
from tools.serve_recovery import ledger, profile_duplex_evidence as d
from tools.serve_recovery.profile_duplex_followup_source import REVIEW, ORIGINAL, REQUESTS
from tools.serve_recovery.profile_duplex_followup_source import FOLLOWUP_REVIEW, DIAGNOSTICS
from tools.serve_realtime import duplex_quality_checks as checks

FORMAT='q4t-profile-realtime-duplex-followup-v1'
SEEDS=(41,42)


def diagnostic(row, label, variant, seed):
    ledger.keys(row, ('label','variant','seed','request','status','response','quality'))
    request=dict(copy.deepcopy(REQUESTS[label][variant]),seed=seed)
    ledger.require(row['label']==label and row['variant']==variant and
        type(row['seed']) is int and row['seed']==seed and row['request']==request and
        type(row['status']) is int and row['status']==200, 'fixed HTTP diagnostic drift')
    value=row['response'];choices=value['choices']
    ledger.require(len(choices)==1 and choices[0]['finish_reason']=='stop' and
        not choices[0]['message'].get('refusal') and not choices[0]['message'].get('tool_calls') and
        value['usage']['total_tokens']>0, 'diagnostic was not a complete actual text response')
    quality=d.duplex_checks.judgment(label,choices[0]['message']['content'],d.PHRASE)
    # Diagnostic quality remains factual, including the fixed seed-41 partial
    # answer. It is not a service protocol oracle or a model-quality repair.
    ledger.require(row['quality']==quality, 'diagnostic quality was relabelled')


def business(value):
    ledger.keys(value,('format','original','followup','diagnostics','source_review','followup_review'))
    ledger.require(value['format']==FORMAT and value['source_review']==REVIEW and
        ledger.digest(value['original'])==ORIGINAL and value['followup_review']==FOLLOWUP_REVIEW,
        'original failed answers/source changed')
    def original_quality(mode, rows):
        expected={'mixed'} if mode=='manual' else {'vad_auto'}
        ledger.require({q['label'] for q in rows if q['status']=='FAIL'}==expected and
            all(q['status'] in ('PASS','FAIL') for q in rows),'unreviewed original quality failures')
    d.original_business(value['original'],quality_check=original_quality)
    ledger.keys(value['followup'], ('mixed','vad_once'))
    for mode,witness in value['followup'].items():
        ledger.keys(witness, ('session','before','after'))
        ledger.require(witness['session']['mode']==mode,'follow-up owner differs')
        observed=checks.observe(witness['session'],d.PHRASE)
        ledger.require(all(q['status']=='PASS' for q in observed['quality']),
                       'targeted new real task failed')
        before,after=witness['before'],witness['after'];d.state(before);d.state(after)
        ledger.require(after['admitted']-before['admitted']==observed['admitted'] and
            after['settled']-before['settled']==observed['visible_tokens'],
            'targeted admission/settlement differs')
    rows=value['diagnostics'];ledger.require(type(rows) is list and len(rows)==8 and
        ledger.digest(rows)==DIAGNOSTICS,'diagnostic scope or original answers changed')
    for row,(seed,label,variant) in zip(rows,[(s,l,v) for s in SEEDS
            for l in ('mixed','vad_auto') for v in ('before','after')]):
        diagnostic(row,label,variant,seed)
    return value
