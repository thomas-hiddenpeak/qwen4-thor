"""Grouped host observer, legacy/v2 notification and official rule checks."""
import ast
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
from tools.serve_host import observer as o,rules,test_host
from tools.serve_notify import notify as n,test_notify
from tools.serve_service import package as pkg

BASE=test_host.BASE
PROMTOOL=ROOT/'.q4t-work/serve-ops-20261103/deps/promtool'
SOURCES=sorted(set((*o.FILES,*n.V2_SOURCES,'tools/serve_host/rules.py','tools/serve_host/cpu.py',
    'tools/serve_host/test_host.py','tools/serve_notify/test_notify.py','docs/SERVE_HOST_OBSERVATION_2026-11-04.md')))


def rule_tests():
    target='job="q4t-host",instance="127.0.0.1:19448"'
    labels=target+',deployment="fixture",generation="'+'a'*64+'"'
    state=lambda key:'q4t_host_state{'+labels+',state="'+key+'"}'
    metric=lambda key:'q4t_host_'+key+'{'+labels+'}'
    kind=lambda key:'q4t_host_snapshot_kind{'+labels+',kind="'+key+'"}'
    event=lambda key:'q4t_host_memory_events_total{'+labels+',event="'+key+'"}'
    up='up{'+target+'}'
    baseline={up:'1x80',metric('evaluated_timestamp_seconds'):'0+5x80',
        **{state(s):('1x80' if s=='RUNNING' else '0x80') for s in o.c.STATES},
        **{kind(k):('1x80' if k=='current' else '0x80') for k in o.c.KINDS},
        metric('memory_current_bytes'):'10x80',metric('memory_max_bytes'):'100x80',
        metric('tasks_current'):'1x80',metric('tasks_max'):'100x80',metric('tasks_limit_events_total'):'0x80',
        **{event(e):'0x80' for e in ('high','max','oom','oom_kill')}}
    defs={v['alert']:v for v in rules.render()['groups'][0]['rules']};cases=[]
    def case(name,changes,alerts,when='40s',empty=False):
        values={} if empty else baseline.copy()
        for key,value in changes.items():
            if value is None:values.pop(key,None)
            else:values[key]=value
        expected=[]
        for alert,rule in defs.items():
            found=[]
            if alert in alerts:
                lb=dict(rule['labels'],job='q4t-host')
                if alert!='Q4THostTargetMissing':lb['instance']='127.0.0.1:19448'
                if alert not in ('Q4THostTargetMissing','Q4THostExporterDown','Q4THostMetricContractMissing'):
                    lb.update(deployment='fixture',generation='a'*64)
                found=[dict(exp_labels=lb,exp_annotations=rule['annotations'])]
            expected.append(dict(eval_time=when,alertname=alert,exp_alerts=found))
        cases.append(dict(name=name,interval='5s',input_series=[dict(series=k,values=v) for k,v in values.items()],
            alert_rule_test=expected))
    case('fresh-running',{},[])
    case('observer-stale-boundary',{metric('evaluated_timestamp_seconds'):'0x80'},[],'30s')
    case('observer-paused',{metric('evaluated_timestamp_seconds'):'0x80'},['Q4THostObserverStale'],'35s')
    case('future-boundary',{metric('evaluated_timestamp_seconds'):'70x80'},[])
    case('future-skew',{metric('evaluated_timestamp_seconds'):'71x80'},['Q4THostObserverStale'])
    case('source-unknown',{state('RUNNING'):'0x80',state('UNKNOWN'):'1x80',kind('current'):'0x80',kind('none'):'1x80',
        metric('memory_current_bytes'):None,metric('tasks_current'):None},['Q4THostSourceUnknown'])
    case('stopped-final',{state('RUNNING'):'0x80',state('STOPPED'):'1x80',kind('current'):'0x80',kind('final'):'1x80'},['Q4THostGenerationTerminal'])
    case('waiting-start-boundary',{state('RUNNING'):'0x80',state('WAITING'):'1x80'},['Q4THostGenerationWaiting'],'2m')
    case('oom-visible-even-on-first-scrape',{event('oom'):'1x80',event('oom_kill'):'3x80'},['Q4THostProtectionFailed'])
    case('pids-visible-even-on-first-scrape',{metric('tasks_limit_events_total'):'1x80'},['Q4THostProtectionFailed'])
    case('protection-lost',{'q4t_host_reason{'+labels+',reason="host_resource_observation_unavailable"}':'1x80'},['Q4THostProtectionFailed'])
    case('reclaimed-max-is-not-oom',{event('max'):'5x80'},[])
    case('throttling-history',{event('high'):'18x80'},['Q4THostThrottlingObserved'])
    case('current-memory-headroom',{metric('memory_current_bytes'):'90x80'},['Q4THostHeadroomLow'])
    case('current-task-headroom',{metric('tasks_current'):'90x80'},['Q4THostHeadroomLow'])
    case('historical-high-is-not-current-headroom',{metric('memory_current_bytes'):'95x80',kind('current'):'0x80',kind('final'):'1x80'},[])
    case('exporter-down',{up:'0x80'},['Q4THostExporterDown'])
    case('expected-target-absent',{},['Q4THostTargetMissing'],empty=True)
    case('state-contract-incomplete',{state('UNKNOWN'):None},['Q4THostMetricContractMissing'])
    case('state-contradictory',{state('UNKNOWN'):'1x80'},['Q4THostMetricContractMissing','Q4THostSourceUnknown'])
    case('unknown-recovery',{state('RUNNING'):'0 0 1x78',state('UNKNOWN'):'1 1 0x78'},[])
    case('freshness-recovery',{metric('evaluated_timestamp_seconds'):'0 0 0 0 0 0 0 35 40 45'},[],'45s')
    return dict(rule_files=[str(ROOT/rules.RULE)],evaluation_interval='5s',tests=cases)


def run():
    os.umask(0o077);output=BASE/'cpu-01';output.mkdir(mode=0o700)
    sources={name:pkg.sha(ROOT/name) for name in SOURCES}
    for name in sources:
        raw=(ROOT/name).read_bytes();dest=output/'source'/name;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(raw)
        if name.endswith('.py'):ast.parse(raw,filename=name)
    log=io.StringIO();suite=unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromModule(m) for m in (test_host,test_notify))
    result=unittest.TextTestRunner(stream=log,verbosity=2).run(suite);(output/'tests.log').write_text(log.getvalue())
    pkg.require(pkg.sha(PROMTOOL)==n.BINARIES['promtool'],'promtool changed')
    fixture=rule_tests();pkg.save(output/'rule-tests.json',fixture)
    sample=o.c.view('UNKNOWN','source_stale');sample['evaluated_unix']=1
    wire=o.c.metrics(sample,'fixture','a'*64).encode();(output/'metrics.prom').write_bytes(wire)
    commands=[]
    for label,argv,stdin in (('rules',[PROMTOOL,'check','rules',ROOT/rules.RULE],None),
        ('series',[PROMTOOL,'test','rules',output/'rule-tests.json'],None),
        ('metrics',[PROMTOOL,'check','metrics'],wire),
        ('render',[sys.executable,'-B',ROOT/'tools/serve_host/rules.py','--check'],None)):
        value=subprocess.run(argv,input=stdin,capture_output=True,timeout=45)
        (output/(label+'.stdout')).write_bytes(value.stdout);(output/(label+'.stderr')).write_bytes(value.stderr)
        commands.append(dict(label=label,argv=list(map(str,argv)),returncode=value.returncode))
    pkg.save(output/'result.json',dict(passed=result.wasSuccessful() and all(v['returncode']==0 for v in commands),
        tests_run=result.testsRun,errors=len(result.errors),failures=len(result.failures),sources=sources,
        commands=commands,rule_scenarios=len(fixture['tests']),models_started=0,listeners_started=0,gpu_started=0,
        reused_host_evidence_sha256=pkg.sha(ROOT/'docs/evidence/serve-host-envelope-20261104/records.tar.gz')))
    print(log.getvalue(),end='');print(json.dumps(commands))
    return 0 if pkg.read(output/'result.json')['passed'] else 1


if __name__=='__main__':raise SystemExit(run())
