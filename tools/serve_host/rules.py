"""Fixed per-generation host accounting alerts, separate from model metrics."""
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tools.serve_host import core

RULE='deploy/serve-ops/q4t-host.rules.yml'
BY='job, instance, deployment, generation'
TARGET='{job="q4t-host"}'
SUMMARIES={
 'Q4THostExporterDown':'The configured host resource exporter is unreachable.',
 'Q4THostTargetMissing':'No configured host resource exporter target is present.',
 'Q4THostMetricContractMissing':'The host observer lifecycle metric contract is incomplete or contradictory.',
 'Q4THostObserverStale':'Host observer output is stale or its wall clock is skewed.',
 'Q4THostSourceUnknown':'The bound managed generation cannot be verified; resource values are unavailable.',
 'Q4THostGenerationWaiting':'The expected managed generation has not become available.',
 'Q4THostGenerationTerminal':'The managed generation has terminated; check whether this was intended.',
 'Q4THostProtectionFailed':'This generation recorded an OOM, task limit refusal, or lost resource protection.',
 'Q4THostThrottlingObserved':'This generation has recorded host memory high events; these may be historical.',
 'Q4THostHeadroomLow':'The current host memory or task accounting domain is near its configured maximum.'}


def render():
    rules=[]
    def add(name,expr,duration='',severity='critical'):
        rules.append(dict(alert=name,expr=expr,**({'for':duration} if duration else {}),
            labels=dict(severity=severity,component='q4t-host'),
            annotations=dict(summary=SUMMARIES[name],runbook_url='https://github.com/thomas-hiddenpeak/qwen4-thor/blob/codex/main-reaudit-fixes/tools/serve_host/README.md')))
    add('Q4THostExporterDown','up{job="q4t-host"} == 0','15s')
    add('Q4THostTargetMissing','absent(up{job="q4t-host"})','30s')
    valid=(f'(count by (job,instance) (q4t_host_state{TARGET}) == {len(core.STATES)}) and on (job,instance) '
        f'(sum by (job,instance) (q4t_host_state{TARGET}) == 1)')
    add('Q4THostMetricContractMissing','(up{job="q4t-host"} == 1) unless on (job,instance) ('+valid+')','5s')
    stamp='q4t_host_evaluated_timestamp_seconds'+TARGET
    add('Q4THostObserverStale',f'(time() - {stamp} > 30) or ({stamp} - time() > 30)')
    aggregate=lambda expr:f'max by ({BY}) ({expr})'
    add('Q4THostSourceUnknown',aggregate('q4t_host_state{job="q4t-host",state="UNKNOWN"} == 1'),'5s')
    add('Q4THostGenerationWaiting',aggregate('q4t_host_state{job="q4t-host",state=~"WAITING|STARTING"} == 1'),'2m','warning')
    add('Q4THostGenerationTerminal',aggregate('q4t_host_state{job="q4t-host",state=~"STOPPED|FAILED"} == 1'),severity='warning')
    failure=('q4t_host_memory_events_total{job="q4t-host",event=~"oom|oom_kill"} > 0 or '
        'q4t_host_tasks_limit_events_total{job="q4t-host"} > 0 or '
        'q4t_host_reason{job="q4t-host",reason="host_resource_observation_unavailable"} == 1')
    add('Q4THostProtectionFailed',aggregate(failure))
    add('Q4THostThrottlingObserved',aggregate('q4t_host_memory_events_total{job="q4t-host",event="high"} > 0'),severity='information')
    low=(f'q4t_host_memory_current_bytes{TARGET} >= on ({BY}) (0.9 * q4t_host_memory_max_bytes{TARGET}) or '
         f'q4t_host_tasks_current{TARGET} >= on ({BY}) (0.9 * q4t_host_tasks_max{TARGET})')
    add('Q4THostHeadroomLow',aggregate('('+low+f') and on ({BY}) '
        '(q4t_host_snapshot_kind{job="q4t-host",kind="current"} == 1)'),'30s','warning')
    return dict(groups=[dict(name='q4t-host-accounting',interval='5s',rules=rules)])


if __name__=='__main__':
    value=json.dumps(render(),indent=2)+'\n'
    if sys.argv[1:]==['--write']:(ROOT/RULE).write_text(value)
    elif sys.argv[1:]==['--check']:
        if (ROOT/RULE).read_text()!=value:raise SystemExit('host rules differ')
    else:raise SystemExit('--check | --write')
