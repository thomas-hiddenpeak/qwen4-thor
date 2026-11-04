"""Finite TLS material and gateway source alerts for one explicit target."""
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True;sys.path.insert(0,str(ROOT))
from tools.serve_tls import core as c

RULE='deploy/serve-ops/q4t-tls.rules.yml'
BY='job,instance,deployment,generation'
TARGET='{job="q4t-tls"}'
SUMMARIES={
    'Q4TTLSExporterDown':'The configured TLS observer exporter is unreachable.',
    'Q4TTLSTargetMissing':'No configured TLS observer target is present.',
    'Q4TTLSMetricContractMissing':'The TLS observer metric contract is incomplete or contradictory.',
    'Q4TTLSObserverStale':'TLS observer output is stale or its wall clock is skewed.',
    'Q4TTLSSourceUnknown':'The bound gateway generation cannot be verified.',
    'Q4TTLSGenerationWaiting':'The expected gateway generation has not become ready.',
    'Q4TTLSGenerationTerminal':'The bound gateway generation has terminated; check whether this was intended.',
    'Q4TTLSCertificateExpiring':'The pinned certificate chain has seven days or less remaining; prepare explicit renewal.',
    'Q4TTLSCertificateInvalid':'The pinned certificate chain is expired or not yet valid.',
    'Q4TTLSTrustNotPinned':'No explicit CA anchor is configured; client trust was not verified by package preparation.'}


def render():
    rows=[]
    def add(name,expression,duration='',severity='critical'):
        rows.append(dict(alert=name,expr=expression,**({'for':duration} if duration else {}),
            labels=dict(severity=severity,component='q4t-tls'),annotations=dict(summary=SUMMARIES[name],
                runbook_url='https://github.com/thomas-hiddenpeak/qwen4-thor/blob/codex/main-reaudit-fixes/tools/serve_tls/README.md')))
    def enum(name,label,values):
        full='q4t_tls_'+name;all_=full+TARGET;allowed=full+'{job="q4t-tls",'+label+'=~"'+'|'.join(values)+'"}'
        valid=f'(count by ({BY}) ({all_}) == {len(values)}) and on ({BY}) '
        valid+=f'(count by ({BY}) ({allowed}) == {len(values)}) and on ({BY}) '
        valid+=f'(sum by ({BY}) ({all_}) == 1)'
        invalid=f'({all_} != 0) and ({all_} != 1)'
        return '('+valid+f') unless on ({BY}) ({invalid})'
    add('Q4TTLSExporterDown','up{job="q4t-tls"} == 0','15s')
    add('Q4TTLSTargetMissing','absent(up{job="q4t-tls"})','30s')
    stamp='q4t_tls_evaluated_timestamp_seconds'+TARGET
    parts=[enum('state','state',c.STATES),enum('certificate_state','state',c.CERTIFICATES),
           enum('reason','reason',c.REASONS),enum('trust','trust',c.TRUST),
           f'count by ({BY}) (({stamp} >= 0) and ({stamp} <= 9007199254740991)) == 1']
    start,end=('q4t_tls_'+n+'_timestamp_seconds'+TARGET for n in ('not_before','not_after'))
    material=(f'({end} > on ({BY}) {start}) and on ({BY}) ({start} >= 0) and on ({BY}) ({end} <= 9007199254740991)')
    absent='q4t_tls_certificate_state{job="q4t-tls",state="UNKNOWN"} == 1'
    parts.append(f'count by ({BY}) (({material}) or ({absent})) == 1')
    valid=f' and on ({BY}) '.join('('+p+')' for p in parts)
    valid='count by (job,instance) ('+valid+') == 1'
    add('Q4TTLSMetricContractMissing','(up{job="q4t-tls"} == 1) unless on (job,instance) ('+valid+')','5s')
    add('Q4TTLSObserverStale',f'(time() - {stamp} > 30) or ({stamp} - time() > 30)')
    aggregate=lambda expr:f'max by ({BY}) ({expr})'
    fresh=f'((time() - {stamp} <= 30) and ({stamp} - time() <= 30))'
    add('Q4TTLSSourceUnknown',aggregate('q4t_tls_state{job="q4t-tls",state="UNKNOWN"} == 1'),'5s')
    add('Q4TTLSGenerationWaiting',aggregate('q4t_tls_state{job="q4t-tls",state=~"WAITING|STARTING"} == 1'),'2m','warning')
    add('Q4TTLSGenerationTerminal',aggregate('q4t_tls_state{job="q4t-tls",state=~"STOPPED|FAILED"} == 1'),severity='warning')
    expiring=f'({end} - time() <= 604800) and ({end} > time()) and on ({BY}) ({start} <= time())'
    invalid=f'({end} <= time()) or ({start} > time())'
    add('Q4TTLSCertificateExpiring',aggregate('('+expiring+f') and on ({BY}) '+fresh),'15s','warning')
    add('Q4TTLSCertificateInvalid',aggregate('('+invalid+f') and on ({BY}) '+fresh))
    add('Q4TTLSTrustNotPinned',aggregate('q4t_tls_trust{job="q4t-tls",trust="not_configured"} == 1'),'15s','warning')
    return dict(groups=[dict(name='q4t-tls-material-and-lifecycle',interval='5s',rules=rows)])


if __name__=='__main__':
    value=json.dumps(render(),indent=2)+'\n'
    if sys.argv[1:]==['--write']:(ROOT/RULE).write_text(value)
    elif sys.argv[1:]==['--check']:
        if (ROOT/RULE).read_text()!=value:raise SystemExit('TLS rules differ')
    else:raise SystemExit('--check | --write')
