"""One owned model: cache bytes/layouts, storage-fault fallback and progress."""
import argparse
import copy
import http.client
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import uuid

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from jsonschema import Draft202012Validator
from tools.serve_schema_v2 import acceptance as base
from tools.serve_cache_io.progress_capture import Capture
from tools.serve_recovery import progress
from tools.serve_service.package import require, read, save, sha

LIMITS = dict(starts=1, chats=8, controls=2000, seconds=600)
guard = base.lifecycle.guard


def requests():
    source = base.requests()
    mtp = copy.deepcopy(source['greedy'])
    mtp.update(session_id='e'*64)
    mtp['messages'].insert(0, copy.deepcopy(source['features']['messages'][0]))
    return dict(cold=source['features'], shared=source['warm'], mtp=mtp)


def follow(label, previous, answer):
    body = base.tagged(label, previous)
    body.pop('prefix_cache_scope', None)
    body.pop('prefix_cache_write_tokens', None)
    body['messages'] += [dict(role='assistant', content=answer.message['content']),
        dict(role='user', content=
        'Return the same JSON object again, with node.children empty and note null.'
        if 'response_format' in body else 'Compute 17 + 25 again. Reply only with 42.')]
    return body


def select_worker(records, parent, executable, directory):
    """Resolve only a direct, live child holding this exact cache directory."""
    expected = Path(executable).stat()
    owned = Path(directory).stat()
    matches = []
    for pid, row in records.items():
        if row.get('ppid') != parent or row.get('state') in ('Z', 'X') or \
                '--q4t-cache-worker' not in row.get('argv', []):
            continue
        try:
            birth = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
            if int(birth[19]) != row.get('start_ticks') or int(birth[1]) != parent:
                continue
            mapped = Path(f'/proc/{pid}/exe').stat()
            if (mapped.st_dev, mapped.st_ino) != (expected.st_dev, expected.st_ino):
                continue
            for fd in Path(f'/proc/{pid}/fd').iterdir():
                item = fd.stat()
                if (item.st_dev, item.st_ino) == (owned.st_dev, owned.st_ino):
                    matches.append((pid, row))
                    break
        except (FileNotFoundError, ProcessLookupError):
            continue
    require(len(matches) == 1, 'need one exact owned cache worker')
    return matches[0]


def restore(log, request_id):
    # The original helper rightly rejects failures; this stage explicitly
    # checks the failed restore and fallback instead of relabelling it a hit.
    import re
    rows = re.findall(r'\[session-cache\] request=' + re.escape(request_id) +
        r' event=restore prefix=(\d+) ms=[^ ]+ error=([^\n]*?) source=(\w+) layout=(\w+)', log)
    require(len(rows) == 1, 'one restore observation required: ' + request_id)
    prefix, error, source, layout = rows[0]
    return dict(prefix=int(prefix), error=error, source=source, layout=layout)


class Stage(base.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = 'PASS_CACHE_IO_AND_RESOURCE_PROGRESS_BUSINESS'

    def __init__(self, plan):
        super().__init__(plan)
        self.capture = None
        self.config_validator = Draft202012Validator(
            read(ROOT/'docs/serve-config-v1.schema.json'))
        self.business_verdict = {}

    def config(self, label):
        status, raw = self.http('/v1/config', label=label)
        require(status == 200, 'config unavailable')
        value = json.loads(raw)
        self.config_validator.validate(value)
        require(all(value['cache_io'][name]['configured'] and
                    not value['cache_io'][name]['readiness_dependency']
                    for name in ('session', 'prefix')), 'cache configuration missing')
        return value

    def start(self):
        for port in (self.plan['port'], self.plan['operations_port']):
            with socket.socket() as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(('127.0.0.1', port))
        for name in ('private-cache', 'shared-cache'):
            (self.out/name).mkdir(mode=0o700)
        args = [self.plan['binary'], 'serve', '--model-dir', self.plan['model'],
            '--host', '127.0.0.1', '--port', str(self.plan['port']),
            '--operations-port', str(self.plan['operations_port']), '--mtp',
            '--max-seq', '1', '--max-len', '16384', '--seq-capacities', '16384',
            '--max-prefill', '2048', '--max-tokens', '384',
            '--max-inflight-requests', '8', '--max-queued-requests', '2',
            '--session-cache-dir', str(self.out/'private-cache'),
            '--prefix-cache-dir', str(self.out/'shared-cache'),
            '--prefix-cache-checkpoints', '1024']
        parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        env = {k: v for k, v in os.environ.items() if not k.startswith('Q4T_PROGRESS_')}
        env.update(Q4T_PROGRESS_FD=str(child.fileno()),
            Q4T_PROGRESS_PARENT_START=str(guard.processes()[os.getpid()]['start_ticks']),
            Q4T_CONTROLLER_GENERATION='1', Q4T_MODEL_GENERATION='1',
            Q4T_SERVICE_ID=self.plan['service_id'], Q4T_SESSION_INCREMENTAL='1')
        save(self.out/'model-command.json', args)
        self.budget('starts')
        try:
            with (self.out/'model.log').open('xb', buffering=0) as log:
                self.model = subprocess.Popen(args, cwd=ROOT, stdout=log,
                    stderr=subprocess.STDOUT, env=env, pass_fds=(self.lease, child.fileno()))
            self.model_identity = guard.processes()[self.model.pid]
            self.history[self.model.pid] = self.model_identity
            owner = dict(pid=self.model.pid, **self.model_identity,
                boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip())
            save(self.out/'model-identity.json', owner)
            self.capture = Capture(parent, progress.Identity.owned(
                owner, self.plan['service_id'], 1, 1))
        except BaseException:
            parent.close()
            raise
        finally:
            child.close()
        end = time.monotonic() + 180
        while time.monotonic() < end:
            self.same_model()
            try:
                if self.http('/readyz', timeout=1)[0] == 200:
                    break
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(.1)
        else:
            raise TimeoutError('model readiness')
        status, raw = self.http('/v1/capabilities', label='initial-capabilities')
        require(status == 200, 'capabilities unavailable')
        caps = json.loads(raw)
        save(self.out/'capabilities.json', caps)
        self.client.protocol_sha256 = caps['protocol_source_sha256']
        save(self.out/'configuration.json', self.config('initial-config'))
        require(self.capture.summary()['snapshots'] > 0, 'native progress unavailable')

    def observation(self, body):
        return restore((self.out/'model.log').read_text(), body['request_id'])

    def business(self):
        rows = self.plan['requests']
        cold = self.sdk_request('cold', rows['cold'])
        base.check_features(cold)
        require(8192 <= cold.usage['prompt_tokens'] <= 14500, 'private cache token bound')
        shared = self.sdk_request('shared', rows['shared'])
        base.check_features(shared)
        require(cold.message == shared.message and cold.logprobs == shared.logprobs,
                'shared restore changed exact generated token probabilities')
        require(self.observation(rows['shared']) == dict(prefix=1024, error='none',
            source='shared', layout='target'), 'shared flat cache missed')
        private_body = follow('private', rows['cold'], shared)
        private = self.sdk_request('private', private_body)
        base.check_features(private)
        private_record = self.observation(private_body)
        require(private_record['prefix'] >= 8192 and private_record['error'] == 'none'
            and private_record['source'] == 'session' and private_record['layout'] == 'target',
            'private incremental target cache missed')
        before = self.config('before-fault-config')['cache_io']
        pid, owner = select_worker(guard.processes(), self.model.pid,
            self.plan['binary'], self.out/'private-cache')
        self.history[pid] = owner
        save(self.out/'fault-worker.json', dict(pid=pid, **owner))
        guard.signal_identity(pid, owner, signal.SIGSTOP)
        fault_body = follow('fault-fallback', private_body, private)
        fault = self.sdk_request('fault-fallback', fault_body)
        base.check_features(fault)
        record = self.observation(fault_body)
        require(record['error'] in ('cache storage unavailable',
                'cache storage time limit exceeded') and record['prefix'] == 0 and
                record['source'] == 'session' and record['layout'] == 'target',
                'fault failed to exercise private restore and reset/fallback')
        self.same_model()
        require(guard.processes().get(pid, {}).get('start_ticks') != owner['start_ticks'],
                'stopped cache worker not reaped by native owner')
        after = self.config('after-fault-config')['cache_io']
        require(after['prefix']['available'] and
                after['prefix']['generation'] == before['prefix']['generation'],
                'private failure disturbed independent prefix worker')
        recovered_body = follow('recovered', fault_body, fault)
        recovered = self.sdk_request('recovered', recovered_body)
        base.check_features(recovered)
        recovered_record = self.observation(recovered_body)
        if recovered_record['prefix'] == 0:
            # Recovery backoff can postpone the fault request's new Save.
            # This is a new turn, not retry/replay; its budget is pre-reserved.
            recovered_body = follow('recovered-confirm', recovered_body, recovered)
            recovered = self.sdk_request('recovered-confirm', recovered_body)
            base.check_features(recovered)
            recovered_record = self.observation(recovered_body)
        require(recovered_record['prefix'] >= 8192 and
                recovered_record['error'] == 'none', 'new cache generation did not restore')
        latest = self.config('recovered-config')['cache_io']
        require(latest['session']['generation'] != before['session']['generation'] and
                latest['session']['workers_started'] == 2 and
                latest['session']['workers_reaped'] == 1, 'cache restart budget or lineage differs')
        mtp = self.sdk_request('mtp-cold', rows['mtp'])
        require(mtp.finish_reason == 'stop' and mtp.message['content'].strip() == '42',
                'MTP plain response differs')
        mtp_body = follow('mtp-warm', rows['mtp'], mtp)
        mtp_body.update(stream=True, stream_options=dict(include_usage=True))
        mtp_warm = self.sdk_request('mtp-warm', mtp_body)
        require(mtp_warm.finish_reason == 'stop' and mtp_warm.message['content'].strip() == '42',
                'MTP cache restored response differs')
        mtp_record = self.observation(mtp_body)
        require(mtp_record['prefix'] >= 8192 and mtp_record['layout'] == 'mtp' and
                mtp_record['error'] == 'none', 'MTP cache did not restore')
        log = (self.out/'model.log').read_text()
        require('[mtp-accept] request=' + mtp_body['request_id'] in log,
                'default MTP did not execute')
        final = self.drained('final')
        require(final['q4t_requests_total'] == self.counts['chats'] and
                final['q4t_requests_success_total'] == self.counts['chats'] and
                final['q4t_requests_error_total'] == 0, 'cache fault leaked into request failure/replay')
        self.config('final-config')
        time.sleep(.3)
        observed = self.capture.summary()
        require(not observed['error'] and observed['cache_wait_samples'] >= 2 and
                observed['last']['successes'] == self.counts['chats'],
                'actual completion/wait observations missing or false stall')
        self.business_verdict = dict(requests=self.counts['chats'], same_model=True,
            cache_fault=record, recovered=recovered_record, mtp_restore=mtp_record,
            cache_before=before, cache_after=latest, progress=observed,
            progress_sampling='250ms snapshots can miss short device/resource scopes; '
                'their direct CPU contracts are reused, not asserted as fully sampled.',
            real_tool_closure='Reuse R03 business-02; tool/history/template code unchanged.')
        save(self.out/'business-verdict.json', self.business_verdict)

    def cleanup(self):
        signal.setitimer(signal.ITIMER_REAL, 0)
        try:
            super().cleanup()
        finally:
            if self.capture is not None:
                self.capture.close(self.out/'progress.bin')
                save(self.out/'progress-summary.json', self.capture.summary())


def prepare(args):
    base.prepare(args, rows=requests(), limits=LIMITS, scope=__doc__)
    plan_path = args.output.resolve()/'plan.json'
    plan = read(plan_path)
    extra = subprocess.check_output(['git', 'ls-files', 'tools/serve_cache_io',
        'docs/serve-config-v1.schema.json', 'docs/SERVE_CACHE_IO_ISOLATION_2026-11-09.md'],
        cwd=ROOT, text=True).splitlines()
    plan['sources'].update({n: sha(ROOT/n) for n in extra})
    plan.update(service_id=str(uuid.uuid4()), validation_only=True,
        gpu_probe_starts=0, recovery_model_restarts=0,
        mutation='SIGSTOP one verified own private-cache worker; native timeout/reap/recovery',
        scope='shared flat, private incremental target and MTP; cache failure and recovery; '
              'native progress/resource observations. No full performance/quality matrix.')
    save(plan_path, plan)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='mode', required=True)
    p = sub.add_parser('prepare')
    p.add_argument('--binary', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--port', type=int, default=18771)
    p = sub.add_parser('run')
    p.add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    if args.mode == 'prepare':
        prepare(args)
    else:
        def deadline(_sig, _frame):
            raise TimeoutError('cache business bounded deadline')
        signal.signal(signal.SIGALRM, deadline)
        signal.setitimer(signal.ITIMER_REAL, LIMITS['seconds'] - 40)
        with base.lifecycle.child_subreaper():
            Stage(read(args.plan)).run()


if __name__ == '__main__':
    main()
