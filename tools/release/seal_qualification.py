"""Audit completed text-v1 evidence; never change earlier failed records."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import pickle
import shutil
import sqlite3


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def read(path):return json.loads(path.read_text())


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--qualification',type=Path,required=True)
    args=ap.parse_args();root=Path.cwd();q=args.qualification.resolve()
    assert not (q/'artifact-binding.json').exists()
    results=read(q/'results.json');assert len(results)==10
    package=q/'package';service=q/'service-trial'
    if (q/'recovery.json').exists():
        recovery=read(q/'recovery.json')
        assert recovery['exit']==0 and results[-1]['step']=='service-trial' and results[-1]['exit']!=0
        assert all(x['exit']==0 for x in results[:-1])
        package=q/recovery['package'];service=q/recovery['service_trial']
        assert package.resolve().is_relative_to(q) and service.resolve().is_relative_to(q)
        for name,h in recovery['runtime_files'].items():
            assert sha(q/'package'/name)==h==sha(package/name)
        completed=read(q/'completed-recovery.json')
    else:
        assert all(x['exit']==0 for x in results)
        completed=read(q/'completed.json')
    digest=completed['binary_sha256']
    assert completed['functional_pass']
    assert sha(package/'q4t')==digest==read(q/'plan.json')['binary_sha256']
    host=read(q/'host/result.json');assert host['passed'] and len(host['required_tests'])==22
    direct=read(q/'input/summary.json');assert direct['failure'] is None and direct['server_exit']==0 and len(direct['rejections'])==55
    assert read(q/'input/identity.json')['binary_sha256']==digest
    raw_rows=0
    for mode,expected in [('quality',11),('performance',15)]:
        d=q/mode;e=read(d/'exit.json')
        assert e['failure'] is None and e['server']==0 and e['http_output_checks_passed']
        assert (d/'binary.sha256').read_text().strip()==digest
        summary=read(d/'results.json');count=0
        for p in d.rglob('benchmark_data.db'):
            with sqlite3.connect('file:'+str(p)+'?mode=ro',uri=True) as db:
                db.row_factory=sqlite3.Row;rows=db.execute('select * from result order by start_time').fetchall()
            for row in rows:
                request=json.loads(row['request']);ph=hashlib.sha256(request['prompt'].encode()).hexdigest()
                ref=next(x for x in summary if x['prompt_sha256']==ph)
                events=pickle.loads(base64.b64decode(row['response_messages']))
                assert row['success'] and request['stream'] and not any('error' in e for e in events)
                choices=[c for e in events for c in e.get('choices',[])]
                text=''.join(c.get('delta',c.get('message',{})).get('content','') for c in choices)
                finish=[c['finish_reason'] for c in choices if c.get('finish_reason')]
                if mode=='quality':
                    assert text==ref['text'] and finish==['stop']
                    assert row['prompt_tokens']==ref['actual_input'] and row['completion_tokens']==ref['actual_output']
                else:
                    assert hashlib.sha256(text.encode()).hexdigest() in ref['outputs'] and finish==['length']
                    assert row['prompt_tokens']==ref['length'] and row['completion_tokens']==256
                count+=1
        assert count==expected;raw_rows+=count
    d=q/'templates';e=read(d/'exit.json')
    assert e['server']==0 and e['failure'] is None and e['completed']==63 and e['template_checks_passed']
    assert read(d/'runtime.json')['sha256']==digest
    manifest=read(d/'manifest.json');parsed=read(d/'responses.json')
    with sqlite3.connect('file:'+str(next(d.rglob('benchmark_data.db')))+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row;rows=db.execute('select * from result order by start_time').fetchall()
    assert len(rows)==len(manifest)==len(parsed)==63
    for row,item,response in zip(rows,manifest,parsed):
        req=json.loads(row['request']);assert all(req[k]==v for k,v in item['request'].items())
        events=pickle.loads(base64.b64decode(row['response_messages']))
        choices=[c for e in events for c in e.get('choices',[])]
        assert row['success'] and not any('error' in e for e in events)
        assert ''.join(c.get('delta',{}).get('content','') for c in choices)==response['text']
        assert row['prompt_tokens']==response['prompt_tokens']==item['prompt_tokens']
        assert row['completion_tokens']==response['completion_tokens']
        assert [c['finish_reason'] for c in choices if c.get('finish_reason')]==response['finish']
    assert all(x['equal'] for x in read(d/'comparison.json')['cases']);raw_rows+=63
    fault=q/'decode-fault'
    assert read(fault/'identity.json')['binary_sha256']==digest
    assert len(read(fault/'summary.json'))==4 and all(x['passed'] for x in read(fault/'summary.json'))
    assert all(read(p)['server_exit']==0 and not read(p).get('failure') for p in fault.glob('*/result.json'))
    # The cancellation driver owns its complete assertion matrix and exit file.
    assert read(q/'cancellation/plan.json')['binary_sha256']==digest
    d=service;exit_record=read(d/'exit.json')
    assert exit_record['failure'] is None and exit_record['stopped']
    assert not (d/'runtime/supervisor.json').exists()
    trial=read(d/'trial.json')
    # The frozen driver samples before its final unconditional five-second
    # sleep. A successful loop exit proves the full window completed; retain
    # the raw last-sample time and report only a derived duration lower bound.
    terminal=next(x for x in exit_record['records'] if x['case']=='1800-second-controlled-trial')
    assert terminal['passed'] and terminal['requests']==len(trial['requests'])
    window_min_seconds=trial['elapsed_seconds']+5
    assert window_min_seconds>=1800 and len(trial['requests'])>=60
    assert trial['peak_rss']-trial['baseline_rss']<=512*1024*1024
    normal=0
    reference=read(q/'quality/results.json')
    for path in sorted(d.glob('*.json')):
        record=read(path)
        if not isinstance(record,dict) or 'events' not in record:continue
        events=record['events'];length=record['length'];ref=next(x for x in reference if x['actual_input']==length)
        assert record['done'] and not any('error' in e for e in events)
        choices=[c for e in events for c in e.get('choices',[])]
        assert ''.join(c.get('delta',{}).get('content','') for c in choices)==ref['text']
        assert [c['finish_reason'] for c in choices if c.get('finish_reason')]==['stop']
        usage=next(e['usage'] for e in events if e.get('usage'))
        assert usage['prompt_tokens']==length and usage['completion_tokens']==ref['actual_output']
        normal+=1
    assert normal==len(trial['requests'])+9  # 4 lifecycle smoke + 5 warmups.
    # Bind the actual test binary, actual cache, build logs and numerical reuse.
    build=Path(host['binary']).parent
    shutil.copy2(build/'q4t_host_tests',q/'host/q4t_host_tests')
    shutil.copy2(__file__,q/'seal_qualification.py')
    for name in ['build.log','configure.log','plan-frozen.md','numerical-reuse.json','fixture-equivalence.json']:
        shutil.copy2(q.parent/name,q/name)
    reuse=read(q/'numerical-reuse.json');assert reuse['identical_device_code'] and not reuse['numerical_source_diff']
    assert reuse['candidate']['binary_sha256']==digest
    assert 'warning:' not in (q/'build.log').read_text()
    for name,digest_expected in read(package/'manifest.json')['files'].items():
        assert sha(package/name)==digest_expected
    summary={'functional_pass':True,'scope':'private text-v1 controlled deployment',
             'binary_sha256':digest,'evalscope_rows_audited':raw_rows,'service_outputs_audited':normal,
             'trial_requests':len(trial['requests']),'trial_seconds':window_min_seconds,
             'trial_duration_kind':'lower bound: last sample plus final mandatory five-second sleep',
             'trial_last_sample_seconds':trial['elapsed_seconds'],
             'performance':'measured repair cost, not strict non-regression proof',
             'numerical_evidence':'unchanged model/quant sources and identical device code; inherited finite-domain evidence',
             'whole_model_oracle':False,'public_multitenant_sla':False}
    (q/'release-acceptance.json').write_text(json.dumps(summary,indent=2))
    binding={str(p.relative_to(q)):sha(p) for p in sorted(q.rglob('*')) if p.is_file()}
    (q/'artifact-binding.json').write_text(json.dumps(binding,indent=2))
    assert all(sha(q/p)==h for p,h in binding.items())
    print(json.dumps({'files':len(binding),**summary}))


if __name__=='__main__':main()
