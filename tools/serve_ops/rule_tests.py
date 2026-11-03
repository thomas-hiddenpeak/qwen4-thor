"""Independent time-series scenarios for the official promtool rule evaluator."""
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tools.serve_ops import contracts, fixtures, rules

ALERTS = (
    'Q4TTargetDown','Q4TTargetMissing','Q4TMetricContractMissing',
    'Q4TMetricContractInvalid','Q4TGpuUnhealthy','Q4TMemoryObservationUnavailable',
    'Q4TMemoryPressure','Q4TMemoryGateDisabled','Q4TNotReady','Q4TAdmissionRejections',
    'Q4TStructuredFailures','Q4TChatErrorsObserved','Q4TTransportWriteFailures',
    'Q4TTransportReceiveFailures','Q4TWorkerReapStalled','Q4TIdleResourcesRetained',
    'Q4TExecutionProgressStalled')


def label(name, instance='a:8080', job='q4t'):
    return (name[:-1]+',' if '{' in name else name+'{')+f'job="{job}",instance="{instance}"}}'


def build(rule_path, *, version=4):
    policy={r['alert']:r for r in rules.render(version=version)['groups'][0]['rules'] if 'alert' in r}
    assert set(policy)==set(ALERTS)
    base=contracts.parse_metrics(fixtures.text_metrics(version=version),version=version)[0]
    base.update(up=1)
    tests=[]
    def scenario(name, changes=None, checks=(), *, omit=(), instances=('a:8080',), all_clear=False):
        series=[]
        for instance in instances:
            for metric, value in base.items():
                if metric in omit and instance=='a:8080': continue
                seq=(changes or {}).get(metric, f'{value}x64') if instance=='a:8080' else f'{value}x64'
                series.append(dict(series=label(metric,instance),values=seq))
        # An unrelated scrape job must never trigger q4t rules.
        series.append(dict(series=label('q4t_gpu_healthy','other:80','unrelated'),values='0x64'))
        units=[]
        for alert, seconds, expected in checks:
            rule=policy[alert]
            rows=[]
            for instance in expected:
                labels=dict(job='q4t',**rule['labels'])
                if instance is not None: labels['instance']=instance
                rows.append(dict(exp_labels=labels,exp_annotations=rule['annotations']))
            units.append(dict(eval_time=f'{seconds}s',alertname=alert,exp_alerts=rows))
        if all_clear:
            units.extend(dict(eval_time='15m',alertname=alert,exp_alerts=[]) for alert in ALERTS)
        tests.append(dict(name=name,interval='15s',input_series=series,alert_rule_test=units))

    # Active interval is [60, 660); independent expected for boundaries/recovery.
    def gate(name, changes, alert, delay):
        seq={}
        for key,value in changes.items():
            active=('stale _x38' if value=='_' else ' '.join(['NaN']*40)
                    if value=='NaN' else f'{value}x39')
            seq[key]=f'{base[key]}x3 {active} {base[key]}x20'
        scenario(name,seq,[(alert,45,[]),
            (alert,60+delay-15,[]) if delay else (alert,45,[]),
            (alert,60+delay,['a:8080']),(alert,660,[])],instances=('a:8080','b:8080'))

    scenario('healthy idle',all_clear=True)
    scenario('normal busy progressing and explicit cancellation',{
        'q4t_active_chats':'2x64','q4t_business_connections':'2x64',
        'q4t_structured_active':'1x64','q4t_structured_workers_active':'1x64',
        'q4t_seq_slots_free':'0x64','q4t_execution_steps_total':'0+1x64',
        'q4t_requests_aborted_total':'0+1x64',
        'q4t_requests_aborted_by_reason_total{reason="explicit_cancel"}':'0+1x64',
        'q4t_http_write_failures_total{origin="handler",reason="explicit_cancel"}':'0+1x64',
        'q4t_http_write_failures_total{origin="handler",reason="peer_closed"}':'0+1x64'},all_clear=True)
    gate('target unreachable',{'up':0},'Q4TTargetDown',60)
    scenario('all target series absent',{'up':'_x64'},[
        ('Q4TTargetMissing',105,[]),('Q4TTargetMissing',120,[None])])
    gate('scrape contract missing',{'q4t_ready':'_'},'Q4TMetricContractMissing',30)
    gate('boolean contradiction',{'q4t_ready':2},'Q4TMetricContractInvalid',30)
    gate('nonfinite observation',{'q4t_ready':'NaN'},'Q4TMetricContractInvalid',30)
    gate('gpu sticky failure',{'q4t_gpu_healthy':0},'Q4TGpuUnhealthy',0)
    gate('memory read failed',{'q4t_memory_observation_valid':0,
        'q4t_memory_admission_open':0,'q4t_memory_admission_state{state="open"}':0,
        'q4t_memory_admission_state{state="memory_observation_unavailable"}':1},
        'Q4TMemoryObservationUnavailable',30)
    gate('memory pressure',{'q4t_memory_admission_open':0,'q4t_ready':0,
        'q4t_memory_admission_state{state="open"}':0,
        'q4t_memory_admission_state{state="memory_pressure"}':1},'Q4TMemoryPressure',30)
    disabled={'q4t_memory_admission_enabled':'0x64','q4t_memory_observation_valid':'0x64',
        'q4t_memory_available_bytes':'0x64','q4t_memory_admission_state{state="open"}':'0x64',
        'q4t_memory_admission_state{state="disabled"}':'1x64'}
    scenario('explicitly disabled without unknown-memory false positive',disabled,[
        ('Q4TMemoryGateDisabled',285,[]),('Q4TMemoryGateDisabled',300,['a:8080']),
        ('Q4TMemoryObservationUnavailable',900,[]),('Q4TMetricContractInvalid',900,[])])
    gate('unready outside gpu memory',{'q4t_ready':0},'Q4TNotReady',120)
    gate('idle worker ownership',{'q4t_structured_workers_active':1},'Q4TWorkerReapStalled',60)
    gate('idle body retained',{'q4t_request_body_bytes':1024},'Q4TIdleResourcesRetained',60)
    gate('idle slot retained',{'q4t_seq_slots_free':0},'Q4TIdleResourcesRetained',60)
    scenario('another classified body receiver still owns resources',{
        'q4t_business_connections':('1x64' if version == 4 else '2x64'),
        'q4t_request_body_bytes':'1024x64',
        'q4t_structured_workers_active':'1x64'},all_clear=True)
    gate('execution boundary stopped',{'q4t_active_chats':1,'q4t_business_connections':1,
                                     'q4t_seq_slots_free':0},'Q4TExecutionProgressStalled',300)
    # Single increases disappear from the 5m window; no automatic retries.
    for name,metric,alert,delay in (
        ('queue reject','q4t_queue_rejected_total','Q4TAdmissionRejections',60),
        ('memory reject','q4t_memory_rejected_total{checkpoint="execute",reason="memory_pressure"}',
            'Q4TAdmissionRejections',60),
        ('body capacity','q4t_http_receive_failures_total{reason="body_capacity"}','Q4TAdmissionRejections',60),
        ('compiler error','q4t_structured_prepare_failed_total','Q4TStructuredFailures',0),
        ('input chat error','q4t_requests_error_total','Q4TChatErrorsObserved',0),
        ('write deadline','q4t_http_write_failures_total{origin="handler",reason="write_deadline"}',
            'Q4TTransportWriteFailures',0),
        ('receive socket error','q4t_http_receive_failures_total{reason="socket_error"}',
            'Q4TTransportReceiveFailures',0)):
        scenario(name,{metric:'0x3 1x60'},[(alert,45,[]),
            (alert,60+delay-15,[]) if delay else (alert,45,[]),
            (alert,60+delay,['a:8080']),(alert,375,[])],instances=('a:8080','b:8080'))
    scenario('counter reset alone is not a new failure',{
        'q4t_requests_error_total':'100x3 0x60','q4t_queue_rejected_total':'100x3 0x60'},all_clear=True)
    scenario('stale target series does not remain healthy',{'up':'1x3 stale _x60'},[
        ('Q4TTargetDown',300,[]),('Q4TTargetMissing',165,[]),('Q4TTargetMissing',180,[None])])
    if version == 4:
        gate('observation family must not disappear', {'q4t_observation_connections':'_'},
             'Q4TMetricContractMissing',30)
        for kind in ('unclassified','business','observation','control'):
            alert='Q4TAdmissionRejections'
            metric=f'q4t_connection_rejections_total{{kind="{kind}"}}'
            scenario('connection rejection '+kind,{metric:'0x3 1x60'},[
                (alert,105,[]),(alert,120,['a:8080']),(alert,375,[])],instances=('a:8080','b:8080'))
    return dict(rule_files=[str(rule_path)],evaluation_interval='15s',tests=tests)


if __name__=='__main__':
    if len(sys.argv)!=2: raise SystemExit('output.json (new path only)')
    with Path(sys.argv[1]).open('x') as stream:
        json.dump(build(ROOT/'deploy/serve-ops/q4t.rules.yml'),stream,indent=2)
