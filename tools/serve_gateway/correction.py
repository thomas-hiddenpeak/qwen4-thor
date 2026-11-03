"""Targeted, separately budgeted cancellation supplement; never replay old tests."""
import argparse
import ast
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.dont_write_bytecode=True
sys.path.insert(0,str(ROOT))
from tools.serve_gateway import acceptance as model, gateway as g
from tools.serve_service import package as pkg, supervisor as sup

BASE=model.BASE
PLAN=BASE/'model-corrected-plan.json'
OUT=BASE/'model-corrected'
CPU=BASE/'correction-cpu'
CONTRACT='docs/SERVE_GATEWAY_CORRECTION_2026-11-04.md'
SOURCES=sorted(set((*model.SOURCES,'tools/serve_gateway/correction.py',
    'tools/serve_gateway/test_correction.py','docs/serve-openapi-v1.json',CONTRACT)))
CHANGED={'tools/serve_gateway/gateway.py','tools/serve_gateway/acceptance.py',
         'tools/serve_gateway/transport_tests.py'}


def configure():
    model.PLAN,model.OUT=PLAN,OUT
    model.LIMITS=dict(starts=1,generations=3,negatives=2,observations=96,seconds=360)
    model.GPU_WINDOW,model.SUPERVISOR_BOUND=300,252
    model.EXPECTED=dict(successful_requests=2,real_tool_executions=0,rejections=2)
    model.configure()


def cpu():
    CPU.mkdir(mode=0o700)
    sources={name:g.sha(ROOT/name) for name in SOURCES}
    for name in SOURCES:
        dest=CPU/'source'/name;dest.parent.mkdir(parents=True,exist_ok=True)
        g.save(dest,(ROOT/name).read_bytes())
    tests=['tools.serve_gateway.test_gateway.GatewayTests.'
           'test_real_nginx_parse_and_private_package_verification',
           'tools.serve_gateway.test_correction.CorrectionTests',
           'tools.serve_gateway.test_correction.ContractPackageTests.'
           'test_contract_identity_refuses_drift']
    stream=io.StringIO()
    result=unittest.TextTestRunner(stream=stream,verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromNames(tests))
    g.save(CPU/'tests.log',stream.getvalue().encode())
    value=dict(passed=result.wasSuccessful(),tests_run=result.testsRun,
        failures=len(result.failures),errors=len(result.errors),sources=sources,
        reused_gateway_cases=8,reused_cancel_parser_checks=4,
        listener_or_model_or_gpu_started=False)
    pkg.save(CPU/'result.json',value)
    print(stream.getvalue(),end='')
    g.require(value['passed'],'corrective CPU failed; preserve originals')
    return value


def reuse():
    prior=pkg.read(BASE/'model-plan.json')
    result=pkg.read(BASE/'model/result.json')
    g.require(result['status']=='FAILED' and result['counts']['generations']==7 and
        result['cleanup_confirmed'],'original failure must remain')
    changed={n for n,d in prior['source_files'].items() if g.sha(ROOT/n)!=d}
    g.require(changed==CHANGED,'unexpected changed dependency')
    # Rendering, authentication, quotas, proxy and secret handling are identical.
    old=subprocess.check_output(['git','show',prior['source_commit']+':tools/serve_gateway/gateway.py'])
    def functions(raw):
        return {n.name:ast.dump(n,include_attributes=False) for n in ast.parse(raw).body
                if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))}
    before,after=functions(old),functions((ROOT/'tools/serve_gateway/gateway.py').read_bytes())
    g.require({n for n in before if before[n]!=after.get(n)}=={'prepare','check'} and
        after.keys()-before.keys()=={'validate_routes'},'unexpected gateway behavior change')
    for name in ('ordinary-alpha','ordinary-beta','strict','project-read','project-answer'):
        body=pkg.read(BASE/'model'/(name+'-request.json'))
        raw=(BASE/'model'/(name+'-wire.bin')).read_bytes()
        parsed=(model.a.read_sse if body['stream'] else model.a.read_json)(io.BytesIO(raw))
        g.require(model.asdict(parsed)==pkg.read(BASE/'model'/(name+'-response.json')) and
            parsed.id==body['request_id'],'original success wire differs')
    return dict(original_plan_sha256=g.sha(BASE/'model-plan.json'),
        original_result_sha256=g.sha(BASE/'model/result.json'),
        independent_stop_sha256=g.sha(BASE/'model-independent-stop.json'),
        reused_successful_requests=5,real_tool_executions=1,
        changed_dependencies=sorted(changed),render_function_unchanged=True,
        prior_failure_retained=True,prior_fixture_cancel_not_backend_proof=True)


def prepare():
    g.require(not PLAN.exists() and not OUT.exists(),'preserve prior corrective phase')
    for name in SOURCES:
        g.require(subprocess.check_output(['git','show','HEAD:'+name])==(ROOT/name).read_bytes(),
                  'commit reviewed dependency first: '+name)
    tested=pkg.read(CPU/'result.json')
    g.require(tested['passed'] and tested['tests_run']==4,'CPU correction missing')
    for name,digest in tested['sources'].items():g.require(g.sha(ROOT/name)==digest,'CPU source changed')
    proof=reuse()
    prior=pkg.read(BASE/'model-plan.json')
    pkg.verify(prior['package']['package'],prior['package']['manifest_sha256'])
    private=BASE/'model-private'
    old=Path(prior['gateway']['package'])
    new=private/'package-corrected'
    g.require(g.sha(private/'policy-private.json')==prior['private_policy_sha256'],'keys changed')
    previous=pkg.read(old/'manifest.json')
    for name,digest in previous['files'].items():g.require(g.sha(old/name)==digest,'old private package changed')
    gateway=g.prepare(private/'policy-private.json',previous['nginx']['path'],
        old/'certificate.pem',old/'private-key.pem',new)
    # Compare private configs in memory, never export credentials in a diff.
    expected=(old/'nginx.conf').read_text().replace(str(old),str(new))
    expected=expected.replace('/v1/chat/cancel','/v1/requests/cancel')
    g.require(expected==(new/'nginx.conf').read_text(),'configuration changed beyond cancel route')
    proof['only_rendered_change']='cancel route and private package directory'
    pkg.save(BASE/'correction-reuse.json',proof)
    value=dict(prior,limits=model.LIMITS,gpu_window_seconds=model.GPU_WINDOW,
        supervisor_http_upper_bound=model.SUPERVISOR_BOUND,gateway=gateway,
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
        source_files={name:g.sha(ROOT/name) for name in SOURCES},
        corrective_cpu_sha256=g.sha(CPU/'result.json'),reuse_sha256=g.sha(BASE/'correction-reuse.json'),
        extra_attempts=True,prior_model_generations=7,prior_model_starts=1,
        original_plan_not_retroactively_accepted=True)
    pkg.save(PLAN,value)
    return dict(prepared=True,model_or_listener_started=False,limits=model.LIMITS)


def check():
    plan=pkg.read(PLAN)
    g.require(plan['limits']==model.LIMITS and plan['gpu_window_seconds']==model.GPU_WINDOW,'budget changed')
    for name,digest in plan['source_files'].items():g.require(g.sha(ROOT/name)==digest,'source changed: '+name)
    for name,key in ((CPU/'result.json','corrective_cpu_sha256'),
                     (BASE/'correction-reuse.json','reuse_sha256')):
        g.require(g.sha(name)==plan[key],'corrective evidence changed')
    g.require(reuse().items() <= pkg.read(BASE/'correction-reuse.json').items(),'reuse evidence changed')
    pkg.verify(plan['package']['package'],plan['package']['manifest_sha256'])
    g.check(plan['gateway']['package'],plan['gateway']['manifest_sha256'])
    g.require(g.sha(BASE/'model-private/policy-private.json')==plan['private_policy_sha256'],'keys changed')
    g.require(g.sha(ROOT/'build/q4t')==plan['package']['binary_sha256'] and
        g.sha(ROOT/'build/q4t.release.json')==plan['default_release_sha256'],'default pair changed')
    return plan


class Run(model.Run):
    def business(self):
        raw=self.wire('old-route-refused',model.a.ordinary(),expected=404,kind='negatives',
                      path='/v1/chat/cancel')
        g.require(json.loads(raw)['error']['code']=='gateway_route_not_found','obsolete route forwarded')
        model.a.healthy_idle(self.metrics('before-cancel'),0)
        self.cancel_business(prior_success=0,grammar_workers=0)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('cpu','prepare','check','run'))
    args=parser.parse_args();configure()
    if args.action=='cpu':value=cpu()
    elif args.action=='prepare':value=prepare()
    else:
        value=check()
        if args.action=='run':
            with sup.processes.child_subreaper():value=Run(value).execute()
    print(json.dumps(value))
