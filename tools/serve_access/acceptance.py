"""N04 TLS, credentials and quota continuity; one strict lease, no production faults.

The pending engine can run only through an exact-identity validation gate in
this harness. Deployable package verification never accepts a pending release.
"""
import argparse
import copy
from dataclasses import asdict
import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import ssl
import subprocess
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('q4t_access_common', ROOT/'tools/serve_recovery/acceptance.py')
common = importlib.util.module_from_spec(spec);spec.loader.exec_module(common)
common.__file__ = __file__
original_load = common.load_modules
LIMITS = dict(starts=3, chats=16, probes=1, controls=6000, seconds=1200)
CONFIG_COMPLETION_LIMITS = dict(LIMITS, starts=4, chats=17)
save, Budget = common.save, common.Budget


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=False).encode()).hexdigest()


def validation_gate(original, expected):
    def check(metadata, decision):
        if metadata.get('validation_only') is not True:return original(metadata, decision)
        if (metadata.get('acceptance') != 'VALIDATION_ONLY_N04' or
            decision.get('status') != 'PENDING_REAL_BUSINESS_N04' or
            fingerprint(metadata) != expected['metadata'] or fingerprint(decision) != expected['decision']):
            raise ValueError('N04 validation release differs from frozen identity')
    return check


def load_modules(plan=None):
    result = original_load(plan)
    if plan:
        engine = result[-1]
        original = getattr(engine, '_n04_original_acceptance', engine.accepted_release)
        engine._n04_original_acceptance = original
        engine.accepted_release = validation_gate(original, plan['validation_release'])
    return result


def verify_sdk(sdk, *, dispatch=False):
    # Guardian deliberately gives controllers a closed environment without
    # PATH. Node is a test-client dependency, never a controller dependency.
    executable = Path(sdk['node']['path'])
    with executable.open('rb') as stream:
        executable_hash = hashlib.file_digest(stream, 'sha256').hexdigest()
    if (str(executable.resolve(strict=True)) != sdk['node']['path'] or
            executable_hash != sdk['node']['sha256']):
        raise ValueError('N04 pinned Node executable changed')
    if dispatch:
        selected = shutil.which('node')
        if selected is None or Path(selected).resolve(strict=True) != executable:
            raise ValueError('N04 client Node executable changed')
    for name, digest in sdk['files'].items():
        if hashlib.sha256((Path(sdk['sdk_directory'])/name).read_bytes()).hexdigest() != digest:
            raise ValueError('N04 SDK file changed')


def verify_prior(plan):
    if not plan.get('prior_evidence'):
        return
    for row in plan['prior_evidence']:
        if hashlib.sha256(Path(row['path']).read_bytes()).hexdigest() != row['sha256']:
            raise ValueError('N04 prior failure evidence changed')
    prior = json.loads(Path(plan['prior_execution']).read_bytes())
    guard = json.loads(Path(plan['prior_guard']).read_bytes())
    probe = json.loads(Path(plan['prior_probe']).read_bytes())
    counts = prior['budget']['counts']
    if plan.get('prior_failure_category') == 'config_schema':
        stopped_before_business = (counts['starts'] == counts['chats'] == 1 and
            list(prior['models']) == ['1'] and 'current_client_policy' in prior['failure'] and
            'jsonschema.exceptions.ValidationError' in prior['failure'])
    else:
        stopped_before_business = not prior['models'] and counts['starts'] == counts['chats'] == 0
    if (prior['status'] != 'FAILED' or prior['cleanup_failure'] is not None or
            not stopped_before_business or counts['probes'] != 1 or counts != plan['prior_counts'] or
            guard['status'] != 'COMMAND_FAILED' or
            probe != dict(version=1, ok=True, cuda_code=0, stage='complete') or
            prior['package'] != plan['package'] or
            plan['prior_seconds'] < prior['seconds']):
        raise ValueError('N04 previous attempt cannot be reused')


def verify(plan):
    bundle, _, _, _, _, engine = load_modules(plan)
    limits = CONFIG_COMPLETION_LIMITS if plan.get('prior_failure_category') == 'config_schema' else LIMITS
    engine.require(plan['scenario'] == 'native_client_access' and plan['limits'] == limits,
                   'N04 frozen budget changed')
    for name, digest in plan['sources'].items():
        engine.require(engine.sha(ROOT/name) == digest, 'N04 source changed: '+name)
    for row in [*plan['protected'], *plan['tls_inputs'].values()]:
        engine.require(engine.sha(row['path']) == row['sha256'], 'N04 protected input changed')
    verify_sdk(plan['sdk'])
    verify_prior(plan)
    value = bundle.verify(plan['package']['package'], plan['package']['manifest_sha256'])
    engine.require(value['version'] == 8 and value['ingress_mode'] == 'native' and value['tls'],
                   'N04 requires managed native TLS')
    return value


common.load_modules, common.verify = load_modules, verify


def controller_actor(plan, config):
    """Observe actual policy/usage state; only count work before dispatch."""
    load_modules(plan)
    from tools.serve_recovery import controller
    original = controller.Controller.tick
    last = [0.0]
    generation = int(os.environ['Q4T_CONTROLLER_GENERATION'])
    def tick(actor, **kwargs):
        result = original(actor, **kwargs)
        now = time.monotonic()
        if now-last[0] >= .25:
            last[0] = now
            save(Path(plan['output'])/'execution'/f'client-runtime-{generation}.json', dict(
                monotonic=now, phase=actor.phase, limits=actor.policies.status() if actor.policies else None,
                usage=actor.usage.status() if actor.usage else None))
        return result
    controller.Controller.tick = tick
    # Existing explicit wrappers count starts/self-check and refuse accidental
    # metric gating. No auxiliary-fault marker is created by this scenario.
    common.controller_actor(plan, config)


def answer_body(label, *, stream=False, store=False):
    from tools.examples import chat_client
    body = chat_client.make_request([dict(role='user', content='Compute 17 + 25. Reply with the number only.')],
        [], stream=stream, max_tokens=128, timeout_ms=60000)
    body['request_id'] = 'n04-'+label
    if store:body['store'] = True
    return body


class Stage(common.Stage):
    def __init__(self, plan, plan_path):
        super().__init__(plan, plan_path)
        self.context = ssl.create_default_context(cafile=plan['tls_inputs']['ca']['path'])
        self.scope = self.output/'state'
        self.active_package = self.package
        self.active_digest = plan['package']['manifest_sha256']
        self.saved_files = []
        self.verdict = {}

    def request(self, label, body=None, *, client='alpha', path='/v1/models', expected=200):
        from tools.examples import chat_client
        self.budget.reserve('chats' if body is not None else 'controls', label)
        if body is not None:
            path = '/v1/chat/completions';save(self.output/(label+'-request.json'), body)
        conn = http.client.HTTPSConnection('127.0.0.1', self.port, context=self.context, timeout=65)
        started = time.monotonic()
        try:
            conn.request('POST' if body is not None else 'GET', path,
                json.dumps(body).encode() if body is not None else None,
                {'Authorization':'Bearer '+self.keys[client], 'Content-Type':'application/json'})
            response = conn.getresponse()
            with (self.output/(label+'-wire.bin')).open('xb') as wire:
                if body is not None and response.status == expected == 200:
                    value = (chat_client.read_sse if body['stream'] else chat_client.read_json)(response, sink=wire.write)
                    report = asdict(value)
                else:
                    raw = response.read(1024*1024);wire.write(raw);report = value = json.loads(raw)
            save(self.output/(label+'-response.json'), dict(status=response.status,
                headers=dict(response.getheaders()), seconds=time.monotonic()-started, value=report))
            self.engine.require(response.status == expected, 'unexpected N04 HTTP status: '+label)
            return value
        finally:conn.close()

    def management(self, operation, label, *arguments):
        """Same packaged CLI parser, under the explicitly pending validation gate."""
        import contextlib
        import io
        self.budget.reserve('controls', operation)
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = self.service.main([operation, '--state', str(self.scope), *map(str, arguments)],
                                    package=self.active_package, digest=self.active_digest)
        raw = output.getvalue()+errors.getvalue()
        self.engine.require(not any(key and key in raw for key in self.keys.values()),
                            'credential appeared in management output')
        result = json.loads(raw)
        save(self.output/(label+'-management.json'), dict(exit_code=code, result=result))
        self.engine.require(code == 0, 'N04 management failed: '+label)
        return result

    def until(self, label, read, predicate, seconds=12):
        end = time.monotonic()+seconds
        while time.monotonic()<end:
            value = read()
            if predicate(value):return value
            time.sleep(.1)
        raise TimeoutError('N04 bounded wait: '+label)

    def apply(self, family, label, value):
        source = self.output/(label+'-private.json');save(source, value);source.chmod(0o600)
        proposal = self.output/(label+'-proposal.json')
        self.management(family+'-propose', label+'-propose',
            '--keys' if family == 'access' else '--limits', source, '--proposal', proposal)
        result = self.management(family+'-apply', label+'-apply', '--proposal', proposal, '--wait-seconds', 3)
        self.engine.require(result['durable'], 'policy not durable')
        if not result['live_applied']:
            self.until(label, lambda:self.management(family+'-status', label+'-status'),
                       lambda v:v['live_applied'])
        return result

    def limits(self, label, *, alpha_tpm=None, beta_rpm=None, add_beta=False):
        from tools.serve_recovery import access_store, quota_store
        binding = access_store.binding(self.scope, self.service_id)
        with quota_store.open_bound(binding) as current:
            clients = copy.deepcopy(current.head['state']['policy']['clients'])
        if add_beta:clients.append(dict(clients[0], id='beta'))
        for row in clients:
            if row['id'] == 'alpha' and alpha_tpm is not None:row['tokens_per_minute'] = alpha_tpm
            if row['id'] == 'beta' and beta_rpm is not None:row['requests_per_minute'] = beta_rpm
        return self.apply('limits', label, dict(version=1, clients=clients))

    def keys_update(self, label, alpha, *, beta=None):
        rows = [dict(id='alpha', api_keys=alpha)]
        if beta is not None:rows.append(dict(id='beta', api_keys=[beta]))
        return self.apply('access', label, dict(version=1, clients=rows))

    def sdk(self, label, body, *, client='alpha'):
        from tools.serve_protocol.js_client import JsSdkClient
        verify_sdk(self.plan['sdk'], dispatch=True)
        self.budget.reserve('chats', label);save(self.output/(label+'-request.json'), body)
        sdk = JsSdkClient(f'https://127.0.0.1:{self.port}/v1', self.plan['sdk']['sdk_directory'],
            protocol_sha256=self.plan['protocol_sha256'], timeout=65,
            api_key=self.keys[client], ca_file=self.plan['tls_inputs']['ca']['path'])
        with (self.output/(label+'-wire.bin')).open('xb') as wire:value = sdk.complete(body, sink=wire.write)
        save(self.output/(label+'-response.json'), asdict(value))
        self.engine.require(value.finish_reason == 'stop' and value.message['content'].strip() == '42',
                            'N04 official SDK task quality')
        return value

    def ready(self, generation, *, mode):
        result = self.wait(lambda s:s['model_generation'] == generation and s['business_ready'])
        observed_ready = time.monotonic()
        status = self.management('usage-status', 'ready-'+str(generation)+'-usage')
        self.engine.require(status['continuity'] == mode, 'usage recovery mode differs')
        if generation > 1 and mode != 'exact':
            deadline = self.quota_deadlines.get(generation)
            self.engine.require(deadline is not None and observed_ready >= deadline-.05,
                                'unknown recovery admitted before complete window')
        save(self.output/f'ready-{generation}.json', result)
        return result

    def same(self, before):
        after = self.sample()
        self.engine.require(all(after[k] == before[k] for k in ('guardian_pid', 'controller_pid',
            'model_pid', 'model_start_ticks', 'model_generation')) and after['business_ready'],
            'auxiliary/policy change disturbed healthy core')

    def unavailable_file(self, family):
        path = self.scope/('client-'+family)/'policy.json'
        saved = path.with_name('acceptance-unavailable.json')
        self.engine.require(not saved.exists(), 'fault evidence already exists')
        path.rename(saved);self.saved_files.append((path,saved))
        return path, saved

    def restore_file(self, pair):
        path, saved = pair
        self.engine.require(not path.exists() and not path.with_name('policy.pending').exists(),
                            'journal changed during unavailable-file fault')
        saved.rename(path);self.saved_files.remove(pair)

    def runtime(self, generation):
        return json.loads((self.output/f'client-runtime-{generation}.json').read_bytes())

    def start_again(self, label):
        plan = dict(self.plan, package=dict(self.plan['package'], package=str(self.active_package),
                                          manifest_sha256=self.active_digest))
        path = self.output/(label+'-plan.json');save(path, plan)
        with (self.output/(label+'-launcher.log')).open('xb') as log:
            self.process = subprocess.Popen([sys.executable, '-I', '-B', __file__,
                'launcher', '--plan', str(path)], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                pass_fds=(self.lease,), start_new_session=True,
                env={k:v for k,v in os.environ.items() if k not in ('DBUS_SESSION_BUS_ADDRESS','NOTIFY_SOCKET','WATCHDOG_USEC')})

    def stopped(self, label):
        super().stop()
        save(self.output/(label+'-stop.json'), self.engine.read(self.output/'stop.json'))
        self.process = None

    def exercise(self):
        from tools.examples import chat_client
        from tools.examples.project_records import ProjectRecords, VALIDATORS
        from tools.serve_recovery import access_store, usage_store
        first = self.ready(1, mode='conservative_new_window')
        caps = self.request('initial-capabilities', path='/v1/capabilities')
        self.engine.require(caps['protocol_source_sha256'] == self.plan['protocol_sha256'],
                            'actual compiled protocol identity differs')
        save(self.output/'capabilities.json', caps)
        from tools.serve_ops import transport
        from jsonschema import Draft202012Validator
        observed = transport.get(self.policy['upstream_operations_port'], '/v1/config', timeout=3)
        self.engine.require(observed['http_status'] == 200, 'actual configuration unavailable')
        config = json.loads(observed['body'])
        save(self.output/'configuration.json', config)
        Draft202012Validator(self.engine.read(ROOT/'docs/serve-config-v1.schema.json')).validate(config)
        self.keys['old-alpha'] = self.keys['alpha']
        rotated = 'n04-rotation-'+secrets.token_hex(24)
        self.keys_update('overlap', [self.keys['alpha'], rotated])
        self.keys['alpha'] = rotated
        self.request('new-key-control');self.request('old-key-overlap', client='old-alpha')
        self.keys_update('revoke-old', [rotated])
        self.request('old-key-denied', client='old-alpha', expected=401)

        tool = copy.deepcopy(self.plan['requests']['tool'])
        answer = self.request('project-tool', tool)
        calls = chat_client.prepare_tool_calls(answer, VALIDATORS)
        self.engine.require(len(calls) == 1 and calls[0].arguments ==
            dict(path='docs/MODEL.md', start_line=1, line_count=3), 'project tool arguments')
        actual = ProjectRecords(ROOT).execute(calls[0])
        self.engine.require(actual['ok'] and actual['source']['sha256'] == self.plan['sources']['docs/MODEL.md'],
                            'project source changed')
        save(self.output/'project-tool-result.json', actual)
        follow = copy.deepcopy(self.plan['requests']['follow'])
        follow['messages'] = chat_client.append_tool_results(tool['messages'], answer,
            {calls[0].call_id:json.dumps(actual,ensure_ascii=False)})
        final = self.request('project-final', follow)
        self.engine.require(final.finish_reason == 'stop' and actual['source']['citation'] in final.message['content'],
                            'real project tool closure')

        self.keys['beta'] = 'n04-new-client-'+secrets.token_hex(24)
        self.limits('add-beta', add_beta=True)
        self.keys_update('new-client-key', [rotated], beta=self.keys['beta'])
        self.sdk('new-client-sdk', answer_body('new-client-sdk'), client='beta')
        self.limits('rpm-on', beta_rpm=1)
        self.request('rpm-off-on-kept', answer_body('rpm-off-on-kept'), client='beta', expected=429)
        self.limits('rpm-two', beta_rpm=2)
        self.limits('tpm-on', alpha_tpm=1)
        self.request('tpm-off-on-kept', answer_body('tpm-off-on-kept'), expected=429)
        self.limits('tpm-off', alpha_tpm=0)

        self.keys['fault-revoked'] = 'n04-revocable-'+secrets.token_hex(24)
        self.keys_update('overlap-before-limits-loss', [rotated, self.keys['fault-revoked']], beta=self.keys['beta'])
        self.request('fault-key-before', client='fault-revoked')
        pair = self.unavailable_file('limits')
        self.until('actual limits reader error', lambda:self.runtime(1),
                   lambda v:bool(v['limits'] and v['limits'].get('error')))
        self.keys_update('revoke-during-limits-loss', [rotated], beta=self.keys['beta'])
        self.request('fault-key-after', client='fault-revoked', expected=401)
        structured = answer_body('limits-lost', stream=True)
        structured['messages'] = [dict(role='user',content='Return JSON with answer equal to 42.')]
        structured['response_format'] = dict(type='json_schema', json_schema=dict(name='n04', strict=True,
            schema=dict(type='object',properties=dict(answer=dict(type='integer',enum=[42])),
                        required=['answer'],additionalProperties=False)))
        answer = self.request('limits-lost-sse', structured)
        self.engine.require(answer.finish_reason == 'stop' and json.loads(answer.message['content']) == {'answer':42},
                            'auxiliary loss structured quality')
        self.same(first);self.restore_file(pair)
        self.until('limits reader recovery', lambda:self.runtime(1), lambda v:not v['limits'].get('error'))

        stored = self.request('beta-stored', answer_body('beta-stored', store=True), client='beta')
        self.engine.require(stored.finish_reason == 'stop' and stored.message['content'].strip() == '42', 'stored task quality')
        self.request('stored-other-client-denied', path='/v1/chat/completions/'+stored.id, expected=404)
        self.request('stored-owner', path='/v1/chat/completions/'+stored.id, client='beta')
        self.limits('rpm-one-at-close', beta_rpm=1)
        self.request('rpm-at-close', answer_body('rpm-at-close'), client='beta', expected=429)
        self.stopped('first')
        binding = access_store.binding(self.scope, self.service_id)
        with usage_store.open_bound(binding) as journal:sealed = copy.deepcopy(journal.head['state']['policy'])
        save(self.output/'first-sealed.json', sealed)
        self.engine.require(sealed['phase'] == 'sealed', 'normal stop did not seal usage')
        second = self.plan['second_package']
        self.active_package, self.active_digest = Path(second['package']), second['manifest_sha256']
        self.management('deploy', 'switch-second', '--expected-manifest-sha256', self.plan['package']['manifest_sha256'],
                        '--request-id', self.plan['switch_id'])
        self.start_again('second');second_ready = self.ready(2, mode='exact')
        self.request('revoked-after-upgrade', client='old-alpha', expected=401)
        beta = next(c for c in sealed['snapshot']['clients'] if c['id'] == 'beta')
        expiry = int(beta['window_ns'])+60_000_000_000
        observed = time.monotonic_ns()
        window_live = expiry-observed > 3_000_000_000
        # Avoid racing a legitimate rollover while constructing a TLS request.
        # This branch is frozen before the run; it never obtains extra samples.
        if not window_live and observed < expiry:time.sleep((expiry-observed)/1e9+.05)
        save(self.output/'same-window-decision.json', dict(window_live=window_live,
            old_window_ns=beta['window_ns'], observed_ns=observed, dispatched_ns=time.monotonic_ns(),
            minimum_margin_seconds=3, expected=429 if window_live else 200))
        self.request('restart-rate-window', answer_body('restart-rate-window'), client='beta',
                     expected=429 if window_live else 200)
        self.sdk('upgraded-sdk', answer_body('upgraded-sdk'))
        pair = self.unavailable_file('usage')
        self.same(second_ready);self.stopped('second-unsealed')
        self.restore_file(pair)
        with usage_store.open_bound(binding) as journal:unsealed = copy.deepcopy(journal.head['state']['policy'])
        self.engine.require(unsealed['phase'] == 'active' and unsealed['generation'] == 2,
                            'unknown handoff was incorrectly sealed')
        save(self.output/'second-unsealed.json', unsealed)
        self.active_package, self.active_digest = self.package, self.plan['package']['manifest_sha256']
        self.management('rollback', 'rollback-first', '--expected-manifest-sha256', second['manifest_sha256'],
                        '--request-id', self.plan['rollback_id'])
        self.start_again('third');self.ready(3, mode='conservative_new_window')
        self.request('revoked-after-rollback', client='old-alpha', expected=401)
        self.request('stored-after-rollback', path='/v1/chat/completions/'+stored.id, client='beta')
        answer = self.request('recovered-sse', answer_body('recovered-sse', stream=True), client='beta')
        self.engine.require(answer.finish_reason == 'stop' and answer.message['content'].strip() == '42',
                            'unknown recovery task quality')
        self.verdict = dict(rotation=True, new_client=True, off_on_usage=True,
            auxiliary_loss_same_model=True, stored_identity=True, exact_restore=True,
            same_window_checked=window_live, unknown_guard=True, compatible_rollback=True,
            official_sdk=True, actual_document_tool=True)
        save(self.output/'business-verdict.json', self.verdict)

    def stop(self):
        cleaned = False
        try:
            super().stop();cleaned = True
        finally:
            if cleaned:
                for pair in self.saved_files[:]:self.restore_file(pair)

    def run(self):
        if self.plan.get('prior_evidence'):
            # Only the documented, cleaned pre-business attempts with this
            # exact package can reuse the probe. All spent work stays charged.
            verify_prior(self.plan)
            return super().run()
        self.budget.reserve('probes', 'compiled-native-probe')
        with (self.output/'device-probe.json').open('xb') as output, \
             (self.output/'device-probe.stderr').open('xb') as errors:
            result = subprocess.run([str(self.package/'bin/q4t-cuda-probe')],
                stdin=subprocess.DEVNULL, stdout=output, stderr=errors, timeout=30,
                pass_fds=(self.lease,))
        self.engine.require(result.returncode == 0 and
            json.loads((self.output/'device-probe.json').read_bytes()) ==
                dict(version=1,ok=True,cuda_code=0,stage='complete'), 'compiled device probe failed')
        return super().run()


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('run','launcher','controller'))
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--config', type=Path)
    args = parser.parse_args();plan = json.loads(args.plan.read_bytes())
    if args.mode == 'launcher':return common.launcher(plan, args.plan)
    if args.mode == 'controller':controller_actor(plan, args.config);return 0
    from tools.release.release import child_subreaper
    with child_subreaper():return Stage(plan, args.plan).run()


if __name__ == '__main__':raise SystemExit(main())
