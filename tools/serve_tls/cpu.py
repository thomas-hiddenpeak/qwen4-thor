"""Grouped source, isolated entry, owned cleanup and official rule scenarios."""
import ast
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True;sys.path.insert(0,str(ROOT))
from tools.serve_tls import observer as o,rules,test_tls
from tools.serve_host import test_host,test_runtime
from tools.serve_notify import notify as n,test_notify
from tools.serve_service import package as pkg

BASE=test_tls.BASE
PROMTOOL=ROOT/'.q4t-work/serve-ops-20261103/deps/promtool'
TESTS=(test_tls,test_host,test_runtime,test_notify)
SOURCES=sorted(set((*o.FILES,*n.V3_SOURCES,'tools/serve_tls/rules.py','tools/serve_tls/cpu.py',
    'tools/serve_tls/test_tls.py','tools/serve_host/cpu.py','tools/serve_host/rules.py',
    'tools/serve_host/test_host.py','tools/serve_host/test_runtime.py','tools/serve_notify/test_notify.py',
    'tools/serve_gateway/test_gateway.py','docs/SERVE_TLS_OBSERVATION_2026-11-04.md')))


def rule_tests():
    target='job="q4t-tls",instance="127.0.0.1:19450"'
    labels=target+',deployment="fixture",generation="'+'a'*64+'"'
    metric=lambda n:'q4t_tls_'+n+'{'+labels+'}'
    enum=lambda n,label,key:'q4t_tls_'+n+'{'+labels+','+label+'="'+key+'"}'
    state=lambda v:enum('state','state',v)
    cert=lambda v:enum('certificate_state','state',v)
    reason=lambda v:enum('reason','reason',v)
    trust=lambda v:enum('trust','trust',v)
    up='up{'+target+'}';stamp=metric('evaluated_timestamp_seconds')
    begin,end=metric('not_before_timestamp_seconds'),metric('not_after_timestamp_seconds')
    baseline={up:'1x80',stamp:'0+5x80',begin:'0x80',end:'10000000x80',
        **{state(v):('1x80' if v=='READY' else '0x80') for v in o.c.STATES},
        **{cert(v):('1x80' if v=='VALID' else '0x80') for v in o.c.CERTIFICATES},
        **{reason(v):('1x80' if v=='none' else '0x80') for v in o.c.REASONS},
        **{trust(v):('1x80' if v=='explicit_ca' else '0x80') for v in o.c.TRUST}}
    definitions={v['alert']:v for v in rules.render()['groups'][0]['rules']};cases=[]
    def case(name,changes,alerts,when='40s',empty=False):
        values={} if empty else baseline.copy()
        for key,value in changes.items():
            if value is None:values.pop(key,None)
            else:values[key]=value
        expected=[]
        for alert,rule in definitions.items():
            found=[]
            if alert in alerts:
                lb=dict(rule['labels'],job='q4t-tls')
                if alert!='Q4TTLSTargetMissing':lb['instance']='127.0.0.1:19450'
                if alert not in ('Q4TTLSTargetMissing','Q4TTLSExporterDown','Q4TTLSMetricContractMissing'):
                    lb.update(deployment='fixture',generation='a'*64)
                found=[dict(exp_labels=lb,exp_annotations=rule['annotations'])]
            expected.append(dict(eval_time=when,alertname=alert,exp_alerts=found))
        cases.append(dict(name=name,interval='5s',input_series=[dict(series=k,values=v) for k,v in values.items()],alert_rule_test=expected))
    missing='Q4TTLSMetricContractMissing'
    case('fresh-ready',{},[])
    case('expiring-material',{end:'604800x80',cert('VALID'):'0x80',cert('EXPIRING'):'1x80'},['Q4TTLSCertificateExpiring'])
    case('expired-boundary',{end:'40x80',cert('VALID'):'0x80',cert('EXPIRED'):'1x80'},['Q4TTLSCertificateInvalid'])
    case('future-material',{begin:'41x80',cert('VALID'):'0x80',cert('NOT_YET_VALID'):'1x80'},['Q4TTLSCertificateInvalid'])
    case('not-before-equality',{begin:'40x80'},[])
    case('renewed-material-resolves',{end:'604800x4 10000000x75'},[])
    case('observer-stale-boundary',{stamp:'0x80'},[],'30s')
    case('observer-stale',{stamp:'0x80'},['Q4TTLSObserverStale'],'35s')
    case('future-skew-boundary',{stamp:'70x80'},[])
    case('future-skew',{stamp:'71x80'},['Q4TTLSObserverStale'])
    case('no-expiry-claim-from-stale-projection',{stamp:'0x80',end:'604800x80'},['Q4TTLSObserverStale'])
    case('observer-recovered',{stamp:'0x7 35 40x70'},[])
    case('source-unknown',{state('READY'):'0x80',state('UNKNOWN'):'1x80',reason('none'):'0x80',reason('source_stale'):'1x80'},['Q4TTLSSourceUnknown'])
    case('package-unavailable',{state('READY'):'0x80',state('UNKNOWN'):'1x80',cert('VALID'):'0x80',cert('UNKNOWN'):'1x80',
        trust('explicit_ca'):'0x80',trust('unknown'):'1x80',begin:None,end:None},['Q4TTLSSourceUnknown'])
    case('stopped-is-not-ready',{state('READY'):'0x80',state('STOPPED'):'1x80'},['Q4TTLSGenerationTerminal'])
    case('failed-expired-keeps-both-facts',{state('READY'):'0x80',state('FAILED'):'1x80',end:'40x80'},
        ['Q4TTLSGenerationTerminal','Q4TTLSCertificateInvalid'])
    case('waiting-long',{state('READY'):'0x80',state('WAITING'):'1x80'},['Q4TTLSGenerationWaiting'],'2m')
    case('unconfigured-trust',{trust('explicit_ca'):'0x80',trust('not_configured'):'1x80'},['Q4TTLSTrustNotPinned'])
    case('target-down',{up:'0x80'},['Q4TTLSExporterDown'])
    case('target-missing',{},['Q4TTLSTargetMissing'],empty=True)
    case('metric-set-empty',{up:'1x80'},[missing],empty=True)
    case('stamp-missing',{stamp:None},[missing])
    case('missing-state',{state('STARTING'):None},[missing])
    case('contradictory-states',{state('STARTING'):'1x80'},[missing])
    case('fractional-states',{state('STARTING'):'.5x80',state('READY'):'.5x80'},[missing])
    case('unknown-enum-label',{state('STARTING'):None,state('BAD'):'0x80'},[missing])
    case('missing-material-boundary',{end:None},[missing])
    case('cross-generation-family',{cert('VALID'):None,cert('VALID').replace('a'*64,'b'*64):'1x80'},[missing])
    case('invalid-negative-timestamp',{stamp:'-1x80'},[missing,'Q4TTLSObserverStale'])
    return dict(rule_files=[str(ROOT/rules.RULE)],evaluation_interval='5s',tests=cases)


def run():
    os.umask(0o077);BASE.mkdir(exist_ok=True);output=BASE/'cpu-01';output.mkdir(mode=0o700)
    sources={n:pkg.sha(ROOT/n) for n in SOURCES}
    for name in sources:
        raw=(ROOT/name).read_bytes();target=output/'source'/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(raw)
        if name.endswith('.py'):ast.parse(raw,filename=name)
    log=io.StringIO();suite=unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromModule(m) for m in TESTS)
    result=unittest.TextTestRunner(stream=log,verbosity=2).run(suite);(output/'tests.log').write_text(log.getvalue())
    pkg.require(pkg.sha(PROMTOOL)==n.BINARIES['promtool'],'promtool identity')
    fixture=rule_tests();pkg.save(output/'rule-tests.json',fixture)
    value=o.c.view('UNKNOWN','source_unavailable');value.update(evaluated_unix=1,certificate=o.c.certificate(None,1))
    wire=o.c.metrics(value,'fixture','a'*64).encode();(output/'metrics.prom').write_bytes(wire)
    commands=[]
    for label,argv,stdin in (('rules',[PROMTOOL,'check','rules',ROOT/rules.RULE],None),
        ('series',[PROMTOOL,'test','rules',output/'rule-tests.json'],None),('metrics',[PROMTOOL,'check','metrics'],wire),
        ('render',[sys.executable,'-B',ROOT/'tools/serve_tls/rules.py','--check'],None)):
        r=subprocess.run(argv,input=stdin,capture_output=True,timeout=45)
        (output/(label+'.stdout')).write_bytes(r.stdout);(output/(label+'.stderr')).write_bytes(r.stderr)
        commands.append(dict(label=label,returncode=r.returncode))
    pkg.save(output/'result.json',dict(passed=result.wasSuccessful() and all(v['returncode']==0 for v in commands),
        tests_run=result.testsRun,errors=len(result.errors),failures=len(result.failures),sources=sources,commands=commands,
        rule_scenarios=len(fixture['tests']),models_started=0,http_started=0,gpu_started=0,
        reused_tls_archive_sha256=pkg.sha(ROOT/'docs/evidence/serve-tls-lifecycle-20261104/records.tar.gz')))
    print(log.getvalue(),end='');print(json.dumps(commands))
    return 0 if pkg.read(output/'result.json')['passed'] else 1


if __name__=='__main__':raise SystemExit(run())
