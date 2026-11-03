"""Render the reviewed local operations policy as Prometheus rules (YAML JSON)."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_ops.contracts import BOOLEAN, FAMILIES, METRICS

TARGET = '{job="q4t"}'
UP = 'up{job="q4t"} == 1'
BY = 'job, instance'
RUNBOOK = 'https://github.com/thomas-hiddenpeak/qwen4-thor/blob/codex/main-reaudit-fixes/docs/SERVE_OPERATIONS.md'


def series(name):
    return 'q4t_'+name+TARGET


def increased(name):
    return f'increase({series(name)}[5m]) > 0'


def render():
    rules = []
    def alert(name, expr, duration, severity, summary, section, *, live=True):
        if live:
            expr = f'max by ({BY}) ({expr}) and on ({BY}) ({UP})'
        rules.append(dict(alert=name, expr=expr, **({'for':duration} if duration else {}),
            labels=dict(severity=severity, component='q4t-serve'),
            annotations=dict(summary=summary, runbook_url=RUNBOOK+'#'+section)))
    alert('Q4TTargetDown', 'up{job="q4t"} == 0', '1m', 'critical',
          'The configured q4t scrape target is unreachable.', 'unavailable', live=False)
    alert('Q4TTargetMissing', 'absent(up{job="q4t"})', '2m', 'critical',
          'No q4t target series is present; inspect discovery and monitoring.', 'unavailable', live=False)
    pattern = '|'.join(sorted(FAMILIES))
    present = f'count by ({BY}) ({{job="q4t",__name__=~"{pattern}"}}) == {len(METRICS)}'
    alert('Q4TMetricContractMissing', f'({UP}) unless on ({BY}) ({present})', '30s', 'critical',
          'The operational metric contract is incomplete.', 'observation', live=False)
    booleans = '{job="q4t",__name__=~"'+'|'.join(sorted(BOOLEAN | {'q4t_memory_admission_state'}))+'"}'
    invalid = (f'({booleans} != 0 and on (__name__, job, instance, state) {booleans} != 1) or '
               f'(sum by ({BY}) (q4t_memory_admission_state{{job="q4t"}}) != 1) or '
               f'({series("seq_slots_free")} > on ({BY}) {series("seq_slots_total")})')
    alert('Q4TMetricContractInvalid', invalid, '30s', 'critical',
          'Operational observations are contradictory or invalid.', 'observation')
    alert('Q4TGpuUnhealthy', series('gpu_healthy')+' == 0', '', 'critical',
          'The server has observed a sticky GPU failure.', 'gpu')
    alert('Q4TMemoryObservationUnavailable', f'{series("memory_admission_enabled")} == 1 and '
          f'{series("memory_observation_valid")} == 0', '30s', 'critical',
          'Memory observation is unavailable and new work is refused.', 'memory')
    alert('Q4TMemoryPressure', 'q4t_memory_admission_state{job="q4t",state="memory_pressure"} == 1',
          '30s', 'warning', 'The memory gate is closed to new work.', 'memory')
    alert('Q4TMemoryGateDisabled', series('memory_admission_enabled')+' == 0', '5m', 'information',
          'Global memory admission is explicitly disabled; verify deployment intent.', 'memory')
    alert('Q4TNotReady', f'{series("ready")} == 0 and {series("gpu_healthy")} == 1 and '
          f'{series("memory_admission_open")} == 1', '2m', 'warning',
          'The server remains unready outside the GPU and memory gates.', 'unavailable')
    rejection = ' or '.join(increased(n) for n in ('queue_rejected_total',
        'text_admission_rejected_total', 'structured_rejected_total'))
    rejection += ' or (increase(q4t_memory_rejected_total{job="q4t"}[5m]) > 0)'
    rejection += ' or (increase(q4t_http_receive_failures_total{job="q4t",reason="body_capacity"}[5m]) > 0)'
    alert('Q4TAdmissionRejections', rejection, '1m', 'warning',
          'Admission rejections increased; counters are separate views, not an additive failure total.', 'capacity')
    alert('Q4TStructuredFailures', ' or '.join(increased(n) for n in (
        'structured_prepare_failed_total', 'structured_compile_timeouts_total',
        'structured_resource_exhausted_total')), '', 'warning',
        'Structured preparation failures increased; inspect fixed error codes.', 'structured')
    alert('Q4TChatErrorsObserved', increased('requests_error_total'), '', 'information',
          'Chat errors increased; this includes input rejection and is not a model error rate.', 'errors')
    alert('Q4TTransportWriteFailures',
          'increase(q4t_http_write_failures_total{job="q4t",reason=~"write_deadline|socket_error|exception"}[5m]) > 0',
          '', 'warning', 'Unexpected write failures increased; delivery and task outcome differ.', 'transport')
    alert('Q4TTransportReceiveFailures',
          'increase(q4t_http_receive_failures_total{job="q4t",reason=~"header_deadline|body_deadline|socket_error|exception"}[5m]) > 0',
          '', 'warning', 'Receive deadlines or transport faults increased.', 'transport')
    # /metrics itself owns one non-control connection (chat_http.cpp).
    # Extra classified connections may still be receiving a body; do not
    # mistake those owners for a leak before chat admission.
    idle = f'{series("active_chats")} == 0 and {series("business_connections")} <= 1'
    alert('Q4TWorkerReapStalled', f'{series("structured_workers_active")} > 0 and '
          f'{series("structured_active")} == 0 and {series("structured_preparing")} == 0 and ({idle})',
          '1m', 'warning', 'A worker remains owned after requests and structured leases have drained.', 'owners')
    retained = (f'{series("request_body_bytes")} > 0 or {series("structured_active")} > 0 or '
                f'({series("seq_slots_free")} < on ({BY}) {series("seq_slots_total")})')
    alert('Q4TIdleResourcesRetained', f'({retained}) and ({idle})', '1m', 'warning',
          'Resources remain retained while observed business work is idle.', 'owners')
    alert('Q4TExecutionProgressStalled', f'({series("active_chats")} > 0) and '
          f'({series("seq_slots_free")} < on ({BY}) {series("seq_slots_total")}) and '
          f'(changes({series("execution_steps_total")}[5m]) == 0)', '5m', 'warning',
          'No execution boundary progress was observed; investigate before acting.', 'progress')
    rules.append(dict(record='q4t:local_response_p95_seconds:5m',
        expr='histogram_quantile(0.95, sum by (job, instance, le) '
             '(rate(q4t_response_seconds_bucket{job="q4t"}[5m])))'))
    rules.append(dict(record='q4t:slot_wait_p95_seconds:5m',
        expr='histogram_quantile(0.95, sum by (job, instance, le) '
             '(rate(q4t_slot_wait_seconds_bucket{job="q4t"}[5m])))'))
    return dict(groups=[dict(name='q4t-operational', interval='15s', rules=rules)])


if __name__ == '__main__':
    path = ROOT/'deploy/serve-ops/q4t.rules.yml'
    raw = json.dumps(render(), indent=2)+'\n'
    if sys.argv[1:] == ['--check']:
        if path.read_text() != raw:
            raise SystemExit('operational rules differ from reviewed policy')
    elif sys.argv[1:] == ['--write']:
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(raw)
    else:
        raise SystemExit('--check | --write')
