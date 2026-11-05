"""Two-start, single-lease real governance acceptance and reversible delivery."""
import argparse
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from dataclasses import asdict

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.serve_client_governance import cpu
from tools.serve_error_params.stage import h, a, DAYS, Children
from tools.serve_clients import short as s
from tools.serve_service import package as pkg

BASE = cpu.BASE
PACKAGE, OUTPUT = BASE/'package', BASE/'execution'
PORT, TLS_PORT = 18138, 18458
LIMITS = dict(starts=2, generation_attempts=24, controls=250, seconds=480)
PLANNED = dict(models=2, successes=12, cancelled=2, real_tools=2, refusals=2)
CPU_LABEL, BUILD_LABEL = 'cpu-01', 'build-01'


def frozen_policy():
    return dict(version=2, fair_queue=True, gateway_token=s.secrets.token_hex(32), clients=[
        dict(id='alpha', max_inflight=4, max_control_inflight=2, max_prompt_tokens=8000,
             max_output_tokens=2048, max_body_bytes=131072, requests_per_minute=6,
             tokens_per_minute=10000),
        dict(id='beta', max_inflight=4, max_control_inflight=2, max_prompt_tokens=8000,
             max_output_tokens=64, max_body_bytes=65536, requests_per_minute=20,
             tokens_per_minute=2000)])


def source_paths():
    paths = set(cpu.sources())
    paths.update(subprocess.check_output(['git', 'ls-files', 'tools', 'cmake'], cwd=ROOT, text=True).splitlines())
    paths.update(('docs/MODEL.md', 'docs/SERVE_CLIENT_GOVERNANCE_2026-11-05.md'))
    return {name: cpu.sha(ROOT/name) for name in sorted(paths)}


def prepare():
    cpu.require(not PACKAGE.exists() and not OUTPUT.exists(), 'preserve original attempt')
    cpu.require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT), 'commit stage first')
    for label in (CPU_LABEL, BUILD_LABEL):
        cpu.require(cpu.read(BASE/label/'result.json')['passed'], 'CPU/build gate')
    for name, digest in cpu.read(BASE/CPU_LABEL/'sources.json').items():
        cpu.require(cpu.sha(ROOT/name) == digest, 'CPU source drift: '+name)
    cpu.require(cpu.runtime() == cpu.read(BASE/BUILD_LABEL/'runtime.json'), 'runtime build drift')
    for name, digest in cpu.read(BASE/BUILD_LABEL/'result.json')['binaries'].items():
        cpu.require(cpu.sha(name) == digest, 'built binary drift')
    old = cpu.read(ROOT/'build/q4t.release.json')
    PACKAGE.mkdir()
    for name in ('candidate', 'previous'):
        (PACKAGE/name).mkdir()
    for name in a.NAMES:
        h.copy_checked(h.identity(ROOT/'build'/name), PACKAGE/'previous'/name)
    for field, name in [('build_cache', 'CMakeCache.txt'), ('acceptance_decision', 'acceptance.json'),
                        ('tested_source_manifest', 'runtime-source.json')]:
        h.copy_checked(h.identity(old[field]), PACKAGE/'previous'/name)
    h.copy_checked(old['operations_rules'], PACKAGE/'previous/q4t.rules.yml')
    shutil.copytree(old['release_declarations']['directory'], PACKAGE/'previous/declarations')
    for name in ('q4t', 'CMakeCache.txt', 'compile_commands.json'):
        h.copy_checked(h.identity(cpu.ENGINE/name), PACKAGE/'candidate'/name)
    shutil.copytree(cpu.ENGINE/'generated', PACKAGE/'candidate/generated')
    cpu.save(PACKAGE/'candidate/runtime-source.json', cpu.runtime())
    schema = PACKAGE/'candidate/schemas'
    schema.mkdir()
    for name in pkg.DECLARATION_SCHEMAS:
        source = (ROOT/f'docs/serve-{name}-v1.schema.json' if name in ('config', 'preflight')
                  else Path(old['chat_protocol'][name+'_schema']))
        h.copy_checked(h.identity(source), schema/(name+'.json'))
    h.copy_checked(h.identity(ROOT/'deploy/serve-ops/q4t.rules.yml'), PACKAGE/'candidate/q4t.rules.yml')
    previous = cpu.read(old['tested_source_manifest'])
    changed = sorted(n for n, d in cpu.runtime().items() if previous.get(n) != d)
    expected = cpu.read(ROOT/'tools/serve_audit/inventory.json')['client_governance_review']['changed_sources']
    cpu.require(changed == sorted(expected), 'unreviewed runtime change')
    cpu.save(PACKAGE/'numerical-reuse.json', dict(changed=changed,
        previous=h.identity(old['tested_source_manifest']), current=h.identity(PACKAGE/'candidate/runtime-source.json'),
        unchanged_runtime_sources=sum(previous.get(n) == d for n, d in cpu.runtime().items()),
        model_math_tokenizer_template_cache_unchanged=True, performance_acceptance=False))
    private = BASE/'private'
    private.mkdir(mode=0o700)
    cert, key = s.certificate(private)
    front = s.example(TLS_PORT, PORT)
    front.update(per_client_inflight=6, global_inflight=12, per_client_rps=100, burst=128)
    policy = frozen_policy()
    s.g.save(private/'clients.json', json.dumps(policy).encode())
    s.g.save(private/'gateway.json', json.dumps(front).encode())
    gateway = s.g.prepare(private/'gateway.json', cpu.read(s.VENDOR)['binary'], cert, key,
                          private/'gateway', private/'clients.json', trusted_ca=cert)
    prior = cpu.read(ROOT/'.q4t-work/serve-gateway-sources-20261104/plan.json')
    protocol = cpu.read(PACKAGE/'candidate/generated/q4t/server/chat_protocol_sources.json')
    digest = hashlib.sha256(''.join(f'{n}:{protocol[n]}\n' for n in sorted(protocol)).encode()).hexdigest()
    preserved = dict(prior['preserved'])
    for path in DAYS:
        preserved[str(path)] = cpu.sha(path)
    sources = source_paths()
    cpu.save(PACKAGE/'sources.json', sources)
    plan = dict(root=str(ROOT), limits=LIMITS, planned=PLANNED, port=PORT, tls_port=TLS_PORT,
        binary=str(PACKAGE/'candidate/q4t'), candidate=str(PACKAGE/'candidate/q4t'),
        candidate_sha256=cpu.sha(PACKAGE/'candidate/q4t'), protocol=digest, protocol_sha256=digest,
        previous_binary_sha256=cpu.sha(PACKAGE/'previous/q4t'),
        old_hashes={n: cpu.sha(PACKAGE/'previous'/n) for n in a.NAMES},
        model_dir=prior['model_dir'], model_inputs=prior['model_inputs'], gateway=gateway,
        frontend=str(private/'gateway.json'), policy=str(private/'clients.json'), preserved=preserved,
        local={str(p): cpu.sha(p) for p in (private/'clients.json', private/'gateway.json')},
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        performance_acceptance=False)
    cpu.save(PACKAGE/'plan.json', plan)
    h.seal(PACKAGE)
    return dict(prepared=True, limits=LIMITS, planned=PLANNED, models_started=0)


def check():
    h.verify_seal(PACKAGE)
    plan = cpu.read(PACKAGE/'plan.json')
    cpu.require(plan['root'] == str(ROOT) and plan['limits'] == LIMITS and plan['planned'] == PLANNED, 'frozen scope')
    a.verify_hashes(ROOT, cpu.read(PACKAGE/'sources.json'))
    a.verify_hashes(plan['model_dir'], plan['model_inputs'])
    for path, digest in {**plan['preserved'], **plan['local']}.items():
        cpu.require(cpu.sha(path) == digest, 'preserved/frozen input changed')
    s.g.check(plan['gateway']['package'], plan['gateway']['manifest_sha256'])
    if (OUTPUT/'begin.json').exists():
        plan['started'] = plan['original_started'] = cpu.read(OUTPUT/'begin.json')['started']
    return plan


def echo(stream=True):
    value = s.a.ordinary(stream)
    value['max_tokens'] = 32
    return value


def rate(metrics, client, field):
    return metrics[f'q4t_client_rate_{field}{{client="{client}"}}']


def admitted(log):
    return re.findall(r'\[q4t\]\[request\] [^\n]* id=([^ ]+) event=admitted ', log)


class Run(s.Run):
    def __init__(self, plan):
        s.OUT = OUTPUT/'business'
        s.PORT, s.TLS_PORT, s.LIMITS = PORT, TLS_PORT, LIMITS
        s.OUT.mkdir()
        super().__init__(plan)
        self.children = Children(OUTPUT/'lease')
        self.current_client = 0

    def complete(self, label, body, client=None):
        client = self.current_client if client is None else client
        body = copy.deepcopy(body)
        body['max_tokens'] = min(body.get('max_tokens', 512), 512 if client == 0 else 64)
        return super().complete(label, body, client)

    def launch(self, scoped):
        self.budget('starts')
        label = 'scoped-model' if scoped else 'legacy-model'
        argv = [self.plan['binary'], 'serve', '--model-dir', self.plan['model_dir'],
                '--host', '127.0.0.1', '--port', str(PORT), '--max-seq', '1',
                '--max-prefill', '2048', '--max-len', '8192',
                '--max-inflight-requests', '8', '--max-queued-requests', '6']
        if scoped:
            argv += ['--client-policy-file', self.plan['policy']]
        env = {k: v for k, v in os.environ.items() if not k.startswith('Q4T_') and
               k not in ('LD_PRELOAD', 'LD_LIBRARY_PATH')}
        self.model = self.children.launch(s.OUT/label, argv, env)
        until = time.monotonic()+180
        while 'serving on port' not in (s.OUT/label/'stderr.log').read_text():
            cpu.require(self.children.running(self.model) and time.monotonic() < until, 'startup failed')
            time.sleep(.1)
        config = self.request(label+'-config', path='/v1/config', method='GET', direct=True)
        s.Draft202012Validator(cpu.read(ROOT/'docs/serve-config-v1.schema.json')).validate(config)
        cpu.require(config['protocol_source_sha256'] == self.plan['protocol'], 'protocol drift')
        s.cp.observed_matches(config['client_policy'], s.cp.binding(Path(self.plan['policy']).read_bytes()) if scoped else None)
        cpu.require(config['client_governance']['fair_queue'] is scoped and
                    config['client_governance']['policy_version'] == (2 if scoped else 1), 'optional configuration')
        if scoped:
            package = Path(self.plan['gateway']['package'])
            manifest = s.g.check(package, self.plan['gateway']['manifest_sha256'])
            self.nginx = self.children.launch(s.OUT/'nginx', [manifest['nginx']['path'], '-e', 'stderr',
                '-p', str(package)+'/', '-c', str(package/'nginx.conf')], env)
            until = time.monotonic()+10
            while True:
                cpu.require(self.children.running(self.nginx) and time.monotonic() < until, 'gateway startup failed')
                try:
                    with socket.create_connection(('127.0.0.1', TLS_PORT), .1):
                        break
                except ConnectionRefusedError:
                    time.sleep(.05)
            self.tls = True
        else:
            self.tls = False

    def preflight(self, label, body, client=0):
        value = self.request(label, body, client=client, path='/v1/chat/preflight')
        schema = cpu.read(ROOT/'docs/serve-preflight-v1.schema.json')
        s.Draft202012Validator(schema['$defs']['response']).validate(value)
        cpu.require(value['client_limits']['rate_quota_reserved'] is False, 'preflight rate exemption')
        return value['prompt_tokens']+value['output']['available_tokens']

    def queued(self, label, count):
        until = time.monotonic()+8
        while True:
            values, _ = self.get_metrics(label+'-'+str(self.counts['controls']))
            if values['q4t_queued_requests'] == count:
                return values
            cpu.require(time.monotonic() < until, 'slot queue count: '+label)
            time.sleep(.1)

    def finish_pending(self, label, body, connection, response, prefix=b'', cancel=False):
        try:
            raw = self.record(label, response, prefix+response.read(8*1024**2+1), 200)
            if cancel:
                cpu.save(s.OUT/(label+'-terminal.json'), s.cancelled_wire(raw, body['request_id']))
                self.cancelled += 1
            else:
                value = s.a.read_sse(io.BytesIO(raw))
                cpu.require(value.id == body['request_id'] and value.finish_reason == 'stop' and
                            value.message['content'].strip() == '710003', 'queued answer quality')
                cpu.save(s.OUT/(label+'-response.json'), asdict(value))
                self.successes += 1
        finally:
            connection.close()

    def cancel(self, label, body, client=0):
        self.request(label, dict(request_id=body['request_id'], cancel_token=body['cancel_token']),
                     path='/v1/requests/cancel', client=client, expected=202)

    def fairness(self):
        active = echo()
        active['messages'][0]['content'] = '逐行列出从1到1000的数字，不要省略。'
        active['max_tokens'] = 2048
        active_cost = self.preflight('active-preflight', active)
        connection, response = self.begin('active', active)
        prefix = b''
        until = time.monotonic()+15
        while True:
            line = response.readline()
            cpu.require(line and len(prefix) < 262144 and time.monotonic() < until, 'active content deadline')
            prefix += line
            if line.startswith(b'data: {'):
                delta = json.loads(line[6:])['choices'][0].get('delta', {})
                if delta.get('content'):
                    break
        queued = []
        for label, client in [('a1', 0), ('a2', 0), ('a3', 0), ('b1', 1)]:
            body = echo()
            cost = self.preflight(label+'-preflight', body, client)
            conn, resp = self.begin(label, body, client=client)
            queued.append((label, body, conn, resp, cost))
            self.queued('waiting-'+label, len(queued))
        before = self.get_metrics('before-queued-cancel')[0]
        label, body, conn, resp, cost = queued[2]
        self.cancel('cancel-a3', body)
        self.finish_pending(label, body, conn, resp, cancel=True)
        after = self.queued('after-queued-cancel', 3)
        cpu.require(rate(before, 'alpha', 'reserved_tokens') - rate(after, 'alpha', 'reserved_tokens') == cost and
                    rate(before, 'alpha', 'settled_tokens_total') == rate(after, 'alpha', 'settled_tokens_total'),
                    'queued cancellation refund')
        self.cancel('cancel-active', active)
        self.finish_pending('active', active, connection, response, prefix, cancel=True)
        for i in (3, 0, 1):
            label, body, conn, resp, _ = queued[i]
            self.finish_pending(label, body, conn, resp)
        values = self.idle('fairness-idle')
        order = admitted((s.OUT/'scoped-model/stderr.log').read_text())
        expected = [active['request_id'], *(queued[i][1]['request_id'] for i in (3, 0, 1))]
        cpu.require(order[-4:] == expected and queued[2][1]['request_id'] not in order, 'actual client slot rotation')
        cpu.require(sum(rate(values, c, 'settled_tokens_total') for c in ('alpha', 'beta')) ==
                    values['q4t_prompt_tokens_total']+values['q4t_generation_tokens_total'], 'logical usage ledger')
        cpu.require(all(rate(values, c, 'reserved_tokens') == 0 for c in ('alpha', 'beta')), 'reservation leaked')
        cpu.save(s.OUT/'fairness.json', dict(order=expected, observed=order, queued_cancel_cost=cost,
            active_reservation=active_cost, before=before, after=after, final=values))

    def quotas(self):
        before = self.get_metrics('quota-before')[0]
        cpu.require(rate(before, 'alpha', 'requests') == 6, 'RPM setup')
        denied = self.request('rpm-denied', echo(), expected=429)
        cpu.require(denied['error']['code'] == 'client_rpm_exhausted', 'RPM denial')
        body = echo(False)
        body['messages'][0]['content'] = 'hello '*2500
        cost = self.preflight('tpm-preflight', body, 1)
        cpu.require(cost > 2000, 'TPM deterministic oversized reservation')
        denied = self.request('tpm-denied', body, client=1, expected=429)
        cpu.require(denied['error']['code'] == 'client_tpm_exhausted', 'TPM denial')
        after = self.get_metrics('quota-after')[0]
        for client in ('alpha', 'beta'):
            for field in ('requests', 'settled_tokens_total', 'reserved_tokens'):
                cpu.require(rate(before, client, field) == rate(after, client, field), 'rejection/preflight charged')
        value = self.complete('beta-after-refusal', echo(), client=1)
        cpu.require(value.finish_reason == 'stop' and value.message['content'].strip() == '710003', 'isolated recovery')
        self.idle('before-rollover')
        before = self.get_metrics('window-before')[0]
        remaining = max(rate(before, c, 'window_remaining_ms') for c in ('alpha', 'beta'))
        cpu.require(0 < remaining <= 60000, 'real fixed window not observed')
        # No busy polling; remain under the one original GPU lease throughout.
        time.sleep(remaining/1000+.15)
        after = self.get_metrics('window-expired')[0]
        cpu.require(all(rate(after, c, f) == 0 for c in ('alpha', 'beta')
                        for f in ('requests', 'tokens', 'reserved_tokens')), 'window rollover')
        for client, name in enumerate(('alpha', 'beta')):
            value = self.complete('window-recovery-'+name, echo(), client=client)
            cpu.require(value.finish_reason == 'stop' and value.message['content'].strip() == '710003', 'rollover answer')
        final = self.idle('quota-final')
        cpu.require(all(rate(final, c, 'requests') == 1 for c in ('alpha', 'beta')), 'new window charge')
        cpu.require(sum(rate(final, c, 'settled_tokens_total') for c in ('alpha', 'beta')) ==
                    final['q4t_prompt_tokens_total']+final['q4t_generation_tokens_total'], 'final logical ledger')
        cpu.save(s.OUT/'quotas.json', dict(before=before, expired=after, final=final, waited_ms=remaining))

    def execute(self):
        result = dict(status='FAILED', limits=LIMITS, counts=self.counts)
        try:
            s.a.require_idle(s.OUT/'initial-gpu.json')
            for port in (PORT, TLS_PORT):
                with socket.socket() as sock:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    sock.bind(('127.0.0.1', port))
            self.launch(True)
            self.request('capabilities', path='/v1/capabilities', method='GET')
            for client, name in enumerate(('alpha', 'beta')):
                self.current_client = client
                self.tools('project-'+name)
            self.current_client = 0
            self.idle('tools-idle')
            self.fairness()
            self.quotas()
            self.stop(True)
            self.launch(False)
            for stream in (True, False):
                value = self.complete('disabled-'+str(stream), echo(stream))
                cpu.require(value.finish_reason == 'stop' and value.message['content'].strip() == '710003', 'disabled quality')
            self.idle('disabled-idle')
            self.stop(False)
            self.children.reap(s.OUT)
            s.a.require_idle(s.OUT/'final-gpu.json')
            cpu.require(self.successes == PLANNED['successes'] and self.cancelled == 2, 'planned responses')
            result.update(status='PASS_BOUNDED_CLIENT_GOVERNANCE', successes=self.successes,
                          cancelled=self.cancelled, real_tools=2)
        except BaseException as error:
            result.update(status='ENV_ABORT' if 'ENV_ABORT' in str(error) else 'FAILED', error=repr(error))
            raise
        finally:
            for connection in self.sockets:
                connection.close()
            try:
                self.children.cleanup()
            except BaseException as error:
                result.update(status='FAILED', cleanup_error=repr(error))
                raise
            finally:
                result['seconds'] = time.monotonic()-self.started
                cpu.save(s.OUT/'result.json', result)


def verifier(plan, installed=False):
    if not installed:
        Run(plan).execute()
        return
    cpu.require(cpu.sha(ROOT/'build/q4t') == plan['candidate_sha256'] and
        cpu.read(OUTPUT/'business/result.json')['status'] == 'PASS_BOUNDED_CLIENT_GOVERNANCE', 'acceptance identity')
    folder = OUTPUT/'health'
    folder.mkdir()
    cpu.require(cpu.command(folder, 'version', [ROOT/'build/q4t', 'version'], 10), 'installed CLI')
    cpu.save(folder/'result.json', dict(passed=True, installed=h.identity(ROOT/'build/q4t'),
        acceptance=h.identity(OUTPUT/'business/result.json'), new_models=0, new_http=0))


def release(plan):
    result=OUTPUT/'business/result.json'
    cpu.require(cpu.read(result)['status']=='PASS_BOUNDED_CLIENT_GOVERNANCE','business gate')
    metadata=cpu.read(PACKAGE/'previous/q4t.release.json')
    inherited={k:copy.deepcopy(metadata[k]) for k in ('acceptance','acceptance_decision',
        'acceptance_decision_sha256','chat_protocol','deployment_completion','scope') if k in metadata}
    inherited['release']=h.identity(PACKAGE/'previous/q4t.release.json')
    for key in ('deployment_completion','implementation_capabilities','metadata_correction'):
        metadata.pop(key,None)
    metadata.update(version='serve-client-governance-20261105',binary_sha256=plan['candidate_sha256'],
        source_commit=plan['source_commit'],tool_source_commit=plan['source_commit'],
        source_note='Optional fixed-window per-client RPM/TPM reservation and settlement; prepared slot round-robin admission with bounded per-client waiting shares.',
        tested_source_manifest=str(PACKAGE/'candidate/runtime-source.json'),
        tested_source_manifest_sha256=cpu.sha(PACKAGE/'candidate/runtime-source.json'),
        build_cache=str(PACKAGE/'candidate/CMakeCache.txt'),build_cache_sha256=cpu.sha(PACKAGE/'candidate/CMakeCache.txt'),
        acceptance='ACCEPT_BOUNDED_CLIENT_GOVERNANCE',acceptance_decision=str(result),acceptance_decision_sha256=cpu.sha(result),
        tool_acceptance=str(result),tool_acceptance_sha256=cpu.sha(result),inherited_acceptance=inherited,
        previous_binary=str(PACKAGE/'previous/q4t'),previous_release=str(PACKAGE/'previous/q4t.release.json'),
        observed_configuration=cpu.read(OUTPUT/'business/scoped-model-config-wire.bin'),
        operations_rules=h.identity(PACKAGE/'candidate/q4t.rules.yml'),rollback_operations_rules=h.identity(PACKAGE/'previous/q4t.rules.yml'),
        scope=dict(hardware='local Thor',production='default MTP k3, full head, text',
            validation='Two local model starts: real TLS quota refusals, logical ledger, active/queued cancellation, client slot order, window rollover; two project tool/citation loops and no-policy JSON/SSE recovery',
            installation_health='identical accepted ELF, installed CLI; HTTP reused',
            boundaries='Single-process fixed windows and prepared-slot fairness only; no hard realtime, preparation/GPU-time fairness, distributed billing or preemption. No new managed/real LAN/long soak/physical RAM/numerical or performance acceptance; old packages and tasks pinned.',
            performance_acceptance=False,numerical_evidence=str(PACKAGE/'numerical-reuse.json')))
    folder=OUTPUT/'release';folder.mkdir();declarations=folder/'declarations';declarations.mkdir()
    for name,label in (('capabilities.json','capabilities-wire.bin'),
                       ('configuration.json','scoped-model-config-wire.bin')):
        h.copy_checked(h.identity(OUTPUT/'business'/label),declarations/name)
    shutil.copytree(PACKAGE/'candidate/schemas',declarations/'schemas')
    caps=cpu.read(declarations/'capabilities.json');protocol=metadata['chat_protocol']
    protocol['protocol_sha256']=plan['protocol']
    protocol['client_governance']=copy.deepcopy(caps['client_governance'])
    for key in caps.keys() & protocol.keys():protocol[key]=copy.deepcopy(caps[key])
    for name in pkg.DECLARATION_SCHEMAS:
        path=declarations/'schemas'/(name+'.json')
        protocol[name+'_schema']=str(path);protocol[name+'_schema_sha256']=cpu.sha(path)
    protocol['declaration_evidence']='Actual governance capabilities/config and frozen schemas; new protocol identity requires separate explicit task compatibility review.'
    metadata['release_declarations']=dict(profile=pkg.DECLARATION_PROFILE,directory=str(declarations),
        binary_sha256=plan['candidate_sha256'],protocol_sha256=plan['protocol'],
        files={name:cpu.sha(declarations/name) for name in pkg.DECLARATION_FILES})
    pkg.verify_declarations(metadata,required=True)
    cpu.save(folder/'q4t.release.json',metadata)
    cpu.save(folder/'binding.json',dict(old=plan['old_hashes'],new=dict(q4t=plan['candidate_sha256'],
        **{'q4t.release.json':cpu.sha(folder/'q4t.release.json')})))
    h.seal(folder);return folder



def bind():
    a.BASE, a.PACKAGE, a.OUTPUT = BASE, PACKAGE, OUTPUT
    a.MARKER = ROOT/'build/serve-client-governance-recovery.json'
    a.check, a.verifier, a.release = check, verifier, release


def execute():
    bind()
    h.require_guard(OUTPUT)
    cpu.save(OUTPUT/'begin.json', dict(started=time.time(), limits=LIMITS))
    def expired(*_):
        raise RuntimeError('fixed GPU window expired; cleanup reserve only')
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, LIMITS['seconds']-45)
    try:
        a.execute()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


def finalize():
    bind()
    a.finalize()
    h.seal(OUTPUT)


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'execute', 'finalize', 'check'))
    print(json.dumps(globals()[parser.parse_args().action]() or dict(passed=True)))
