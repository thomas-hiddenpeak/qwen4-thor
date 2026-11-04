"""Finite TLS material and gateway lifecycle metrics, with separate confidence."""
import math
import re
from tools.serve_host.core import Invalid,require
from tools.serve_gateway import tls

STATES=('WAITING','STARTING','READY','STOPPING','STOPPED','FAILED','UNKNOWN')
CERTIFICATES=('VALID','EXPIRING','NOT_YET_VALID','EXPIRED','UNKNOWN')
REASONS=('none','binding_invalid','package_unavailable','source_unavailable',
         'source_stale','owner_absent','boot_changed','gateway_failure',
         'gateway_tls_not_yet_valid','gateway_tls_expired','gateway_tls_observation_unavailable')
TRUST=('explicit_ca','not_configured','unknown')
STALE_SECONDS=30


def finite(value):
    require(type(value) in (int,float) and math.isfinite(value) and value>=0,'binding_invalid')
    return value


def material(value):
    require(type(value) is dict and type(value['version']) is int and value['version']==1 and value['trust'] in TRUST[:-1] and
        value['revocation_checked'] is False and type(value['chain']) is list and 1<=len(value['chain'])<=8,'binding_invalid')
    rows=[*value['chain'],*([value['anchor']] if value['anchor'] is not None else [])]
    require((value['anchor'] is not None)==(value['trust']=='explicit_ca'),'binding_invalid')
    for row in rows:
        require(set(row)=={'sha256','not_before','not_after'} and re.fullmatch(r'[0-9a-f]{64}',row['sha256']) and
            type(row['not_before']) is int and type(row['not_after']) is int and
            0<=row['not_before']<row['not_after']<=2**53-1,'binding_invalid')
    require(value['not_before']==max(r['not_before'] for r in rows) and
        value['not_after']==min(r['not_after'] for r in rows),'binding_invalid')
    return value


def view(state,reason='none',*,source_age=None,cleanup=None):
    require(state in STATES and reason in REASONS,'binding_invalid')
    return dict(state=state,reason=reason,source_age_seconds=source_age,cleanup_confirmed=cleanup)


def certificate(value,now):
    if value is None:return dict(state='UNKNOWN',trust='unknown',not_before=None,not_after=None)
    value=material(value);observed=tls.observe(value,now)
    return dict(state=observed['state'],trust=value['trust'],not_before=value['not_before'],not_after=value['not_after'])


def metrics(value,deployment,generation):
    require(re.fullmatch(r'[A-Za-z0-9_-]{1,64}',deployment) and re.fullmatch(r'[0-9a-f]{64}',generation),'binding_invalid')
    labels=f'deployment="{deployment}",generation="{generation}"';lines=[]
    def emit(name,help_,rows):
        name='q4t_tls_'+name;lines.extend((f'# HELP {name} {help_}',f'# TYPE {name} gauge'))
        for extra,number in rows:
            finite(number);lines.append(f'{name}{{{labels}{","+extra if extra else ""}}} {number}')
    emit('evaluated_timestamp_seconds','Observer evaluation wall time; does not imply source freshness.',[('',value['evaluated_unix'])])
    emit('state','Bound gateway lifecycle, not a remote TLS handshake check.',[(f'state="{v}"',int(value['state']==v)) for v in STATES])
    emit('reason','Finite source observation or terminal failure reason.',[(f'reason="{v}"',int(value['reason']==v)) for v in REASONS])
    cert=value['certificate']
    emit('certificate_state','Current time validity of the pinned package material, independently of service lifecycle.',
         [(f'state="{v}"',int(cert['state']==v)) for v in CERTIFICATES])
    emit('trust','Explicit CA configured in the pinned package; not revocation or live handshake evidence.',
         [(f'trust="{v}"',int(cert['trust']==v)) for v in TRUST])
    if value['source_age_seconds'] is not None:
        emit('source_age_seconds','Age of the live supervisor record in the same boot.',[('',value['source_age_seconds'])])
    if value['cleanup_confirmed'] is not None:
        emit('cleanup_confirmed','Historical owned gateway cleanup receipt; not backend or GPU cleanup.',[('',int(value['cleanup_confirmed']))])
    for key in ('not_before','not_after'):
        if cert[key] is not None:
            emit(key+'_timestamp_seconds','Validity boundary of the complete pinned certificate chain.',[('',cert[key])])
    return '\n'.join(lines)+'\n'
