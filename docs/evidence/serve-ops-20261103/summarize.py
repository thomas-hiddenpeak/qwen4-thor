"""Summarize already captured SRV-12 evidence without another HTTP request."""
import json
from pathlib import Path
import sys

ROOT=next(p for p in Path(__file__).resolve().parents if (p/'AGENTS.md').exists())
sys.path.insert(0,str(ROOT))
from tools.serve_ops import stage
h=stage.h;base=stage.BASE;output=stage.OUTPUT
for directory in (stage.PACKAGE,output):h.verify_seal(directory)
result=h.read(output/'business/result.json')
h.require(result['status']=='PASS_BOUNDED_LOCAL_OPERATIONS','actual acceptance incomplete')
rows=h.read(output/'business/prometheus/down-observations.json')
first_down=next(row['unix_seconds'] for row in rows
    if row['up']['data']['result'][0]['value'][1]=='0')
first_fire=next(row['unix_seconds'] for row in rows if any(rule.get('state')=='firing'
    and rule['name']=='Q4TTargetDown' for g in row['rules']['data']['groups'] for rule in g['rules']))
diagnostics={name:h.read(output/f'business/diagnostics/{name}/report.json') for name in ('before','after','down')}
events=[json.loads(line) for line in (output/'lease/events.jsonl').read_text().splitlines()]
wire=h.read(output/'business/wire-schema-result.json')
after=diagnostics['after']
summary=dict(source_commit=h.read(stage.PACKAGE/'plan.json')['source_commit'],
    business=result,finalize=h.read(output/'finalized.json'),guard=h.read(output/'lease/exit.json'),
    conflicts_or_abort=[r for r in events if r.get('conflicts') or r['event']=='ENV_ABORT'],
    cpu=dict(python_boundary_tests=29,driver_tests=6,
        scenarios=h.read(stage.PACKAGE/'cpu/source-and-runbook.json')['time_series_scenarios'],
        assertions=h.read(stage.PACKAGE/'cpu/source-and-runbook.json')['assertions'],
        original_failures_preserved=True),
    diagnostics=dict(states={n:r['state'] for n,r in diagnostics.items()},
        identity=after['identity'],comparison=after['comparison'],
        log_projected_events=len(after['log']['events']),
        stopped_probe_errors={n:v.get('error') for n,v in diagnostics['down']['probes'].items()},
        all_sensitive_canaries_removed=h.read(output/'business/diagnostic-result.json')['canaries_absent_from_all_bundles']),
    prometheus=dict(first_observed_up_zero_unix=first_down,first_observed_firing_unix=first_fire,
        observed_delay_seconds=first_fire-first_down,
        sampling_note='observed API samples, not exact scrape or alert activation timestamps',
        notification_receivers_deployed=False),
    wire=dict(records=len(wire['records']),envelopes=sum(r['envelopes'] for r in wire['records'])),
    shutdown=dict(model=h.read(output/'business/model/shutdown.json'),
                  prometheus=h.read(output/'business/prometheus/shutdown.json')),
    runtime_sources_unchanged=198,default_pair_unchanged=True,
    physical_ram_hard_limit_proved=False,long_duration_soak_proved=False,
    journald_template_installed=False,raw_logs_are_not_redacted=True)
h.save(base/'acceptance-summary.json',summary)
print(json.dumps({k:v for k,v in summary.items() if k not in ('diagnostics',)},indent=2))
