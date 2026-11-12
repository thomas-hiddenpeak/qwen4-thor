"""N06 diagnostic-only release: reuse identical engine and business evidence."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

sys.dont_write_bytecode = True
ROOT = Path.cwd();sys.path.insert(0, str(ROOT))
from tools.serve_recovery import bundle, publication
from tools.serve_service import package as p

BASE = ROOT/'.q4t-work/serve-gateway-diagnostics-20261109'
OUT = BASE/'release-01'
BUILD = ROOT/'.q4t-work/serve-custom-tools-20261108/engine-build'
DEFAULT = ROOT/'build';STATE = DEFAULT/'service-native-state'


def hashes():
    return {str(f.relative_to(STATE)): p.sha(f) for f in STATE.rglob('*')
            if f.is_file() and (f.name == 'ledger.json' or
                f.relative_to(STATE).parts[0].startswith('client-'))}


def main():
    p.require(not OUT.exists(), 'output exists; inspect rather than overwrite')
    p.require(not subprocess.check_output(['git','status','--porcelain']), 'commit source first')
    plan = p.read(BASE/'cpu-01/plan.json');cpu = p.read(BASE/'cpu-01/result.json')
    p.require(cpu['status'] == 'PASS_TARGETED_GATEWAY_DIAGNOSTICS' and
        all(p.sha(ROOT/n) == h for n,h in plan['sources'].items()), 'CPU identity changed')
    old = (DEFAULT/'service').resolve(strict=True)
    digest = p.sha(old/'manifest.json')
    p.require(digest == '22849b0d4a1157affd5e80d398fd1ba45a673fd767f75e3f6b3fb7ec1edcd927',
              'N07 predecessor changed')
    old_manifest = bundle.verify(old,digest)
    old_service = p.read((DEFAULT/'service.release.json').resolve())
    expected_changed = {'tools/serve_recovery/control.py','tools/serve_recovery/gateway.py',
                        'tools/serve_service/supervisor.py','tools/serve_host/native.py'}
    changed = {n for n in bundle.SOURCES if p.sha(ROOT/n) != p.sha(old/'source'/n)}
    p.require(changed == expected_changed, 'unexpected control changes')
    native = p.read(ROOT/'.q4t-work/serve-lifecycle-20261109/release-01/native-build.json')
    native_changes = {n for n,h in native['source_files'].items() if p.sha(ROOT/n) != h}
    p.require(native_changes == {'src/service/guardian_main.cpp'}, 'unexpected native change')
    p.require(p.sha(BUILD/'q4t-guardian') == cpu['guardian_sha256'], 'tested Guardian changed')
    for name,key in [('q4t-ingress','ingress_sha256'),('q4t-cuda-probe','cuda_probe_sha256')]:
        p.require(p.sha(BUILD/name) == native[key], 'unchanged native ELF changed')
    current = publication.status(STATE)
    p.require(current['ready'] and current['revision'] == 6 and
              current['current']['manifest_sha256'] == digest, 'publication predecessor changed')
    before = hashes()
    OUT.mkdir(mode=0o700)
    native['source_files'] = {n:p.sha(ROOT/n) for n in bundle.NATIVE_SOURCES}
    native['guardian_sha256'] = cpu['guardian_sha256']
    native['provenance'] = 'Guardian rebuilt/tested only; identical ingress/probe reused from accepted N07'
    p.save(OUT/'native-build.json',native)
    source_commit = subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
    p.save(OUT/'review.json',dict(source_commit=source_commit,
        changed_control={n:dict(previous=p.sha(old/'source'/n),selected=p.sha(ROOT/n)) for n in sorted(changed)},
        changed_native=sorted(native_changes),engine_binary_unchanged=True,
        engine_release_unchanged=True,protocol_and_request_execution_unchanged=True,
        observation_only=True,existing_rejection_conditions_preserved=True,
        real_business_reused='docs/evidence/serve-lifecycle-business-20261109/decision.json',
        targeted_cpu=str(BASE/'cpu-01/result.json'),previous_publication=current,
        original_business_hashes=before,
        install_checks=['sealed package check','selected old package check','sealed diagnostic projection',
            'publish revision 6','default check','default status','default observe','default metrics','publication status'],
        model_starts=0,chat_requests=0,gpu_queries=0))
    receipt = bundle.prepare(old/'engine/q4t',old/'engine/q4t.release.json',
        p.read(old/'engine/manifest.json')['model_dir'],BUILD/'q4t-guardian',BUILD/'q4t-cuda-probe',
        None,old/'gateway-policy.json',old/'engine/client-policy.json',OUT/'bundle',
        native_receipt=OUT/'native-build.json',native_ingress=BUILD/'q4t-ingress',
        capacity_config=dict(version=1))
    p.save(OUT/'receipt.json',receipt)
    package = Path(receipt['package']);selected = receipt['manifest_sha256'];cli = package/'engine/q4t'
    base = p.read(package/'engine/manifest.json');old_base = p.read(old/'engine/manifest.json')
    p.require(base['settings'] == old_base['settings'] and
        all(p.sha(package/n) == p.sha(old/n) for n in
            ('engine/q4t','engine/q4t.release.json','gateway-policy.json','engine/client-policy.json')),
        'business configuration/engine drift')
    rows = []

    def run(label,args,json_result=True):
        argv = [str(x) for x in args];start = time.monotonic()
        value = subprocess.run(argv,stdin=subprocess.DEVNULL,capture_output=True,timeout=45)
        (OUT/(label+'.stdout')).write_bytes(value.stdout);(OUT/(label+'.stderr')).write_bytes(value.stderr)
        row = dict(label=label,argv=argv,returncode=value.returncode,seconds=time.monotonic()-start)
        rows.append(row);p.save(OUT/(label+'.json'),row)
        p.require(value.returncode == 0,'command failed: '+label)
        p.require(hashes() == before,'business state changed: '+label)
        return json.loads(value.stdout) if json_result else value.stdout.decode()

    state_args = ['--state',STATE]
    try:
        p.require(run('sealed-check',[cli,'service','check'])['status'] == 'PASS','sealed package')
        p.require(run('selected-check',[cli,'service','check',*state_args,'--package',old,
                    '--manifest-sha256',digest])['desired'] == 'STOPPED','predecessor state')
        # Import the actual sealed modules in an isolated interpreter, without
        # replacing their production functions or accessing a GPU/model.
        script = '''import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from tools.serve_host import native
from tools.serve_recovery import control
assert Path(native.__file__).is_relative_to(Path(sys.argv[1]))
v=json.loads(Path(sys.argv[2]).read_text())
v.update(state="WAIT_DEPENDENCY",business_ready=False,gateway_bound_model=0,
 gateway_binding_reason="gateway_capacity_mismatch",
 gateway_binding_model_generation=v["model_generation"],gateway_binding_age_ms=5)
r=native.empty();r.update(native.project_status(v))
assert r["gateway_binding"]["reason"]=="gateway_capacity_mismatch"
assert 'reason="gateway_capacity_mismatch"} 1' in native.metrics(r)
print(json.dumps(dict(status="PASS_SEALED_DIAGNOSTIC_PROJECTION",reasons=len(control.GATEWAY_REASONS))))
'''
        (OUT/'projection.py').write_text(script)
        run('sealed-projection',[sys.executable,'-I','-B',OUT/'projection.py',package/'source',
            ROOT/'docs/evidence/serve-progress-business-20261109/business-02/execution/ready-2.json'])
        decision = dict(status='PASS_N06_TYPED_GATEWAY_DIAGNOSTIC_PREREQUISITES',
            acceptance='ACCEPT_TARGETED_GATEWAY_DIAGNOSTICS',manifest_sha256=selected,
            engine_binary_sha256=p.sha(package/'engine/q4t'),engine_release_sha256=p.sha(package/'engine/q4t.release.json'),
            guardian_sha256=cpu['guardian_sha256'],source_commit=source_commit,
            source_review_sha256=p.sha(OUT/'review.json'),targeted_cpu=cpu,
            packaged_checks=rows.copy(),real_business_reused=True,full_performance=False,
            full_model_quality=False,long_stability_accepted=False,remaining_master_tasks_open=True)
        p.save(OUT/'decision.json',decision)
        metadata = dict(old_service,acceptance=decision['acceptance'],
            acceptance_record=str(OUT/'decision.json'),acceptance_sha256=p.sha(OUT/'decision.json'),
            previous_acceptance_record=old_service['acceptance_record'],package=str(package),
            manifest_sha256=selected,guardian_sha256=cpu['guardian_sha256'],
            gateway_diagnostics_targeted_accepted=True,control_review=str(OUT/'review.json'),
            scope=old_service['scope']+' Typed gateway diagnostics; no additional model recovery authority.',
            limitations=[n for n in old_service['limitations'] if not n.startswith('N06 ')],
            rollback_receipt=str(OUT/'rollback.json'))
        rollback = dict(previous=current['current'],revision_before=6,
            required_command='Use current q4t service publish-rollback with retained generation, current revision and a new request ID; do not restore business journals.')
        p.save(OUT/'rollback.json',rollback);metadata['rollback_sha256'] = p.sha(OUT/'rollback.json')
        p.save(OUT/'service.release.json',metadata)
        request = str(uuid.uuid4());p.save(OUT/'publish-request.json',dict(request_id=request,
            previous_revision=6,expected_manifest_sha256=digest,target_manifest_sha256=selected))
        published = run('publish',[cli,'service','publish',*state_args,'--default-root',DEFAULT,
            '--package',package,'--manifest-sha256',selected,'--service-release',OUT/'service.release.json',
            '--expected-manifest-sha256',digest,'--expected-revision',6,'--request-id',request])
        p.require(published['durable'] and published['revision'] == 8,'publication result')
        for name in ('check','status','observe'):
            value = run('default-'+name,[DEFAULT/'q4t','service',name,*state_args])
            if name == 'check':p.require(value['status'] == 'PASS','default package check')
            else:p.require(value['state'] == 'STOPPED','unexpected live state')
        metrics = run('default-metrics',[DEFAULT/'q4t','service','metrics',*state_args],False)
        p.require('q4t_service_state{state="STOPPED"} 1' in metrics,'stopped metric absent')
        final = run('default-publication',[DEFAULT/'q4t','service','publish-status',*state_args])
        p.require(final['ready'] and final['revision'] == 8 and
            final['current']['manifest_sha256'] == selected,'final publication differs')
        p.save(OUT/'result.json',dict(status='PASS_N06_TYPED_GATEWAY_DIAGNOSTICS_DELIVERED',
            current=final['current'],previous=current['current'],publication_revision=8,
            commands=rows,state='STOPPED',business_state_preserved=hashes()==before,
            model_starts=0,chat_requests=0,gpu_queries=0,default_changed=True,
            core_binary_and_metadata_unchanged=True,actual_business_reused=True,
            rollback_transaction_implementation_unchanged=True,main_merged=False,
            remaining_master_tasks_open=True))
    except BaseException as error:
        p.save(OUT/'result.json',dict(status='FAILED',commands=rows,
            category=type(error).__name__,detail=str(error),business_state_preserved=hashes()==before,
            recovery='Inspect actual publication then use same request resume if needed; never restore business state.'))
        raise
    print(json.dumps(dict(status='PASS_N06_TYPED_GATEWAY_DIAGNOSTICS_DELIVERED',
        commands=len(rows),package=selected,guardian=cpu['guardian_sha256'],revision=8)))


if __name__ == '__main__':
    os.umask(0o077)
    main()
