"""Retain the failed worker-count assertion; reuse model proof, run probes only."""
import argparse
import datetime
import json
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tools.serve_startup_memory import stage as original, verify, accounting
from tools.serve_lifecycle.observe import ObservationAbort
from tools.serve_admission import verify as base
from jsonschema import Draft202012Validator

h,a=original.h,original.a
ORIGINAL_OUTPUT=original.OUTPUT
BASE=ROOT/'.q4t-work/serve-startup-memory-correction-20261103'
PACKAGE,OUTPUT=BASE/'package',BASE/'execution'
ORIGINAL_CHECK,ORIGINAL_RELEASE=original.check,original.release
COUNTS=dict(generations=5,negatives=2,starts=3,probes=0)


def accounting_contract(rows):
    expected=dict(q4t_requests_total=5,q4t_requests_success_total=5,
        q4t_requests_error_total=0,q4t_requests_aborted_total=0,
        q4t_structured_workers_started_total=3,q4t_structured_workers_ready_total=3,
        q4t_structured_workers_reaped_total=3,q4t_seq_slots_total=1,q4t_seq_slots_free=1,
        q4t_ready=1)
    for name,value in expected.items():h.require(rows[name]==value,'corrected accounting: '+name)
    h.require(all(rows[name]==0 for name in base.EMPTY),'owned business resources not reclaimed')


def audit_original():
    h.verify_seal(original.PACKAGE);h.verify_seal(ORIGINAL_OUTPUT)
    value=h.read(ORIGINAL_OUTPUT/'business/result.json')
    h.require(value['status']=='FAILED' and value['error']=="RuntimeError('business accounting')" and
              value['counts']==COUNTS,'only the frozen assertion failure is reusable')
    guard=h.read(ORIGINAL_OUTPUT/'lease/exit.json')
    h.require(guard['status']=='COMMAND_FAILED' and guard['child_returncode']==1 and
              'ENV_ABORT' not in (ORIGINAL_OUTPUT/'lease/events.jsonl').read_text(),'original environment invalid')
    h.require(h.read(ORIGINAL_OUTPUT/'native/result.json')['passed'] and
              h.read(ORIGINAL_OUTPUT/'native/result.json')['required_tests']==24,'native proof missing')
    folder=ORIGINAL_OUTPUT/'business/model'
    h.require(h.read(folder/'shutdown.json')['clean'] and h.read(folder/'gpu-after.json')['pids']==[],
              'original owned cleanup incomplete')
    h.require(h.read(folder/'scope/cleanup.json')['unit_removed'],'scope retained')
    for point in verify.POINTS[:2]:
        h.require(h.read(ORIGINAL_OUTPUT/'business'/point/'result.json')['passed'],'startup refusal proof')
    checks=verify.validate_records(verify.startup_records((folder/'server.log').read_text()),2048)
    config=json.loads(h.read(folder/'configuration.json')['raw'])
    Draft202012Validator(h.read(ROOT/'docs/serve-config-v1.schema.json')).validate(config)
    h.require(config['startup_memory']['checks']==checks,'startup snapshot changed')
    rows=h.read(folder/'after-business-resources.json')['metrics']
    accounting_contract(rows)
    h.require(sum('grammar worker reaped=1' in line for line in
                  (folder/'server.log').read_text().splitlines())==3,'worker logs mismatch')
    transport=Draft202012Validator(h.read(ROOT/'docs/serve-transport-v1.schema.json'))
    wires=[]
    for name in ('ordinary-recovery','ordinary-sse','strict-json','project-read','project-answer'):
        request=h.read(folder/(name+'-request.json'))
        result=h.read(folder/(name+'-result.json'))
        path=folder/(name+'-wire.bin')
        values=base.wire_values(path.read_bytes(),request['stream'])
        h.require(values and all('error' not in value for value in values),'wire error')
        for record in values:transport.validate(record)
        h.require(result['finish_reason']==('tool_calls' if name=='project-read' else 'stop'),'turn incomplete')
        wires.append(dict(file=h.identity(path),envelopes=len(values),success=True))
    h.require(h.read(folder/'ordinary-recovery-result.json')['message']['content']=='710003' and
              h.read(folder/'ordinary-sse-result.json')['message']['content']=='710003','ordinary quality')
    h.require(json.loads(h.read(folder/'strict-json-result.json')['message']['content'])=={'ok':True},'strict quality')
    h.require(h.read(folder/'project-completed.json')['tool_calls']==1 and
              h.read(folder/'project-completed.json')['requests']==2,'real tool closure')
    answer=h.read(folder/'project-answer-result.json')['message']['content']
    h.require('Qwen4ExpForConditionalGeneration' in answer and '[docs/MODEL.md:L1-L24]' in answer,'real fact/citation')
    return dict(passed=True,original_failure=h.identity(ORIGINAL_OUTPUT/'business/result.json'),
        original_guard=h.identity(ORIGINAL_OUTPUT/'lease/exit.json'),wires=wires,counts=COUNTS,
        expected_workers=3,reason='strict JSON, required tool, and tool_choice=none final text each compile constraints',
        original_failure_retained=True, runtime_changed=False, new_model_requests=0,new_model_starts=0,
        unexecuted_original_checks=['three final health HTTP probes','post-business standalone resource sample'],
        replacement_evidence='Actual ready metric and drained resources, five successful HTTP turns, clean shutdown. Installed CLI/worker still required; same-ELF HTTP reuse remains explicit.')


def prepare():
    h.require(not subprocess.check_output(['git','status','--porcelain'],cwd=ROOT),'commit correction first')
    plan=ORIGINAL_CHECK()
    audit=audit_original()
    h.require(time.time()<plan['started']+original.LIMITS['seconds']-300,'original time reserve')
    h.require(h.read(BASE/'cpu/result.json')['passed'],'corrected positive/negative CPU contracts')
    PACKAGE.mkdir()
    h.save(PACKAGE/'reused-evidence.json',audit)
    shutil.copytree(BASE/'cpu',PACKAGE/'cpu')
    paths=['tools/serve_startup_memory/correction.py','tools/serve_startup_memory/test_correction.py',
           'docs/SERVE_STARTUP_MEMORY_CORRECTION_2026-11-03.md']
    h.save(PACKAGE/'sources.json',{p:h.sha(ROOT/p) for p in paths})
    h.save(PACKAGE/'plan.json',dict(source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        original_package=h.identity(original.PACKAGE/'sealed-files.json'),
        original_execution=h.identity(ORIGINAL_OUTPUT/'sealed-files.json'),
        started=plan['started'],limits=original.LIMITS,spent=COUNTS,remaining=dict(probes=4,generations=0,starts=0,negatives=0)))
    h.seal(PACKAGE)


def check():
    h.verify_seal(PACKAGE)
    saved=h.read(PACKAGE/'plan.json')
    h.check_identity(saved['original_package']);h.check_identity(saved['original_execution'])
    a.verify_hashes(ROOT,h.read(PACKAGE/'sources.json'))
    h.require(saved['limits']==original.LIMITS and saved['spent']==COUNTS,'correction budget drift')
    plan=ORIGINAL_CHECK()
    h.require(audit_original()==h.read(PACKAGE/'reused-evidence.json'),'reuse evidence changed')
    plan.update(source_commit=saved['source_commit'],started=saved['started'],original_started=saved['started'])
    return plan


def verifier(plan,installed=False):
    if installed:
        from tools.serve_startup_memory.installed_health import run
        run(plan,OUTPUT);return
    audit=audit_original()
    worker=verify.Run(plan,OUTPUT/'business')
    worker.counts=COUNTS.copy();worker.controls=h.read(ORIGINAL_OUTPUT/'business/result.json')['control_requests']
    shutil.copytree(ORIGINAL_OUTPUT/'business/model',worker.out/'model')
    result=dict(status='FAILED')
    try:
        accounting.run(plan,worker.out/'accounting',worker.budget)
        h.require(worker.counts==original.PLANNED,'cumulative frozen attempts')
        h.save(worker.out/'wire-schema-result.json',dict(passed=True,records=audit['wires']))
        result.update(status='PASS_BOUNDED_STARTUP_MEMORY',reused=audit,
            correction_scope='offline worker accounting plus previously unexecuted four probes; no repeated model/native cases',
            original_failed_execution_retained=True,performance_acceptance=False,physical_ram_hard_limit_proved=False)
    except BaseException as error:
        result.update(status='ENV_ABORT' if isinstance(error,(ObservationAbort,a.EnvironmentAbort)) else 'FAILED',error=repr(error))
        if isinstance(error,ObservationAbort):raise a.EnvironmentAbort(str(error)) from error
        raise
    finally:
        result.update(counts=worker.counts,control_requests=worker.controls,
            cumulative_elapsed_seconds=time.time()-plan['started'])
        h.save(worker.out/'result.json',result)


def bind():
    original.OUTPUT=OUTPUT
    a.BASE,a.PACKAGE,a.OUTPUT=BASE,original.PACKAGE,OUTPUT
    a.MARKER=ROOT/'build/serve-startup-memory-correction-recovery.json'
    a.check,a.verifier,a.release=check,verifier,ORIGINAL_RELEASE


def execute():
    h.require_guard(OUTPUT);plan=check()
    h.save(OUTPUT/'begin.json',dict(started=plan['started'],limits=original.LIMITS,original_time_continues=True))
    remaining=plan['started']+original.LIMITS['seconds']-90-time.time()
    h.require(remaining>0,'original deadline exhausted')
    def expired(signum,frame):raise RuntimeError('original deadline; cleanup reserve only')
    signal.signal(signal.SIGALRM,expired);signal.setitimer(signal.ITIMER_REAL,remaining)
    try:bind();a.execute()
    finally:signal.setitimer(signal.ITIMER_REAL,0)


def finalize():
    plan=check();h.require(time.time()-plan['started']<=original.LIMITS['seconds'],'original deadline')
    bind();a.finalize();h.seal(OUTPUT)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('prepare','launch','execute','finalize'))
    args=parser.parse_args()
    if args.action=='launch':
        check();OUTPUT.mkdir(mode=0o700)
        raise SystemExit(subprocess.call(['/usr/bin/python3','-B',str(ROOT/'tools/evalscope/gpu_exclusive.py'),
            '--output',str(OUTPUT/'lease'),'--','/usr/bin/python3','-B',str(Path(__file__)), 'execute'],cwd=ROOT))
    globals()[args.action]()


if __name__=='__main__':main()
