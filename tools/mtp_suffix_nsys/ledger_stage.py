"""One standalone CUPTI fixture, then one conditional real evalscope request."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import stat
import struct
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SPEC = importlib.util.spec_from_file_location('ledger_monitor_reuse', HERE/'run_stage.py')
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)
old, legacy = runner.old, runner.legacy
BASE_SERVER_AUDIT = old.admission._BASE_AUDIT
LIMIT = 64 * 1024 * 1024
CANDIDATE_SHA = 'f0ff0fe8e11791d561bc49f4dc42762415d4ff000836bbd8c38548723906c914'
FIXTURE_SHA = '16b3303c505ea9fabe82e3289f6f647dbfd533b13da4b049d4a7aa035fec63b3'
DATA_SHA = 'bfc82c68950890905c999c22de45cea615061f66052bfb2aac281fc4907644ac'
PROMPT_SHA = '87123c57eb2a547045c6030fc4530940b92acb393f919c0b7a39ffcc2c0e8511'
OUTPUT_SHA = '66bb6d8c428668a991608e634c39eabebd85918b0cdd400c7b22919214ad436e'


def save(path, value):
    with Path(path).open('x') as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False)
        stream.write('\n')


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def analyze_module():
    spec = importlib.util.spec_from_file_location('ledger_analysis', HERE/'ledger_analyze.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate(plan):
    if (plan['http_requests'], plan['http_loads'], plan['stage_timeout_seconds'],
            plan['direct_contract_timeout_seconds']) != (1, 1, 900, 120):
        raise ValueError('fixed single-model stage budget changed')
    if any(plan[k] != 0 for k in ('retries', 'warmups', 'nsys_processes')):
        raise ValueError('retries, warmups and Nsight are outside this stage')
    if (plan['resource_sample_max_age_seconds'] != 5 or
            plan['expected_work'] != dict(t4=74, t1=3) or len(plan['steps']) != 2):
        raise ValueError('frozen monitor/oracle/step scope changed')
    if plan.get('monitor_fix', {}).get('exit_pending') != dict(
            ttl_seconds=5, capacity=16, poll_timeout_ms=0):
        raise ValueError('exit-pending monitor contract is required')
    for field, expected in (('candidate', CANDIDATE_SHA), ('fixture_binary', FIXTURE_SHA)):
        if plan[field+'_sha256'] != expected or plan['files'].get(plan[field]) != expected:
            raise ValueError('unchanged executable identity required: '+field)
    if plan['files'].get(plan['ledger_library']) != plan['ledger_library_sha256']:
        raise ValueError('preload identity is not frozen')
    for field in ('candidate','fixture_binary','ledger_library','model','artifact','output'):
        if not Path(plan[field]).is_absolute():
            raise ValueError('absolute path required: '+field)
    fixture, model = plan['steps']
    if tuple(fixture.get(k) for k in ('name','kind','measurement_role','http_loads',
            'http_requests','timeout','execution_diagnostic','cases')) != (
            'fixture-ledger','contract','ledger_fixture',0,0,120,False,[]):
        raise ValueError('fixed fixture must precede the model')
    if tuple(model.get(k) for k in ('name','kind','measurement_role','head','suffix',
            'execution_diagnostic','profile_input_tokens','http_loads','http_requests',
            'timeout')) != ('model8-off','timing','cupti_parameters','subset',False,
                           True,8170,1,1,600):
        raise ValueError('fixed model mode or budget changed')
    if len(model['cases']) != 1:
        raise ValueError('one fixed model request required')
    case = model['cases'][0]
    if tuple(case.get(k) for k in ('id','input_tokens','input_min','input_max',
            'output_tokens','repeats','role','prompt_sha256','expected_output_sha256')) != (
            'context-8192',8170,8144,8192,256,1,'development_diagnostic',PROMPT_SHA,OUTPUT_SHA):
        raise ValueError('request/prompt/output contract changed')
    if plan['files'].get(case['path']) != DATA_SHA:
        raise ValueError('request dataset is not the frozen fixture')
    for step in plan['steps']:
        command = step['command']
        if (step.get('env') or len(command) != 7 or
                command[1:3] != ['-B', str(Path(__file__).resolve())]
                or command[3] != '--plan' or not Path(command[4]).is_absolute()
                or command[5:] != ['--custom-step', step['name']]):
            raise ValueError('step command must use only this stage entry')


def clean_env():
    return {k:v for k,v in runner.clean_env().items()
            if not k.startswith(('CUPTI_', 'Q4T_')) and k != 'LD_PRELOAD'}


def target_environment(plan, output, model):
    env = clean_env()
    env.update(LD_PRELOAD=plan['ledger_library'],
               Q4T_MEMSET_LEDGER_PATH=str(output/'ledger.jsonl'))
    if model:
        env.update(Q4T_EXEC_DIAGNOSTIC='1', Q4T_EXEC_NVTX='1',
                   Q4T_EXEC_PROFILE_INPUT='8170')
    return env


def text(path):
    if not path.exists():
        return ''
    if path.stat().st_size > LIMIT:
        raise RuntimeError('bounded target log exceeded 64 MiB')
    return path.read_text()


def tagged(path, tag):
    prefix = '['+tag+'] '
    return [json.loads(line[len(prefix):]) for line in text(path).splitlines(keepends=True)
            if line.startswith(prefix) and line.endswith('\n')]


def check_target_log(path):
    raw = text(path)
    if '[memset-ledger-error]' in raw or '[mechanism-error]' in raw:
        raise RuntimeError('target observer/probe reported an error')
    return raw


def ledger_boundary(path, pid):
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= LIMIT:
        raise RuntimeError('ledger must be a bounded nonempty regular file')
    fd = os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
    with os.fdopen(fd,'rb') as stream:
        pinned = os.fstat(stream.fileno())
        if (pinned.st_dev,pinned.st_ino) != (before.st_dev,before.st_ino):
            raise RuntimeError('ledger path identity changed before read')
        raw = stream.read(LIMIT+1)
    after = path.lstat()
    if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns) != (
            after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns):
        raise RuntimeError('ledger changed during closed-boundary read')
    if not raw.endswith(b'\n'):
        raise RuntimeError('ledger lacks a complete last record')
    last = json.loads(raw.splitlines()[-1])
    if last.get('event') != 'summary' or last.get('completed') is not True:
        raise RuntimeError('ledger lacks successful terminal summary')
    return dict(path=str(path),pid=pid,device=after.st_dev,inode=after.st_ino,
                bytes=after.st_size,mtime_ns=after.st_mtime_ns,
                sha256=hashlib.sha256(raw).hexdigest())


def held_fixture(log, pid):
    ready = tagged(log, 'mechanism-ready')
    expected = dict(pid=pid,mode='nsys',passed=True,gemm_calls=2,controls=2,
                    M=4,N=48,K=2560,workspace_bytes=33554432,
                    profiler_start_error=0,profiler_stop_error=0,final_sync_error=0)
    if len(ready) != 1 or any(ready[0].get(k) != v for k,v in expected.items()):
        raise RuntimeError('fixed held-fixture ready contract changed')
    rows = [r for r in tagged(log,'mechanism-event') if r.get('event') == 'check']
    phases = ('control_zero','control_nonzero','gemm_first','gemm_cached')
    if len(rows) != 4 or {r.get('phase') for r in rows} != set(phases):
        raise RuntimeError('fixture lacks four unique output checks')
    for row in rows:
        phase = row['phase']
        expected_words = ([165 if phase == 'control_zero' else 90]*512
            if phase.startswith('control_') else
            [struct.unpack('<I',struct.pack('<f',(n%7-3)*(m%5+1)*(m+1)))[0]>>16
             for m in range(4) for n in range(48)])
        if (row.get('passed') is not True or row.get('mismatches') != 0 or
                row.get('actual_words') != expected_words or
                row.get('expected_words') != expected_words):
            raise RuntimeError('fixture exact output differs: '+phase)
    return ready[0]


def loaded_libraries(plan, pid):
    selected = {}
    library = Path(plan['ledger_library']).resolve()
    for line in Path(f'/proc/{pid}/maps').read_text().splitlines():
        fields = line.split(maxsplit=5)
        if len(fields) != 6 or not fields[5].startswith('/'):
            continue
        path = Path(fields[5]).resolve()
        if path != library and not path.name.startswith(('libcuda','libcublas','libcupti')):
            continue
        digest = sha(path)
        if plan['files'].get(str(path)) != digest:
            raise RuntimeError('actual mapped library not frozen: '+str(path))
        if 'nsight-systems' in str(path):
            raise RuntimeError('Nsight injection is outside the standalone ledger stage')
        selected[str(path)] = digest
    if (str(library) not in selected or not all(any(Path(p).name.startswith(prefix)
            for p in selected) for prefix in ('libcuda.so','libcudart','libcublasLt','libcupti'))):
        raise RuntimeError('required actual CUDA/ledger mappings missing')
    return selected


class OwnedTarget:
    """Direct Popen child, pinned identity/pidfd, with one explicit cleanup."""
    def __init__(self, output, executable):
        self.output, self.executable = output, Path(executable).resolve()
        self.child = self.fd = self.identity = None
        self.closed = False

    def start(self, command, env, stream):
        self.child = subprocess.Popen(command, cwd=ROOT, env=env, stdout=stream,
                                      stderr=subprocess.STDOUT)
        fd = os.pidfd_open(self.child.pid)
        try:
            identity = old.proc_identity(self.child.pid)
            if (identity['ppid'] != os.getpid() or runner.pidfd_exited(fd,0) or
                    Path(f'/proc/{self.child.pid}/exe').resolve() != self.executable):
                raise RuntimeError('direct target ownership/executable binding failed')
            self.identity, self.fd = identity, fd
        except BaseException:
            os.close(fd)
            raise
        save(self.output/'target-identity.json', dict(identity,executable=str(self.executable)))
        (self.output/'server.pid').write_text(str(self.child.pid)+'\n')

    def require_live(self, deadline):
        if time.monotonic() >= deadline:
            raise TimeoutError('fixed ledger step deadline exceeded')
        if self.child.poll() is not None or runner.pidfd_exited(self.fd,0):
            raise RuntimeError('actual target exited before completed request/receipt')
        check_target_log(self.output/'server.log')

    def close(self, normal_shutdown, deadline):
        if self.closed:
            raise RuntimeError('target cleanup called twice')
        self.closed = True
        errors, forced = [], False
        def attempt(label, operation):
            try:
                return operation()
            except BaseException as error:
                errors.append(label+': '+repr(error))
                return None
        def send(sig):
            try:
                if self.fd is not None:
                    signal.pidfd_send_signal(self.fd,sig)
                elif sig == signal.SIGTERM:
                    self.child.terminate()
                else:
                    self.child.kill()
            except ProcessLookupError:
                pass  # Actual wait status and the pinned pidfd still decide exit.
        try:
            if self.child is not None:
                live = attempt('owned child poll', self.child.poll) is None
                if live:
                    forced = not normal_shutdown
                    if self.fd is not None:
                        attempt('owned pidfd TERM', lambda: send(signal.SIGTERM))
                    else:
                        forced = True
                        attempt('exact Popen child TERM', lambda: send(signal.SIGTERM))
                    wait = max(.01,min(30 if normal_shutdown else 5,deadline-time.monotonic()))
                    try:
                        self.child.wait(timeout=wait)
                    except subprocess.TimeoutExpired:
                        forced = True
                        if self.fd is not None:
                            attempt('owned pidfd KILL',lambda:send(signal.SIGKILL))
                        else:
                            attempt('exact Popen child KILL',lambda:send(signal.SIGKILL))
                        attempt('owned child final wait',lambda:self.child.wait(timeout=5))
                    except BaseException as error:
                        errors.append('owned child wait: '+repr(error))
                else:
                    attempt('owned child reap',lambda:self.child.wait(timeout=0))
                if self.fd is not None and attempt('pidfd final exit',lambda:runner.pidfd_exited(self.fd,0)) is not True:
                    errors.append('actual target exit not proven by pinned pidfd')
                if self.child.returncode != 0:
                    errors.append('actual target returncode is not zero: '+repr(self.child.returncode))
        finally:
            if self.fd is not None:
                attempt('pidfd close',lambda:os.close(self.fd))
                self.fd = None
            save(self.output/'cleanup.json',dict(status='ENV_ABORT' if forced or errors else 'NORMAL',
                forced=forced,errors=errors,identity=self.identity,
                actual_returncode=self.child.returncode if self.child else None,
                scope='Only exact direct Popen target and its pinned pidfd; no Nsight launcher.'))
        if forced or errors:
            raise legacy.EnvironmentAbort('owned ledger target cleanup failed/forced: '+repr(errors))


def require_valid(result, label):
    if (result.get('status') != 'VALID' or result.get('completed') is not True or
            result.get('errors')):
        raise RuntimeError(label+' is not valid: '+repr(result.get('errors')))


def cleanup_problems(cleanup):
    problems = list(cleanup['errors'])
    if cleanup.get('forced'):
        problems.append('actual target required forced termination')
    if cleanup.get('status') != 'NORMAL' and not problems:
        problems.append('actual target cleanup did not close normally')
    return problems


def fixture_step(plan, step, plan_path):
    output = Path(plan['output'])/step['name']
    output.mkdir()
    target = OwnedTarget(output,plan['fixture_binary'])
    deadline = time.monotonic()+step['timeout']
    failure, passed = None, False
    command = [plan['fixture_binary'],'--mode','nsys','--release',str(output/'release')]
    env = target_environment(plan,output,False)
    save(output/'server-command.json',dict(argv=command,
        environment={k:v for k,v in env.items() if k.startswith('Q4T_') or k=='LD_PRELOAD'},
        mode_name_scope='Existing probe mode selects no internal CUPTI subscriber; no Nsight process.'))
    save(output/'step.json',step)
    try:
        with (output/'server.log').open('x') as stream:
            target.start(command,env,stream)
            while not tagged(output/'server.log','mechanism-ready'):
                target.require_live(deadline)
                time.sleep(.05)
            target.require_live(deadline)
            ready = held_fixture(output/'server.log',target.child.pid)
            save(output/'ready-boundary.json',ready)
            save(output/'loaded-libraries.json',loaded_libraries(plan,target.child.pid))
            boundary = ledger_boundary(output/'ledger.jsonl',target.child.pid)
            save(output/'ledger-boundary.json',boundary)
            (output/'release').touch(exist_ok=False)
            while target.child.poll() is None:
                if time.monotonic() >= deadline:
                    raise TimeoutError('fixture release/normal exit exceeded step budget')
                time.sleep(.05)
            target.close(False,deadline)
        check_target_log(output/'server.log')
        if boundary != ledger_boundary(output/'ledger.jsonl',target.child.pid):
            raise RuntimeError('fixture ledger changed after application stop returned')
        analyzer = analyze_module()
        result = analyzer.analyze(output/'ledger.jsonl',expected_pid=target.child.pid)
        save(output/'ledger-analysis.json',result)
        require_valid(result,'fixture ledger')
        validation = analyzer.validate_fixture(result,output/'server.log',expected_pid=target.child.pid)
        save(output/'fixture-validation.json',validation)
        require_valid(validation,'fixture outputs and mechanism')
        passed = True
    except BaseException as error:
        failure = repr(error)
        raise
    finally:
        try:
            if not target.closed:
                target.close(False,deadline)
        finally:
            cleanup = json.loads((output/'cleanup.json').read_text())
            save(output/'exit.json',dict(server=target.child.returncode if target.child else None,
                http_output_checks_passed=passed,failure=failure,cleanup_errors=cleanup_problems(cleanup),
                diagnostic_only=True,performance_acceptance=False))
    inputs = {str(p):sha(p) for p in output.iterdir() if p.is_file()}
    save(output/'gate.json',dict(passed=True,plan_sha256=sha(plan_path),inputs=inputs,
        actual_pid=target.child.pid,normal_target_exit=True,performance_acceptance=False,
        scope='One standalone CUPTI fixed fixture; only this gate permits the single model load.'))


def fixture_gate(plan, plan_path):
    output = Path(plan['output'])/'fixture-ledger'
    gate = json.loads((output/'gate.json').read_text())
    if not gate.get('passed') or gate.get('plan_sha256') != sha(plan_path) or not gate.get('normal_target_exit'):
        raise RuntimeError('model lacks its same-plan completed fixture gate')
    for name,expected in gate['inputs'].items():
        if Path(name).parent != output or sha(name) != expected:
            raise RuntimeError('fixture gate input changed')
    for name in ('ledger-analysis.json','fixture-validation.json'):
        require_valid(json.loads((output/name).read_text()),'frozen fixture gate '+name)
    cleanup = json.loads((output/'cleanup.json').read_text())
    if cleanup['status'] != 'NORMAL' or cleanup['forced'] or cleanup['errors']:
        raise RuntimeError('fixture gate has non-normal cleanup')
    return gate


def host_audit(output, step, pid):
    # The old parser names its exact two cudaProfiler API markers "nsys_profile".
    # This private view reuses only that host contract; public role stays CUPTI.
    result = old.audit_execution(output,dict(step,measurement_role='nsys_profile'),pid)
    save(output/'host-capture-contract.json',dict(
        actual_measurement_role=step['measurement_role'],nsys_processes=0,
        parser_view='nsys_profile only selects existing begin/end marker checks',
        complete=result['complete'],errors=result['errors'],
        execution_audit_sha256=sha(output/'execution-audit.json')))
    return result


def audit_server(output, expected_requests):
    """Retain base audits, and scope request-end parsing to actual request logs."""
    BASE_SERVER_AUDIT(output,expected_requests)
    raw = check_target_log(output/'server.log')
    for marker in ('MTP trunk alloc failed','plain decode for this request',
                   'MTP load failed','MTP init failed','MTP allocation failed'):
        if marker in raw:
            raise RuntimeError('MTP fallback invalidates ledger model: '+marker)
    if '[suffix-classes]' in raw:
        raise RuntimeError('suffix-off arm initialized classification table')
    admitted, ended = {}, {}
    for number,line in enumerate(raw.splitlines(),1):
        if not line.startswith('[q4t][request] '):
            continue
        row = dict(re.findall(r'(\w+)=(\S+)',line))
        if row.get('event') not in ('admitted','end'):
            continue
        table = admitted if row['event']=='admitted' else ended
        identity = row.get('id')
        if not identity or identity in table:
            raise RuntimeError('missing/duplicate request lifecycle identity')
        table[identity] = dict(row,line=number)
    if (len(admitted) != expected_requests or admitted.keys() != ended.keys() or
            any(row.get('released') != '1' or row.get('outcome') != 'success' or
                row.get('reason') != 'completed' or row['line'] <= admitted[key]['line']
                for key,row in ended.items())):
        raise RuntimeError('actual request lifecycle incomplete')


def model_step(plan, step, plan_path):
    fixture_gate(plan,plan_path)
    output = Path(plan['output'])/step['name']; output.mkdir()
    target = OwnedTarget(output,plan['candidate'])
    deadline = time.monotonic()+step['timeout']
    env, client_env = target_environment(plan,output,True), clean_env()
    command = old.server_command(plan,step)
    failure, passed, execution = None, False, None
    save(output/'server-command.json',dict(argv=command,
        environment={k:v for k,v in env.items() if k.startswith('Q4T_') or k=='LD_PRELOAD'},
        measurement_role='cupti_parameters',diagnostic_only=True,nsys_processes=0,
        performance_acceptance_sample=False,performance_acceptance=False))
    save(output/'step.json',step)
    try:
        with (output/'server.log').open('x') as stream:
            old.http.event(output,'server_before_start')
            target.start(command,env,stream)
            old.http.event(output,'server_started',pid=target.child.pid,**{
                k:v for k,v in target.identity.items() if k=='start_ticks'})
            while 'serving on port' not in text(output/'server.log'):
                target.require_live(deadline); time.sleep(.1)
            target.require_live(deadline)
            capacity = old.http.capacity_evidence(text(output/'server.log'),208896)
            save(output/'capacity.json',capacity)
            if not capacity['matches_requested']:
                raise RuntimeError('effective service capacity differs')
            old.http.event(output,'server_ready',pid=target.child.pid,capacity=capacity)
            results = old.admission.run_cases(plan,step,output,target.child,client_env,capacity)
            save(output/'output-reference-check.json',dict(passed=(len(results)==1 and
                results[0]['outputs']==[OUTPUT_SHA]),expected_output_sha256=OUTPUT_SHA,
                arbitrary_input_equivalence=False))
            if len(results)!=1 or results[0]['outputs'] != [OUTPUT_SHA]:
                raise RuntimeError('fixed model output differs from frozen reference')
            while True:
                target.require_live(deadline)
                raw = text(output/'server.log')
                if '[exec-capture] event=end ' in raw and re.search(
                        r'^\[q4t\]\[request\].* event=end ',raw,re.M):
                    break
                time.sleep(.05)
            capture = runner.capture_end_receipt(raw,target.child.pid,8170)
            save(output/'capture-boundary.json',capture)
            save(output/'loaded-libraries.json',loaded_libraries(plan,target.child.pid))
            boundary = ledger_boundary(output/'ledger.jsonl',target.child.pid)
            save(output/'ledger-boundary.json',boundary)
            old.http.event(output,'server_before_shutdown',pid=target.child.pid)
            target.close(True,deadline)
            old.http.event(output,'server_after_shutdown',pid=target.child.pid,
                           server_returncode=target.child.returncode)
        raw = check_target_log(output/'server.log')
        if '[layer-timing]' in raw or '[mtp-timing]' in raw:
            raise RuntimeError('legacy intrusive timing is outside this stage')
        if boundary != ledger_boundary(output/'ledger.jsonl',target.child.pid):
            raise RuntimeError('model ledger changed after application stop returned')
        execution = host_audit(output,step,target.child.pid)
        if not execution['complete']:
            raise RuntimeError('host execution/capture evidence incomplete')
        analyzer = analyze_module()
        result = analyzer.analyze(output/'ledger.jsonl',expected_pid=target.child.pid)
        save(output/'ledger-analysis.json',result); require_valid(result,'model ledger')
        validation = analyzer.validate_model(result,output/'server.log',
            expected_pid=target.child.pid,expected_input_tokens=8170)
        save(output/'model-validation.json',validation); require_valid(validation,'model target work')
        passed = True
    except BaseException as error:
        failure = repr(error)
        raise
    finally:
        try:
            if not target.closed:
                target.close(True,deadline)
        finally:
            if execution is None and (output/'server.log').exists():
                execution = host_audit(output,step,target.child.pid if target.child else None)
            cleanup = json.loads((output/'cleanup.json').read_text())
            save(output/'exit.json',dict(server=target.child.returncode if target.child else None,
                http_output_checks_passed=passed,failure=failure,cleanup_errors=cleanup_problems(cleanup),
                execution_records_complete=bool(execution and execution['complete']),
                diagnostic_only=True,performance_acceptance_sample=False,
                performance_acceptance=False,nsys_processes=0))


def custom_step(plan, name, plan_path):
    legacy.require_guard()
    step = next(s for s in plan['steps'] if s['name']==name)
    if name == 'fixture-ledger':
        return fixture_step(plan,step,plan_path)
    if name == 'model8-off':
        return model_step(plan,step,plan_path)
    raise ValueError('step outside fixed ledger stage')


def install():
    legacy.clean_env = clean_env
    old.telemetry.sample = runner.sample_with_failure_context
    legacy.stop_group = runner.stop_group_with_final_flush
    legacy.require_monitor = runner.require_monitor_with_pending
    old.validate = validate
    old.custom_step = custom_step
    old.admission.audit_server = audit_server
    old.__file__ = str(Path(__file__).resolve())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan',type=Path,required=True)
    parser.add_argument('--custom-step',choices=('fixture-ledger','model8-off'))
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text()); validate(plan)
    for step in plan['steps']:
        if Path(step['command'][4]).resolve() != args.plan.resolve():
            raise ValueError('step command points at a different frozen plan')
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt('owned ledger stage interrupted')
    signal.signal(signal.SIGTERM,interrupted)
    install()
    old.main()


if __name__ == '__main__':
    main()
