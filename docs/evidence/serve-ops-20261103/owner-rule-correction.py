"""Reproduce the idle-rule omission with actual scrape semantics, then repair."""
import copy
import json
from pathlib import Path
import subprocess
import sys

ROOT=next(p for p in Path(__file__).resolve().parents if (p/'AGENTS.md').exists())
sys.path.insert(0,str(ROOT))
from tools.serve_ops import stage,rule_tests
h=stage.h;out=stage.BASE/'cpu/owner-correction';out.mkdir()
old=subprocess.check_output(['git','show','52ada556:deploy/serve-ops/q4t.rules.yml'],cwd=ROOT)
(out/'before.rules.json').write_bytes(old)
(out/'after.rules.json').write_bytes((ROOT/'deploy/serve-ops/q4t.rules.yml').read_bytes())
before=json.loads(old)['groups'][0]['rules'];after=h.read(out/'after.rules.json')['groups'][0]['rules']
changed=[(a.get('alert'),b.get('alert')) for a,b in zip(before,after) if a!=b]
h.require(changed==[('Q4TWorkerReapStalled','Q4TWorkerReapStalled'),
                   ('Q4TIdleResourcesRetained','Q4TIdleResourcesRetained')],'unexpected policy changes')
selected={'healthy idle','normal busy progressing and explicit cancellation','idle worker ownership',
          'idle body retained','idle slot retained','another classified body receiver still owns resources'}
fixture=rule_tests.build(out/'after.rules.json')
fixture['tests']=[case for case in fixture['tests'] if case['name'] in selected]
policy={r['alert']:r for r in after if 'alert' in r}
for name in ('before','after'):
    path=stage.OUTPUT/f'business/diagnostics/{name}/report.json'
    report=h.read(path)
    h.require(report['metrics']['q4t_business_connections']==1 and
              report['metrics']['q4t_active_chats']==0,'actual observed scrape condition changed')
    series=[dict(series=rule_tests.label(metric),values=f'{value}x64')
            for metric,value in dict(report['metrics'],up=1).items()]
    checks=[dict(eval_time='15m',alertname=alert,exp_alerts=[]) for alert in policy]
    fixture['tests'].append(dict(name='actual captured '+name,interval='15s',
                                input_series=series,alert_rule_test=checks))
h.save(out/'fixed-tests.json',fixture)
negative=copy.deepcopy(fixture);negative['rule_files']=[str(out/'before.rules.json')]
h.save(out/'original-tests.json',negative)
tool=str(stage.BASE/'deps/promtool');commands={}
for name,argv in [('old-omission',[tool,'test','rules',str(out/'original-tests.json')]),
                  ('fixed-rules',[tool,'check','rules',str(out/'after.rules.json')]),
                  ('fixed-timeseries',[tool,'test','rules',str(out/'fixed-tests.json')])]:
    with (out/(name+'.stdout')).open('xb') as stdout,(out/(name+'.stderr')).open('xb') as stderr:
        process=subprocess.run(argv,cwd=ROOT,stdout=stdout,stderr=stderr,timeout=60)
    commands[name]=dict(argv=argv,returncode=process.returncode)
    print(name,process.returncode,flush=True)
stderr=(out/'old-omission.stderr').read_text()
h.require(commands['old-omission']['returncode']==1 and
          all('name: '+name+',' in stderr for name in ('idle worker ownership','idle body retained','idle slot retained'))
          and stderr.count('name: ')==3,'expected old-rule omissions not demonstrated')
h.require(commands['fixed-rules']['returncode']==commands['fixed-timeseries']['returncode']==0,'rule fix failed')
h.save(out/'result.json',dict(passed=True,old_omission_demonstrated=True,commands=commands,
    affected_rules=[a for a,b in changed],scenarios=len(fixture['tests']),
    assertions=sum(len(case['alert_rule_test']) for case in fixture['tests']),
    actual_metric_evidence={name:h.identity(stage.OUTPUT/f'business/diagnostics/{name}/report.json') for name in ('before','after')},
    corrected_rule_sha256=h.sha(ROOT/'deploy/serve-ops/q4t.rules.yml'),
    correction_source_hashes={str(p.relative_to(ROOT)):h.sha(p) for p in (
        ROOT/'tools/serve_ops/rules.py',ROOT/'tools/serve_ops/fixtures.py',ROOT/'tools/serve_ops/rule_tests.py')},
    runtime_changed=False,model_or_http_repeated=False,unaffected_15_alerts_and_2_records_reused=True,
    frozen_real_run_retained=True,actual_live_monitor_used_original_rules=True))
