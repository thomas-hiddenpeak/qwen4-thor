"""Post-gate real HTTP traces and logits allocation observations."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'verify'))
from serve_logits_gate import ROOT, require_http
from run_prefill_consumers import parse


def save(path, value):
    path.write_text(json.dumps(value, indent=2)+'\n')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--accepted-root', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    a = ap.parse_args()
    run, out = a.accepted_root.resolve(), a.output.resolve()
    assert any(out.is_relative_to(ROOT / p) for p in ['build', '.q4t-work'])
    require_http(run)
    for kind in ['text', 'vision']:
        assert json.loads((run/(kind+'-precision-review.json')).read_text())['accepted_for_observed_cases']
    assert json.loads((run/'budget-numerical/review.json').read_text())['accepted']
    out.mkdir(parents=True, exist_ok=False)
    shutil.copy2(__file__, out)
    source = ROOT/'tools/verify/serve_logits_alloc.cpp.in'
    shutil.copy2(source, out)
    library = out/'alloc.so'
    command = ['g++-14','-std=c++23','-Wall','-Wextra','-shared','-fPIC',
               '-I/usr/local/cuda/include','-x','c++',str(source),'-ldl','-o',str(library)]
    save(out/'build-command.json', command)
    with (out/'build.log').open('w') as log:
        subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
    assert 'warning' not in (out/'build.log').read_text().lower()
    for scenario, cases in [('fallback',['45056']), ('vision',['image','video']), ('mtp',['45056'])]:
        for version, group in [('parent','parent-before'),('candidate','candidate')]:
            directory = out/(scenario+'-'+version)
            directory.mkdir()
            baseline = run/('consumers-'+scenario)/group
            command = json.loads((baseline/'server-command.json').read_text())
            env = {k:v for k,v in os.environ.items() if not k.startswith('Q4T_') and k!='LD_PRELOAD'}
            env['LD_PRELOAD'] = str(library)
            if scenario=='fallback': env['Q4T_CAPTURE_SCHED_FAILURE']='1'
            if scenario=='mtp': env['Q4T_SCHED_DEBUG']='1'
            session = 'q4t-capacity-'+scenario+'-'+version+'-'+str(os.getpid())
            launch = ['nsys','launch','--session-new='+session,'--trace=cuda',
                      '--discard-environment=true']+command
            save(directory/'command.json', launch)
            save(directory/'environment.json', {k:v for k,v in env.items() if k.startswith('Q4T_') or k=='LD_PRELOAD'})
            failure = None
            with (directory/'server.log').open('w') as log, (directory/'nsys.log').open('w') as trace:
                server = subprocess.Popen(launch, env=env, stdout=log, stderr=subprocess.STDOUT)
                try:
                    deadline = time.monotonic()+240
                    while 'serving on port' not in (directory/'server.log').read_text():
                        assert server.poll() is None and time.monotonic()<deadline
                        time.sleep(1)
                    startup = (directory/'server.log').read_text()
                    if scenario=='fallback':
                        assert '[logits-alloc-fault]' in startup
                        assert 'scheduler logits alloc failed; plain decode' in startup
                    for name in cases:
                        case = directory/name
                        case.mkdir()
                        start = ['nsys','start','--session='+session,'--sample=none',
                                 '--cpuctxsw=none','--output='+str(case/'trace')]
                        subprocess.run(start, stdout=trace, stderr=subprocess.STDOUT, check=True)
                        client = json.loads((baseline/name/'command.json').read_text())
                        client[client.index('--number')+1]='1'
                        client[client.index('--outputs-dir')+1]=str(case)
                        save(case/'command.json', client)
                        with (case/'client.log').open('w') as clog:
                            subprocess.run(client, stdout=clog, stderr=subprocess.STDOUT, check=True)
                        subprocess.run(['nsys','stop','--session='+session], stdout=trace,
                                       stderr=subprocess.STDOUT, check=True)
                        payload = json.loads((run/('consumers-'+scenario)/(name+'.jsonl')).read_text())
                        actual, = parse(case, payload, expected_count=1)
                        expected = json.loads((baseline/name/'parsed.json').read_text())[0]
                        for field in ['prompt_tokens','completion_tokens','output_sha256']:
                            assert actual[field]==expected[field], (scenario,version,field)
                except Exception as error:
                    failure = repr(error)
                    raise
                finally:
                    shutdown = subprocess.run(['nsys','shutdown','--session='+session,'--kill=sigterm'],
                                              stdout=trace, stderr=subprocess.STDOUT)
                    try: server.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        server.kill(); server.wait()
                    save(directory/'exit.json', {'failure':failure,'shutdown':shutdown.returncode,
                                                 'launcher':server.returncode})
                assert shutdown.returncode==0 and server.returncode==0
            for name in cases:
                case = directory/name
                with (case/'export.log').open('w') as log:
                    subprocess.run(['nsys','export','--type=sqlite','--output='+str(case/'trace.sqlite'),
                                    str(case/'trace.nsys-rep')], stdout=log, stderr=subprocess.STDOUT, check=True)
            print(scenario,version,'HTTP matched; captures exported', flush=True)
    save(out/'complete.json', {'captures':8,'review_pending':True})


if __name__=='__main__':
    main()
