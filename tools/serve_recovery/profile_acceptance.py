"""Qualify exact managed primary/light capacities with actual bounded business.

Two starts, six Chat and two native embedding requests under one strict lease.
Final qualification is constructed only after guard exit and complete cleanup.
This does not enable deployment or certify automatic managed profile switching.
"""
import argparse
import copy
import fcntl
import http.client
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_ops import contracts
from tools.serve_recovery import bundle, profile_contract, profile_evidence
from tools.serve_responses import background_acceptance as background
from tools.serve_responses import encryption_acceptance as encrypted
from tools.serve_service import package
from tools.serve_service.package import read, require, save, sha

LIMITS = dict(starts=2, chats=8, controls=2000, seconds=900)
DOCUMENT = 'docs/SERVE_MEMORY_STOP_2026-11-03.md'
SUCCESS = 'PASS_QUALIFIED_PRIMARY_LIGHT_BUSINESS'


def requests():
    base = dict(model='qwen3.8-flash-next', temperature=0,
                enable_thinking=False, max_completion_tokens=256)
    schema = dict(type='object', properties=dict(answer=dict(type='integer', const=42)),
                  required=['answer'], additionalProperties=False)
    tool = dict(type='function', function=dict(name='project_document', strict=True,
        description='Read an actual project document and return its SHA256 digest.',
        parameters=dict(type='object', properties=dict(path=dict(type='string', const=DOCUMENT)),
                        required=['path'], additionalProperties=False)))
    return dict(
        schema=dict(base, messages=[dict(role='user', content='Compute 17+25 as JSON.')],
            response_format=dict(type='json_schema', json_schema=dict(
                name='arithmetic', strict=True, schema=schema))),
        tool=dict(base, messages=[dict(role='user', content='Use project_document to read '+
            DOCUMENT+'. Then report only the first 12 hexadecimal digits of its actual SHA256.')],
            tools=[tool], tool_choice='required', parallel_tool_calls=False),
        embedding=dict(model='qwen3-embedding-0.6b', input='!',
                       dimensions=32, encoding_format='float'))


def command(plan, name, directory):
    base = copy.deepcopy(plan['base'])
    base['settings'].update(profile=plan['profiles'][name], port=plan['port'],
                            operations_port=plan['operations_port'])
    args = package.command(directory, base, state_directory=directory)
    args[0] = plan['binary']
    return args


def prepare(args):
    background.prepare(args, rows=requests(), limits=LIMITS, scope=__doc__)
    path = args.output.resolve()/'plan.json'
    plan = read(path)
    engine = args.package.resolve()/'engine'
    base = read(engine/'manifest.json')
    manifest = read(engine.parent/'manifest.json')
    require(plan['binary_sha256'] == base['files']['q4t'], 'qualification engine differs')
    require(base['settings'].get('native_embedding') is not None,
            'this acceptance requires the delivered native embedding backend')
    require(not base['settings']['application_state']['directory'],
            'qualification must use a private application directory')
    plan.update(base=base, profiles=profile_contract.profiles(base['settings']['profile']),
        identity=profile_contract.binding(base['files']['q4t'], base['protocol_sha256'],
            base['model_files'], manifest['model_assets'], base['settings']['native_embedding']))
    plan['sources'][DOCUMENT] = sha(ROOT/DOCUMENT)
    plan['sources'][str(engine/'manifest.json')] = sha(engine/'manifest.json')
    assets = Path(base['settings']['native_embedding']['directory'])
    plan['native_assets'] = {str(assets/name):sha(assets/name) for name in
                            ['config.json', 'tokenizer.json', 'model.safetensors']}
    for name in ('access-program-policy.json', 'file-input-policy.json'):
        if (engine/name).is_file():
            plan['sources'][str(engine/name)] = sha(engine/name)
            shutil.copyfile(engine/name, path.parent/name)
    plan['planned'] = dict(starts=2, requests=8, chats=6, native_embeddings=2,
                           native_embedding_steps=60)
    plan['mutations'] = ['gracefully stop owned primary after four requests',
        'start only qualified light command in same lease/private directory',
        'default deployment and its state remain untouched']
    plan['derived_requests'] = ['tool results computed from actual fixed project file',
        'final conversation uses actual call ID and result, no request retries',
        'qualification only after strict guard completed and both models cleaned']
    save(path, plan)


class Stage(encrypted.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = SUCCESS
    requests_per_minute = 16

    def start(self):
        name = profile_contract.NAMES[self.counts['starts']]
        self.profile_name = name
        background.probe_ports((self.plan['port'], self.plan['operations_port']))
        for path, digest in self.plan['native_assets'].items():
            require(sha(path) == digest, 'native embedding asset drift')
        save(self.out/'client-policy.json', self.client_policy())
        args = command(self.plan, name, self.out)
        save(self.out/(name+'-command.json'), args)
        self.budget('starts')
        with (self.out/(name+'-model.log')).open('xb', buffering=0) as log:
            self.model = subprocess.Popen(args, cwd=ROOT, stdout=log,
                stderr=subprocess.STDOUT, pass_fds=(self.lease,))
        guard = encrypted.foreground.base.lifecycle.guard
        self.model_identity = guard.processes()[self.model.pid]
        self.history[self.model.pid] = self.model_identity
        save(self.out/(name+'-identity.json'), dict(pid=self.model.pid, **self.model_identity))
        end = time.monotonic()+180
        while time.monotonic() < end:
            self.same_model()
            try:
                if self.http('/readyz', timeout=1)[0] == 200: break
            except (OSError, http.client.HTTPException): pass
            time.sleep(.1)
        else: raise TimeoutError('qualified profile readiness')
        for key in ('config', 'capabilities'):
            status, raw = self.http('/v1/'+key, label=name+'-'+key)
            require(status == 200, 'missing actual '+key)
            save(self.out/(name+'-'+key+'.json'), json.loads(raw))
        config = read(self.out/(name+'-config.json'))
        profile_contract.configuration(self.plan['profiles'][name],
                                       contracts.configuration(config, version=5))
        profile_contract.startup_memory(config['startup_memory']['checks']['before_allocations'])

    def request(self, label, body, path='/v1/chat/completions'):
        label = self.profile_name+'-'+label
        save(self.out/(label+'.request.json'), body)
        self.budget('chats')
        status, raw = self.http(path, body, label=label, timeout=120)
        require(status == 200, 'business request failed: '+label)
        result = json.loads(raw)
        self.same_model()
        self.drained(label+'-drain')
        return result

    def profile_business(self):
        rows = self.plan['requests']
        schema = self.request('schema', rows['schema'])
        call = self.request('tool', rows['tool'])
        message = profile_evidence.message(call, 'tool_calls')
        calls = message.get('tool_calls', [])
        require(len(calls) == 1 and calls[0]['function']['name'] == 'project_document' and
            json.loads(calls[0]['function']['arguments']) == dict(path=DOCUMENT),
            'unsafe or different tool request')
        source = dict(path=DOCUMENT, content=(ROOT/DOCUMENT).read_text())
        require(sha(ROOT/DOCUMENT) == self.plan['sources'][DOCUMENT], 'project document drift')
        reply = dict(role='tool', tool_call_id=calls[0]['id'], content=json.dumps(dict(
            path=DOCUMENT, sha256=sha(ROOT/DOCUMENT), answer=sha(ROOT/DOCUMENT)[:12])))
        follow = copy.deepcopy(rows['tool'])
        follow['tool_choice'] = 'none'
        follow['messages'] += [message, reply]
        result = self.request('tool-result', follow)
        embedding = self.request('embedding', rows['embedding'], '/v1/embeddings')
        value = dict(schema=schema, tool_call=call, tool_result=result,
                     tool_reply=reply, tool_source=source, embedding=embedding)
        profile_evidence.business(value, self.plan['identity'])
        save(self.out/(self.profile_name+'-business.json'), value)

    def business(self):
        self.profile_business()
        self.cleanup()
        first = read(self.out/'cleanup.json')
        require(first['model_exit'] == 0, 'primary did not stop normally')
        save(self.out/'primary-cleanup.json', first)
        self.model = None
        self.start()
        self.profile_business()
        profile_contract.capability_pair(read(self.out/'primary-capabilities.json'),
                                         read(self.out/'light-capabilities.json'))


def environment(exit_record, queries, cleanups):
    require(exit_record.get('status') == 'COMPLETED' and
        exit_record.get('child_returncode') == 0 and
        exit_record.get('compute_backend') == 'nvml' and
        exit_record.get('lease_inherited_by_child') is True,
        'strict GPU environment not accepted')
    require(queries and all(q.get('status') == 'OK' and q.get('returncode') == 0
                            for q in queries), 'missing or failed NVML observation')
    require(len(cleanups) == 2 and all(c.get('remaining') == [] and
        c.get('model_exit') == 0 and c.get('ports_released') is True and
        c.get('lease_continuously_held') is True for c in cleanups),
        'model cleanup not accepted')
    return dict(status='PASS', env_abort=False, all_queries_ok=True,
                nvml_queries=len(queries), cleanup_complete=True)


def finalize(out):
    plan = read(out/'plan.json')
    result = read(out/'result.json')
    require(result['status'] == SUCCESS and result['counts']['starts'] == 2 and
        result['counts']['chats'] == 8 and result['seconds'] < LIMITS['seconds'],
        'business result or budget not accepted')
    for name, digest in {**plan['sources'], **plan['protected'], **plan['native_assets']}.items():
        require(sha(ROOT/name) == digest, 'postflight identity drift: '+name)
    require(sha(plan['binary']) == plan['binary_sha256'], 'engine drift')
    bundle.check_assets(plan['model'], plan['model_assets'])
    lease = out/'lease'
    queries = [json.loads(line) for line in (lease/'nvml-queries.jsonl').read_text().splitlines()]
    cleanups = [read(out/name) for name in ('primary-cleanup.json', 'cleanup.json')]
    env = environment(read(lease/'exit.json'), queries, cleanups)
    env['lease_result_sha256'] = sha(lease/'exit.json')
    guard = encrypted.foreground.base.lifecycle.guard
    records = guard.processes()
    require(not any(records.get(int(pid), {}).get('start_ticks') == row['start_ticks']
        for c in cleanups for pid,row in c['owned'].items()), 'owned process still alive')
    background.probe_ports((plan['port'], plan['operations_port']))
    with guard.common_lock().open('r+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    qualified = out/'qualified'
    qualified.mkdir(mode=0o700)
    registry = dict(format=profile_contract.FORMAT, identity=plan['identity'],
                    profiles=plan['profiles'], qualifications={})
    for name in profile_contract.NAMES:
        config = read(out/(name+'-config.json'))
        value = dict(format=profile_evidence.FORMAT, profile=name,
            identity=plan['identity'], capacity=plan['profiles'][name],
            capabilities=read(out/(name+'-capabilities.json')),
            configuration=contracts.configuration(config, version=5),
            startup_memory=config['startup_memory']['checks']['before_allocations'],
            business=read(out/(name+'-business.json')), environment=env)
        path = qualified/(name+'.json')
        save(path, value)
        registry['qualifications'][name] = profile_contract.qualification(name,
            value['capacity'], value['identity'], value['capabilities'],
            value['configuration'], value['startup_memory'], sha(path))
    profile_contract.validate(registry)
    for name in profile_contract.NAMES:
        path = qualified/(name+'.json')
        profile_evidence.validate(read(path), registry, name, sha(path))
    save(qualified/'registry.json', registry)
    save(out/'acceptance.json', dict(status='ACCEPT_PRIMARY_LIGHT_QUALIFICATION',
        environment=env, counts=result['counts'], seconds=result['seconds'],
        registry_sha256=sha(qualified/'registry.json'), default_unchanged=True,
        automatic_managed_switching_accepted=False, performance_claim=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='mode', required=True)
    p = sub.add_parser('prepare')
    for name in ('binary', 'package', 'output'):
        p.add_argument('--'+name, type=Path, required=True)
    p.add_argument('--port', type=int, default=19069)
    p = sub.add_parser('run'); p.add_argument('--plan', type=Path, required=True)
    p = sub.add_parser('finalize'); p.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.mode == 'prepare': prepare(args)
    elif args.mode == 'finalize': finalize(args.output.resolve())
    else:
        def timeout(_signal, _frame): raise TimeoutError('profile qualification deadline')
        prior = signal.signal(signal.SIGALRM, timeout)
        try:
            signal.setitimer(signal.ITIMER_REAL, LIMITS['seconds']-40)
            with encrypted.foreground.base.lifecycle.child_subreaper():
                Stage(read(args.plan)).run()
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, prior)


if __name__ == '__main__': main()
