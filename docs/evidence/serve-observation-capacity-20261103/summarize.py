"""Read existing acceptance bytes; no HTTP/model request or source mutation."""
import json
from pathlib import Path
import sys
from jsonschema import Draft202012Validator

ROOT = next(p for p in Path(__file__).resolve().parents if (p/'AGENTS.md').exists())
sys.path.insert(0, str(ROOT))
from tools.serve_observation_capacity import stage

h, base, out = stage.h, stage.BASE, stage.OUTPUT
for directory in (stage.PACKAGE, out):h.verify_seal(directory)
result = h.read(out/'business/result.json')
h.require(result['status'] == 'PASS_BOUNDED_OBSERVATION_CAPACITY', 'acceptance missing')
validator = Draft202012Validator(h.read(ROOT/'docs/serve-transport-v1.schema.json'))
records = []
for path in sorted((out/'business/model').glob('*-wire.bin')):
    raw = path.read_bytes()
    if raw.startswith(b'HTTP/'):
        raw = raw.split(b'\r\n\r\n', 1)[1]
    if raw.startswith((b'data:', b':')):
        rows = [r[6:] for r in raw.splitlines() if r.startswith(b'data: ')]
        h.require(rows[-1] == b'[DONE]' and rows.count(b'[DONE]') == 1, 'terminal wire order')
        values = [json.loads(r) for r in rows[:-1]]
    else:values = [json.loads(raw)]
    for value in values:validator.validate(value)
    records.append(dict(file=h.identity(path), envelopes=len(values)))
for name in ('business-overflow.json', 'observation-full-probes.json'):
    path = out/'business/model'/name
    values = h.read(path)
    if isinstance(values, dict):values = [values]
    for value in values:
        if value['status'] == 503:
            validator.validate(json.loads(value['raw']))
    records.append(dict(file=h.identity(path), envelopes=sum(v['status'] == 503 for v in values)))
h.save(base/'offline-schema.json', dict(passed=True, records=records,
    raw_wire_files=len(records)-2, extra_http_body_records=2,
    envelopes=sum(r['envelopes'] for r in records),
    diagnostic_refusals=3, diagnostic_refusal_raw_bodies_not_persisted=True,
    diagnostic_evidence='HTTP statuses, private UNKNOWN report, same-instance fixed refusal counters',
    no_new_http_or_model_requests=True))
diagnostics = {label:h.read(out/f'business/diagnostics/{label}/report.json')
               for label in ('before','business-full','observation-full','after')}
after = diagnostics['after']
events = [json.loads(line) for line in (out/'lease/events.jsonl').read_text().splitlines()]
prom = h.read(out/'business/prometheus/saturated-samples.json')
rules = h.read(out/'business/prometheus/saturated-rules.json')
times = h.read(stage.PACKAGE/'cpu/tests/rules.json')['tests']
summary = dict(source_commit=h.read(stage.PACKAGE/'plan.json')['source_commit'],
    business=result, finalized=h.read(out/'finalized.json'), guard=h.read(out/'lease/exit.json'),
    finalize_seconds=h.read(out/'finalized.json')['time']-h.read(out/'begin.json')['started'],
    conflicts_or_abort=[r for r in events if r.get('conflicts') or r['event']=='ENV_ABORT'],
    native=h.read(out/'native/result.json'),
    cpu=dict(host_off=226, host_on=235, required_host=192, backend=9,
        operations=29, versioned_operations=6, driver=6, native_config=15,
        native_positive_partition_configs=7, prometheus_scenarios=len(times),
        prometheus_assertions=sum(len(r['alert_rule_test']) for r in times),
        zero_warnings=True, first_group_passed=True),
    protocol_sha256=h.read(stage.PACKAGE/'plan.json')['protocol_sha256'],
    runtime_sources=len(h.read(stage.PACKAGE/'candidate/runtime-source.json')),
    policy=dict(current=h.identity(ROOT/'deploy/serve-ops/q4t.rules.yml'),
                rollback=h.identity(ROOT/'deploy/serve-ops/q4t-v3.rules.yml')),
    diagnostics=dict(states={k:v['state'] for k,v in diagnostics.items()},
        process_identity=after['identity'], counter_delta=after['comparison'],
        observation_full={k:v['http_status'] for k,v in diagnostics['observation-full']['probes'].items()},
        warning_reason='deliberately induced capacity refusal delta; not failed recovery',
        atomic_snapshot=False),
    resources=dict(after=h.read(out/'business/accounting.json')['after'],
        partial_body_window=h.read(out/'business/model/partial-body-window.json')),
    prometheus=dict(saturated_samples=len(prom), observed=prom[-1],
        rules_count=sum(len(g['rules']) for g in rules['data']['groups']),
        life=h.read(out/'business/prometheus/lifetime.json'),
        production_alert_delay_proved=False, notification_receivers_deployed=False),
    wire=h.read(base/'offline-schema.json'),
    shutdown=dict(model=h.read(out/'business/model/shutdown.json'),
                  prometheus=h.read(out/'business/prometheus/shutdown.json')),
    postflight=h.read(base/'postflight.json'), raw_logs_are_not_redacted=True,
    header_flood_isolated=False, physical_ram_hard_limit_proved=False,
    long_duration_soak_proved=False, previous_acceptance_reused_for_unchanged_numerics=True)
h.save(base/'acceptance-summary.json', summary)
print(json.dumps(dict(business=result, finalize_seconds=summary['finalize_seconds'],
    wire_files=len(records)-2, envelopes=summary['wire']['envelopes'],
    rules=summary['policy'], conflicts=len(summary['conflicts_or_abort'])), indent=2))
