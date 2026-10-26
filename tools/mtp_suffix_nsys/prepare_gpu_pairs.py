"""Freeze the four original GPU comparison arms with limited recorded-work scope."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import time

ROOT=Path(__file__).resolve().parents[2]
BASE=ROOT/'.q4t-work/suffix-gpu-pairs-20261026'
LEDGER=ROOT/'.q4t-work/suffix-model-ledger-20261026'
MECHANISM=ROOT/'.q4t-work/suffix-callback-pairing-20261026'
ORIGINAL=ROOT/'.q4t-work/suffix-execution-rerun-20261024/diagnostic-v1/frozen-plan.json'
REPORT=ROOT/'.q4t-work/suffix-report-binding-20261026/diagnostic-v1/frozen-plan.json'

def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()

def read(path): return json.loads(Path(path).read_text())

def require(ok,message):
    if not ok: raise RuntimeError(message)

def inherit_live_dependencies(parent, bind):
    # Thor's actual driver is outside /usr and /lib. Preserve its existing
    # frozen digest; never discover or accept a replacement after capture.
    prefixes=(str(ROOT/'tools/evalscope/.venv')+'/',parent['artifact']+'/',
              parent['model']+'/', '/usr/', '/lib/',
              '/opt/nvidia/l4t-gpu-libs/')
    selected={path:digest for path,digest in parent['files'].items()
              if path.startswith(prefixes)}
    require(any(Path(path).name.startswith('libcuda.so.') for path in selected),
            'parent freeze has no actual CUDA driver; stop before GPU launch')
    for path,digest in selected.items():
        bind(path,digest)

def main():
    require(not (BASE/'frozen-plan.json').exists(),'one preparation only')
    scope=read(BASE/'scope.json')
    require(time.time()+4380<scope['created_unix']+scope['work_wall_seconds'],
            'insufficient frozen wall for GPU bound and terminal audit')
    files={}
    def bind(path,expected=None):
        path=Path(path).absolute();digest=sha(path)
        require(expected is None or expected==digest,'identity changed: '+str(path))
        require(str(path) not in files or files[str(path)]==digest,'conflicting frozen identity')
        files[str(path)]=digest
        return digest
    for path,digest in scope['frozen_inputs'].items(): bind(path,digest)
    cpu=read(BASE/'cpu-proof.json')
    require(set(cpu['test_modules'])=={'test_recorded_gpu.py','test_gpu_pair_stage.py',
            'test_pair_work_contract.py'} and 10<=cpu['tests']<=40 and
            cpu['tests']==sum(cpu['test_modules'].values()) and cpu['suite_runs']<=2 and
            cpu['elapsed_seconds']<=120 and not any(cpu[k] for k in ('failed','errors','skipped')),
            'affected CPU contracts not passed within fixed budget')
    for path,digest in cpu['files'].items():bind(path,digest)
    parent=read(LEDGER/'frozen-plan.json');mechanism=read(MECHANISM/'frozen-plan.json')
    original=read(ORIGINAL);report=read(REPORT)
    bind(REPORT,'c71846e9741c69ae86569de58e4c42dcef0437e7093bb5762a1f67cc00297892')
    last=read(LEDGER/'terminal-review.json');fixed=read(MECHANISM/'terminal-review.json')
    require(last['evidence_integrity']=='PASS' and last['evidence_validity']=='VALID_ACTUAL_8K_MEMSET_LOCAL_ONLY'
            and last['measurement_complete'] and not last['errors'] and not last['performance_acceptance'],
            'actual8K local mechanism evidence changed')
    require(fixed['evidence_integrity']=='PASS' and fixed['evidence_validity']=='VALID_LOCAL_MECHANISM_ONLY'
            and fixed['measurement_complete'] and not fixed['errors'] and not fixed['performance_acceptance'],
            'fixed same-package local mechanism evidence changed')
    # Direct live dependencies only; do not recursively import historical snapshots.
    inherit_live_dependencies(parent,bind)
    for path,digest in original['files'].items():
        source=Path(path)
        if source==ROOT/'CMakeLists.txt' or any(source.is_relative_to(ROOT/n) for n in ('src','include','cmake')):
            bind(path,digest)
    for field in ('candidate','reference_binary'):
        bind(parent[field],parent[field+'_sha256'])
    tool=mechanism['tool_dependency'];bind(tool['manifest'],tool['manifest_sha256'])
    inventory=read(tool['manifest']);package=Path(tool['package_root'])
    require(len(inventory)==1765 and set(inventory)=={str(p.relative_to(package)) for p in package.rglob('*')
            if p.is_file() or p.is_symlink()},'pinned Nsight inventory changed')
    for relative,row in inventory.items():
        path=package/relative
        require(path.resolve().is_relative_to(package) and path.is_symlink()==row['symlink'] and
                path.stat().st_size==row['bytes'],'pinned package entry changed')
        bind(path,row['sha256'])
    bind(tool['path'],tool['sha256'])
    # Reuse unchanged CPU policies by actual source identity, not suite totals.
    monitor=report['monitor_fix'];refs={}
    for key in ('cpu_proof','native_proof','kernel_contract','runner_cpu_proof'):
        value=monitor[key];bind(value['path'],value['sha256']);refs[value['path']]=value['sha256']
    kernel=read(monitor['kernel_contract']['path'])
    require(kernel['release']==os.uname().release and kernel['PF_EXITING']==4,'running kernel contract changed')
    bind(kernel['original_path'],kernel['sha256']);bind(kernel['copy_path'],kernel['copy_sha256'])
    strict_cpu=read(monitor['cpu_proof']['path'])
    sync_path=ROOT/'.q4t-work/suffix-memset-link-20261026/cpu-proof.json'
    sync_sha=bind(sync_path,'dfe7aae9e80e9e3f712977042cf57f6127b900e496b299d461a1d9f4734a1cfd')
    refs[str(sync_path)]=sync_sha
    sync_cpu=read(sync_path)
    strict_sources=['tools/mtp_suffix_zero_hit/telemetry.py','tools/mtp_suffix_zero_hit/test_telemetry.py',
        'tools/mtp_suffix_zero_hit/test_exit_pending.py','tools/mtp_suffix_nsys/fixture_audit.py',
        'tools/mtp_suffix_nsys/test_fixture_audit.py','tools/mtp_suffix_nsys/analyze_gpu.py',
        'tools/mtp_suffix_nsys/test_analyze_gpu.py']
    for name in strict_sources: bind(ROOT/name,strict_cpu['files'][str(ROOT/name)])
    for name in ('run_stage.py','test_run_stage.py','synchronous.py','test_synchronous.py',
                 'interactive.py','test_interactive.py','automatic.py','report_ready.py',
                 'nvlog_snapshot.py','test_nvlog_snapshot.py'):
        path=ROOT/'tools/mtp_suffix_nsys'/name
        bind(path,sync_cpu['files'][str(path)])
    bind(ROOT/'tools/mtp_suffix_execution/analyze_trace.py',
         sync_cpu['files'][str(ROOT/'tools/mtp_suffix_execution/analyze_trace.py')])
    native=read(monitor['native_proof']['path'])
    require(native['passed'] and native['cleanup_complete'] and native['all_owned_reaped'] and
            native['completed_children']==native['children_started']==64 and not native['failures'],
            'native monitor contract changed')
    bind(native['rows'],native['rows_sha256'])
    for path,expected in parent['model_file_metadata'].items():
        st=Path(path).stat()
        require(expected==dict(size=st.st_size,mtime_ns=st.st_mtime_ns,device=st.st_dev,inode=st.st_ino),
                'read-only model metadata changed')
    for path in (BASE/'scope.json',BASE/'cpu-proof.json',Path(__file__),ROOT/'AGENTS.md',
                 ROOT/'docs/EVALUATION.md',ROOT/'docs/PERFORMANCE_DECISION_PROTOCOL.md'):
        bind(path)
    spec=importlib.util.spec_from_file_location('pairs_sync_contract',ROOT/'tools/mtp_suffix_nsys/synchronous.py')
    sync=importlib.util.module_from_spec(spec);spec.loader.exec_module(sync)
    planpath=BASE/'frozen-plan.json';steps=[]
    for frozen in scope['order']:
        step=copy.deepcopy(next(s for s in original['steps'] if s['name']==frozen['name']))
        require(step['suffix']==frozen['suffix'] and step['profile_input_tokens']==frozen['capture'] and
                [c['input_tokens'] for c in step['cases']]==frozen['inputs'], 'original matrix contract differs')
        for case in step['cases']:
            # The already sealed delivery copies contain these exact requests.
            path=ROOT/'.q4t-work/suffix-delivery-20261026/diagnostic-v1/fixtures'/Path(case['path']).name
            bind(path,original['files'][case['path']]);case['path']=str(path)
        step.update(timeout=900,command=['/usr/bin/python3','-B',
            str(ROOT/'tools/mtp_suffix_nsys/gpu_pair_stage.py'),'--plan',str(planpath),
            '--custom-step',step['name']])
        steps.append(step)
    inherited=('candidate','candidate_sha256','candidate_source_commit','reference_binary',
        'reference_binary_sha256','reference_runtime_source','model','artifact','subset_manifest_sha256',
        'model_file_metadata','telemetry_sources','cache')
    plan={key:parent[key] for key in inherited}
    qualification=dict(scope['frozen_inputs'])|refs
    plan.update(version=1,classification=scope['classification'],source_commit=scope['source_commit'],
        scope=str(BASE/'scope.json'),scope_sha256=sha(BASE/'scope.json'),output=str(BASE/'run'),
        lease=str(BASE/'lease'),port=8759,http_requests=6,http_loads=4,steps=steps,
        stage_timeout_seconds=4200,direct_contract_timeout_seconds=0,
        fixture_runs=0,retries=0,warmups=0,observation_build_limit=0,runtime_optimization_candidates=0,
        source_log_limit_bytes=268435456,sqlite_limit_bytes=8589934592,analysis_per_capture_seconds=120,
        resource_sample_interval_seconds=1,resource_sample_max_age_seconds=5,monitor_fix=monitor,
        nsys=dict(path=tool['path'],sha256=tool['sha256'],interactive_contract=sync.contract(),
            export_timeout_seconds=120,launcher_exit_timeout_seconds=120),
        tool_dependency=tool,nvlog_config=mechanism['nvlog_config'],qualification_reuse=qualification,
        sync_cpu_reuse=dict(path=str(sync_path),sha256=sync_sha,
            unchanged_modules={'test_run_stage.py':19,'test_synchronous.py':15,'test_nvlog_snapshot.py':8}),
        reuse_scope='Local successful fixed mechanism/actual8K parameters and unchanged CPU policies only; no full-trace promotion, cross-process argument fill or normal performance acceptance.',
        cpu_proof=str(BASE/'cpu-proof.json'),normal_performance_claim=False,model_qualification=False,
        deployment=False,source_snapshot_reuse=parent['source_snapshot_reuse'],files=dict(sorted(files.items())))
    # A preparer must reject its own malformed plan before any GPU stage starts.
    spec=importlib.util.spec_from_file_location('pairs_validate',ROOT/'tools/mtp_suffix_nsys/gpu_pair_stage.py')
    stage=importlib.util.module_from_spec(spec);spec.loader.exec_module(stage);stage.validate(plan)
    with planpath.open('x') as stream:json.dump(plan,stream,indent=2);stream.write('\n')
    print(json.dumps(dict(plan=str(planpath),sha256=sha(planpath),files=len(files),model_metadata=len(plan['model_file_metadata']))))

if __name__=='__main__':main()
