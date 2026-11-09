"""N07 targeted runtime checks; production CLI handler under counted launchers.

Only this harness accepts one fingerprinted pending engine. The actual ELF
dispatcher/packaging/publication have separate CPU evidence. No fault injection,
automatic request replay, external gateway, or performance acceptance is added.
"""
import argparse
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys

sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
spec=importlib.util.spec_from_file_location('q4t_n07_common',ROOT/'tools/serve_recovery/acceptance.py')
common=importlib.util.module_from_spec(spec);spec.loader.exec_module(common)
common.__file__=__file__
original_load=common.load_modules
LIMITS=dict(starts=1,chats=5,probes=0,controls=2000,seconds=600)
save=common.save


def fingerprint(value):
    import hashlib
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def validation_gate(original,expected):
    def check(metadata,decision):
        if metadata.get('validation_only') is not True:return original(metadata,decision)
        if not (metadata.get('acceptance') == 'VALIDATION_ONLY_N07' and
                decision.get('status') == 'PENDING_REAL_BUSINESS_N07' and
                fingerprint(metadata) == expected['metadata'] and
                fingerprint(decision) == expected['decision']):
            raise ValueError('N07 pending release identity changed')
    return check


def load_modules(plan=None):
    result=original_load(plan)
    if plan:
        engine=result[-1]
        original=getattr(engine,'_n07_original_acceptance',engine.accepted_release)
        engine._n07_original_acceptance=original
        engine.accepted_release=validation_gate(original,plan['validation_release'])
    return result


def verify(plan):
    bundle,_,_,_,_,engine=load_modules(plan)
    engine.require(plan['scenario']=='native_service_lifecycle' and plan['limits']==LIMITS,
                   'N07 frozen budget changed')
    for name,digest in plan['sources'].items():
        engine.require(engine.sha(ROOT/name)==digest,'N07 source drift: '+name)
    for row in plan['protected']:
        engine.require(engine.sha(row['path'])==row['sha256'],'N07 protected identity changed')
    manifest=bundle.verify(plan['package']['package'],plan['package']['manifest_sha256'])
    engine.require(manifest['version']==10 and manifest['ingress_mode']=='native' and
        not manifest['tls'],'N07 native loopback validation profile required')
    core=engine.verify(Path(plan['package']['package'])/'engine',manifest['engine_manifest_sha256'])
    engine.require(core['version']==11 and core['settings']['profile']==plan['capacity_profile'],
                   'N07 capacity differs from frozen plan')
    return manifest


common.load_modules,common.verify=load_modules,verify


def check_configuration(profile,config):
    from tools.serve_ops import contracts
    from tools.serve_service import capacity,supervisor
    observed=contracts.configuration(config,version=config['observability_version'])
    expected=supervisor.expected_configuration(profile)
    if any(not supervisor.pkg.same_json(observed.get(k),v) for k,v in expected.items()):
        raise ValueError('N07 effective supervised configuration differs')
    capacity.observation(profile,observed)


def launcher(plan,plan_path):
    """Exercise service.main(run), wrapping only pre-dispatch budget counters."""
    _,_,_,_,service,engine=load_modules(plan);verify(plan)
    output=Path(plan['output'])/'execution'
    original=subprocess.Popen
    guardian=str(Path(plan['package']['package'])/'bin/q4t-guardian')
    def spawn(command,**options):
        if command[0]==guardian:
            path=Path(command[command.index('--config')+1]);value=engine.read(path)
            production=value['controller']
            value['controller']=[sys.executable,'-I','-B',str(Path(__file__).resolve()),
                'controller','--plan',str(plan_path),'--config',production[-1]]
            engine.save(path,value)
            save(output/'controller-wrapper.json',dict(production=production,
                validation=value['controller'],scope='pre-dispatch startup/self-check counters'))
        return original(command,**options)
    subprocess.Popen=spawn
    try:
        rc=service.main(['run','--state',str(output/'state')],
            package=Path(plan['package']['package']),digest=plan['package']['manifest_sha256'])
        save(output/'cli-run-result.json',dict(returncode=rc,entry='sealed service.main(run)',
                                              cpp_dispatch_evidence='serve-lifecycle-cli-20261109'))
        return rc
    finally:subprocess.Popen=original


class Stage(common.Stage):
    def cli(self,operation,*args):
        with contextlib.redirect_stdout(io.StringIO()) as out,contextlib.redirect_stderr(io.StringIO()) as err:
            rc=self.service.main([operation,'--state',str(self.output/'state'),*map(str,args)],
                package=self.package,digest=self.plan['package']['manifest_sha256'])
        value=json.loads(out.getvalue() or err.getvalue())
        save(self.output/('cli-'+operation+'.json'),dict(returncode=rc,value=value))
        self.engine.require(rc==0,'N07 CLI operation failed: '+operation)
        return value

    def exercise(self):
        from tools.examples import chat_client
        from tools.examples.project_records import ProjectRecords,VALIDATORS
        from tools.serve_ops import transport
        from tools.serve_recovery import quota_manager,deployment
        first=self.ready(1)
        config=transport.get(self.policy['upstream_operations_port'],'/v1/config',timeout=3)
        self.engine.require(config['http_status']==200,'N07 config HTTP')
        config=json.loads(config['body']);save(self.output/'configuration.json',config)
        profile=self.plan['capacity_profile']
        check_configuration(profile,config)
        caps=self.request('capabilities',path='/v1/capabilities')
        save(self.output/'capabilities.json',caps)
        self.engine.require(caps['protocol_source_sha256']==self.plan['protocol_sha256'],
                            'N07 live protocol source differs')
        self.cli('check');self.cli('status')
        for label in ('json','sse'):
            value=self.request(label,copy.deepcopy(self.plan['requests'][label]))
            self.engine.require(value.finish_reason=='stop' and value.message['content'].strip()=='42',
                                'N07 arithmetic quality: '+label)
        tool=copy.deepcopy(self.plan['requests']['tool']);value=self.request('tool',tool)
        calls=chat_client.prepare_tool_calls(value,VALIDATORS)
        self.engine.require(len(calls)==1 and calls[0].arguments==
            dict(path='docs/MODEL.md',start_line=1,line_count=3),'N07 tool contract')
        actual=ProjectRecords(ROOT).execute(calls[0]);save(self.output/'tool-execution.json',actual)
        self.engine.require(actual['ok'] and actual['source']['sha256']==self.plan['sources']['docs/MODEL.md'],
                            'N07 actual project record source')
        follow=copy.deepcopy(self.plan['requests']['follow'])
        follow['messages']=chat_client.append_tool_results(tool['messages'],value,
            {calls[0].call_id:json.dumps(actual,ensure_ascii=False)})
        value=self.request('tool-final',follow)
        self.engine.require(value.finish_reason=='stop' and actual['source']['citation'] in
                            value.message['content'],'N07 tool closure quality')
        desc=deployment.read_descriptor(self.output/'state')
        clients=copy.deepcopy(quota_manager.base(desc)['clients'])
        clients[0]['requests_per_minute']+=1
        path=self.output/'limits-input.json';save(path,dict(version=1,clients=clients))
        proposal=self.output/'limits-proposal.json'
        quota_manager.propose(self.output/'state',desc,path,proposal)
        result=self.cli('reload','--component','limits','--proposal',proposal,'--wait-seconds','5')
        self.engine.require(result['live_applied'] and result['reloaded'],'N07 limits reload not live')
        after=self.sample()
        self.engine.require(all(first[k]==after[k] for k in
            ('model_pid','model_start_ticks','model_generation')),'N07 unexpected model restart')
        self.metrics('terminal-drain',5)
        save(self.output/'business-pass.json',dict(status='PASS_NATIVE_LIFECYCLE_BUSINESS',
            actual_capacity=profile,model_generations=1,tool_executions=1,
            cli_handler=['run','check','status','reload'],external_gateway=False))

    def stop(self):
        try:
            if self.process is not None and self.process.poll() is None and self.service_id is not None:
                result=self.cli('stop')
                self.engine.require(result['cleanup_confirmed'] and result['stop_durable'],
                                    'N07 CLI stop not durable')
                save(self.output/'stop.json',result)
                self.process.wait(timeout=10)
        finally:super().stop()


def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=('run','launcher','controller'))
    parser.add_argument('--plan',type=Path,required=True);parser.add_argument('--config',type=Path)
    args=parser.parse_args();plan=json.loads(args.plan.read_bytes())
    if args.mode=='launcher':return launcher(plan,args.plan)
    if args.mode=='controller':common.controller_actor(plan,args.config);return 0
    from tools.release.release import child_subreaper
    with child_subreaper():return Stage(plan,args.plan).run()


if __name__=='__main__':raise SystemExit(main())
