"""Bounded real systemd/MTP acceptance of the pinned managed service.

The actual service controller runs gpu_exclusive.run_guard for the entire GPU
phase. Clients live outside its cgroup; no second model lease is acquired and
no model is launched after stopping that controller.
"""
import argparse
import copy
from dataclasses import asdict
import fcntl
import http.client
import io
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path[:0] = [str(ROOT), str(ROOT/'tools/evalscope'), str(ROOT/'tools/examples')]
from tools.serve_service import package as pkg, service, supervisor
from tools.serve_ops import contracts
from chat_client import (append_tool_results, make_request, prepare_tool_calls,
                         read_json, read_sse)
from project_query import SYSTEM, validate_answer
from project_records import ProjectRecords, TOOLS, VALIDATORS
import gpu_exclusive as guard
import nvml_process_query

BASE = ROOT/'.q4t-work/serve-managed-service-20261103'
PLAN = BASE/'gpu-plan.json'
OUT = BASE/'execution'
LIMITS = dict(starts=3, generations=8, negatives=8, observations=240, seconds=1200)
# Each supervisor probe has <=4 HTTP GETs and is followed by a >=5s wait.
# Reserve 4*(floor(220/5)+3)=188, including three possible initial probes.
# Driver controls/cleanup reserve 48, for a conservative total <=236.
GPU_WINDOW = 220
CONTROL_LIMIT = 48
SOURCES = (*pkg.SOURCES, 'tools/serve_service/acceptance.py',
    'tools/serve_service/test_acceptance.py', 'tools/examples/chat_client.py',
    'tools/examples/project_query.py', 'tools/examples/project_records.py',
    'tools/examples/agent_tools_demo.py', 'tools/examples/tool_recovery.py',
    'docs/MODEL.md', 'docs/SERVE_MANAGED_SERVICE_GPU_2026-11-03.md')
PROPERTIES = ('LoadState', 'ActiveState', 'SubState', 'MainPID', 'ControlGroup',
    'FragmentPath', 'Description', 'ExecMainCode', 'ExecMainStatus', 'Result',
    'Restart', 'NRestarts', 'KillMode', 'UnitFileState')
EMPTY = ('q4t_active_chats', 'q4t_queued_requests', 'q4t_request_body_bytes',
    'q4t_structured_active', 'q4t_structured_preparing',
    'q4t_structured_vocab_bytes', 'q4t_structured_workers_active')


def require_idle(path):
    value = nvml_process_query.query(timeout=5)
    pkg.save(path, value)
    pkg.require(value['status'] == 'OK' and not value['pids'],
                'ENV_ABORT: GPU observation failed or occupied')


def lock_available(path):
    with Path(path).open('r+') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True


def prepare():
    pkg.require(not PLAN.exists() and not OUT.exists(), 'preserve prior phase')
    pkg.require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT),
                'commit the reviewed driver before freezing')
    cpu = pkg.read(BASE/'cpu/result.json')
    driver_cpu = pkg.read(BASE/'driver-cpu/result.json')
    pkg.require(driver_cpu['passed'] and driver_cpu['tests_run'] == 8,
                'driver CPU checks incomplete')
    for name, digest in driver_cpu['sources'].items():
        pkg.require(pkg.sha(ROOT/name) == digest, 'driver CPU source changed')
    receipt = cpu['package']
    manifest = pkg.verify(receipt['package'], receipt['manifest_sha256'])
    for name, digest in cpu['source_sha256'].items():
        pkg.require(pkg.sha(ROOT/name) == digest, 'CPU dependency changed: '+name)
    release = pkg.read(ROOT/'build/q4t.release.json')
    sources = pkg.read(Path(release['tested_source_manifest']))
    for name, digest in sources.items():
        pkg.require(pkg.sha(ROOT/name) == digest, 'runtime source changed')
    pkg.require(pkg.sha(ROOT/'build/q4t') == cpu['default_binary_sha256'] and
                pkg.sha(ROOT/'build/q4t.release.json') == cpu['default_release_sha256'],
                'default pair changed')
    token = secrets.token_hex(10)
    pkg.save(PLAN, dict(version=1, limits=LIMITS, gpu_window_seconds=GPU_WINDOW,
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
        source_files={name: pkg.sha(ROOT/name) for name in SOURCES},
        cpu_result_sha256=pkg.sha(BASE/'cpu/result.json'), package=receipt,
        driver_cpu_result_sha256=pkg.sha(BASE/'driver-cpu/result.json'),
        binary_sha256=manifest['files']['q4t'], protocol_sha256=manifest['protocol_sha256'],
        default_release_sha256=cpu['default_release_sha256'],
        unit='q4t-managed-srv14-'+token+'.service', token=token,
        planned=dict(starts=2, generations=8, negative_scenarios=6),
        supervisor_http_reservation=188, explicit_control_limit=CONTROL_LIMIT,
        performance_acceptance=False, operator_notifications=False))


def check():
    plan = pkg.read(PLAN)
    pkg.require(plan['limits'] == LIMITS and plan['gpu_window_seconds'] == GPU_WINDOW,
                'phase budget changed')
    for name, digest in plan['source_files'].items():
        pkg.require(pkg.sha(ROOT/name) == digest, 'phase source changed: '+name)
    pkg.require(pkg.sha(BASE/'cpu/result.json') == plan['cpu_result_sha256'], 'CPU receipt changed')
    pkg.require(pkg.sha(BASE/'driver-cpu/result.json') == plan['driver_cpu_result_sha256'],
                'driver CPU receipt changed')
    pkg.verify(plan['package']['package'], plan['package']['manifest_sha256'])
    pkg.require(pkg.sha(ROOT/'build/q4t') == plan['binary_sha256'] and
                pkg.sha(ROOT/'build/q4t.release.json') == plan['default_release_sha256'],
                'installed release changed')
    return plan


def ordinary(stream=False):
    return make_request([dict(role='user', content='只回复数字710003。')], [],
        stream=stream, max_tokens=128, timeout_ms=180000)


def strict_json():
    body = ordinary()
    body['messages'][0]['content'] = '仅输出符合schema的JSON。'
    body['response_format'] = dict(type='json_schema', json_schema=dict(
        name='admission', strict=True, schema=dict(type='object',
            properties=dict(ok=dict(type='boolean', enum=[True])),
            required=['ok'], additionalProperties=False)))
    return body


def healthy_idle(metrics, requests):
    pkg.require(all(metrics[name] == 0 for name in EMPTY), 'request owners retained')
    pkg.require(metrics['q4t_seq_slots_free'] == metrics['q4t_seq_slots_total'] == 1,
                'sequence slot not returned')
    pkg.require(metrics['q4t_structured_workers_started_total'] ==
                metrics['q4t_structured_workers_reaped_total'], 'grammar worker not reaped')
    pkg.require(metrics['q4t_requests_total'] ==
                metrics['q4t_requests_success_total'] == requests,
                'requests failed, duplicated or automatically replayed')


class Run:
    def __init__(self, plan):
        self.plan = plan
        self.run_dir = OUT/'managed-run'
        self.package = Path(plan['package']['package'])
        self.digest = plan['package']['manifest_sha256']
        self.manifest = pkg.verify(self.package, self.digest)
        self.port = self.manifest['settings']['port']
        self.started, self.gpu_started = time.monotonic(), None
        self.counts = dict(generations=0, negatives=0, controls=0)
        self.unit_file = OUT/plan['unit']
        self.runtime_link = Path(f'/run/user/{os.getuid()}/systemd/user')/plan['unit']
        self.unit_owned, self.link_owned = False, False
        self.last_status = None
        self.controller = None
        self.lock_stat = None
        self.owners = []
        self.cleaning = False
        self.gpu_finished = None

    def budget(self, kind=None):
        pkg.require(time.monotonic()-self.started < LIMITS['seconds'], 'phase deadline')
        if kind:
            limit = CONTROL_LIMIT-8 if kind == 'controls' else LIMITS[kind]
            pkg.require(self.counts[kind] < limit, 'phase budget: '+kind)
            self.counts[kind] += 1
            pkg.save(OUT/'counts.json', self.counts)

    def command(self, label, argv, *, expected=0, timeout=20):
        if self.cleaning:
            # Reserved cleanup must still run if a business budget/deadline
            # failed. An overrun fails the phase after owned cleanup completes.
            self.counts['controls'] += 1
            pkg.save(OUT/'counts.json', self.counts)
        else:
            self.budget('controls')
        result = subprocess.run(list(map(str, argv)), capture_output=True, text=True,
                                timeout=timeout, cwd=ROOT)
        pkg.require(len(result.stdout)+len(result.stderr) <= 512*1024, 'command output bound')
        pkg.save(OUT/(label+'.json'), dict(argv=list(map(str, argv)),
            returncode=result.returncode, stdout=result.stdout, stderr=result.stderr))
        if expected is not None:
            pkg.require(result.returncode == expected, 'command failed: '+label)
        return result

    def properties(self, label):
        result = self.command(label, ['systemctl', '--user', 'show', self.plan['unit'],
            *('--property='+name for name in PROPERTIES)])
        return dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)

    def owned_properties(self, label):
        props = self.properties(label)
        pkg.require(props['Description'] == 'q4t-srv14-'+self.plan['token'] and
                    Path(props['FragmentPath']).resolve() == self.unit_file,
                    'unit ownership changed')
        return props

    def observe_status(self):
        self.budget()
        if (self.run_dir/'terminal.json').exists():
            raise RuntimeError('managed service terminated before scenario completed')
        path = self.run_dir/'status.json'
        if not path.exists():
            return None
        value = pkg.read(path)
        pkg.require(value['attempts'] <= LIMITS['starts'], 'model start budget')
        if value != self.last_status:
            with (OUT/'status-snapshots.jsonl').open('a') as stream:
                stream.write(json.dumps(dict(observed_monotonic=time.monotonic(), status=value))+'\n')
            self.last_status = value
        pkg.require(value['state'] != 'FAILED', 'managed worker failed: '+str(value.get('error')))
        return value

    def wait_state(self, state, attempts, *, timeout=180):
        until = time.monotonic()+timeout
        while time.monotonic() < until:
            value = self.observe_status()
            if value and value['state'] == state and value['attempts'] == attempts:
                return value
            time.sleep(.05)
        raise RuntimeError('managed state timeout: '+state)

    def service_cli(self, label, action, *, output=None, digest=None, expected=0):
        argv = [self.manifest['python']['path'], '-I', '-B',
                self.package/'source/tools/serve_service/service.py', action]
        if action == 'run':
            argv += ['--package', self.package, '--manifest-sha256', digest or self.digest,
                     '--output', output or self.run_dir]
        else:
            argv += ['--run', self.run_dir]
        return self.command(label, argv, expected=expected)

    def bind(self, state, label):
        control = pkg.read(self.run_dir/'controller.json')
        if self.controller is None:
            self.controller = control
        pkg.require(control == self.controller, 'controller changed within stage')
        lock = Path(control['lock_path'])
        st = lock.stat()
        lock_id = [st.st_dev, st.st_ino]
        if self.lock_stat is None:
            self.lock_stat = lock_id
        pkg.require(lock_id == self.lock_stat and not lock_available(lock), 'lease released/replaced')
        lease = pkg.read(lock)
        pkg.require(lease['pid'] == control['owner']['pid'] and lease['state'] == 'held'
                    and lease['output'] == str(self.run_dir/'lease'), 'wrong lease holder')
        props = self.owned_properties(label+'-unit')
        pkg.require(props['Restart'] == 'no' and props['NRestarts'] == '0' and
                    props['KillMode'] == 'control-group' and
                    int(props['MainPID']) == control['owner']['pid'], 'unit policy/owner mismatch')
        owned = [control['owner'], state['owner'], state['backend']]
        descriptors = {}
        for owner in owned:
            pid = owner['pid']
            pkg.require(supervisor.process_identity(pid) == owner, 'owned process changed')
            pkg.require('0::'+props['ControlGroup'] in Path(f'/proc/{pid}/cgroup').read_text().splitlines(),
                        'process outside managed cgroup')
            matches = []
            for entry in Path(f'/proc/{pid}/fd').iterdir():
                try:
                    stat = entry.stat()
                except FileNotFoundError:
                    continue
                if [stat.st_dev, stat.st_ino] == lock_id:
                    matches.append(entry.name)
            pkg.require(matches, 'owned process did not retain inherited lease')
            descriptors[str(pid)] = matches
            if owner not in self.owners:
                self.owners.append(owner)
        pkg.save(OUT/(label+'-ownership.json'), dict(control=control, owners=owned,
            lock_identity=lock_id, descriptor_numbers=descriptors, unit=props))

    def launch(self):
        before = self.properties('unit-before')
        pkg.require(before['LoadState'] == 'not-found' and
                    not os.path.lexists(self.runtime_link), 'unit name already occupied')
        text = service.unit_text(self.package, self.digest, self.run_dir)
        text = text.replace('Description=Qwen4 Thor explicit managed serving generation',
                            'Description=q4t-srv14-'+self.plan['token'])
        self.unit_file.write_text(text)
        self.command('unit-verify', ['systemd-analyze', '--user', 'verify', self.unit_file])
        self.command('unit-link', ['systemctl', '--user', 'link', '--runtime', self.unit_file])
        pkg.require(self.runtime_link.is_symlink() and self.runtime_link.resolve() == self.unit_file,
                    'runtime unit link differs')
        self.link_owned = True
        self.owned_properties('unit-linked')
        self.unit_owned = True
        self.gpu_started = time.monotonic()
        signal.setitimer(signal.ITIMER_REAL, GPU_WINDOW)
        # This actual unit runs the pinned production controller/guard, worker,
        # exec helper and backend. It is not an acceptance-specific substitute.
        self.command('unit-start', ['systemctl', '--user', 'start', self.plan['unit']])
        state = self.wait_state('READY', 1)
        self.bind(state, 'first-ready')

    def complete(self, label, body):
        self.budget('generations')
        pkg.save(OUT/(label+'-request.json'), body)
        started = time.monotonic()
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=180)
        raw, receipt = bytearray(), dict(eof=False)
        try:
            conn.request('POST', '/v1/chat/completions', json.dumps(body),
                         {'Content-Type': 'application/json', 'X-Request-Id': body['request_id']})
            response = conn.getresponse()
            receipt.update(status=response.status, headers=dict(response.getheaders()))
            while part := response.read(65536):
                raw.extend(part)
                pkg.require(len(raw) <= 8*1024**2, 'wire exceeds budget')
            receipt['eof'] = True
            pkg.require(response.status == 200, 'generation HTTP failure')
            expected = 'text/event-stream' if body['stream'] else 'application/json'
            pkg.require(response.headers.get_content_type() == expected, 'wrong response encoding')
            value = (read_sse if body['stream'] else read_json)(io.BytesIO(raw))
            pkg.require(value.id == body['request_id'] and value.model == body['model'],
                        'response identity differs')
            pkg.save(OUT/(label+'-response.json'), asdict(value))
            return value
        finally:
            receipt['seconds'] = time.monotonic()-started
            (OUT/(label+'-wire.bin')).write_bytes(raw)
            pkg.save(OUT/(label+'-http.json'), receipt)
            conn.close()

    def plain(self, label, stream=False):
        result = self.complete(label, ordinary(stream))
        pkg.require(result.finish_reason == 'stop' and
                    result.message['content'].strip() == '710003', 'ordinary quality failure')

    def tools(self, label):
        tools = [copy.deepcopy(TOOLS[0])]
        tools[0]['function']['strict'] = True
        tools[0]['function']['parameters']['properties']['path'] = dict(
            type='string', enum=['docs/MODEL.md'])
        history = [dict(role='system', content=SYSTEM), dict(role='user', content=
            '先调用一次read_project_record读取docs/MODEL.md的第1到24行，start_line=1、line_count=24。'
            '再回答architectures字段的准确值，并附工具给出的引用。')]
        body = make_request(history, tools, stream=True, max_tokens=512, timeout_ms=180000)
        body.update(tool_choice='required', parallel_tool_calls=False)
        completion = self.complete(label+'-read', body)
        planned = prepare_tool_calls(completion, VALIDATORS)
        pkg.require(len(planned) == 1 and planned[0].arguments ==
            dict(path='docs/MODEL.md', start_line=1, line_count=24), 'unexpected tool batch')
        call = planned[0]
        pkg.save(OUT/(label+'-tool-started.json'), asdict(call))
        value = ProjectRecords(ROOT).execute(call)
        pkg.require(value['ok'], 'project read failed')
        value['application_budget'] = dict(remaining_model_requests=1,
            remaining_tool_calls=0, final_response_required=True)
        content = json.dumps(value, ensure_ascii=False)
        pkg.save(OUT/(label+'-tool-result.json'), dict(tool_call_id=call.call_id, content=content))
        history = append_tool_results(history, completion, {call.call_id: content})
        body = make_request(history, tools, stream=True, max_tokens=512, timeout_ms=180000)
        body['tool_choice'] = 'none'
        answer = self.complete(label+'-answer', body)
        validate_answer(answer.message['content'], {value['source']['citation']}, 1)
        pkg.require(answer.finish_reason == 'stop' and
                    'Qwen4ExpForConditionalGeneration' in answer.message['content'],
                    'project answer quality failure')

    def drain(self, label, expected):
        self.budget('controls')
        response = supervisor.transport.get(self.port, '/metrics')
        pkg.require(response['http_status'] == 200, 'metrics unavailable')
        metrics, _ = contracts.parse_metrics(response['body'], version=4)
        healthy_idle(metrics, expected)
        pkg.save(OUT/(label+'-metrics.json'), dict(response=response, metrics=metrics))
        return metrics

    def kill_backend(self, state, label):
        self.budget('negatives')
        owner = state['backend']
        pkg.require(supervisor.signal_owner(owner, signal.SIGKILL), 'owned backend absent')
        pkg.save(OUT/(label+'-kill.json'), dict(owner=owner, signal='SIGKILL', idle=True,
            unknown_requests_replayed=False, monotonic=time.monotonic()))

    def negatives(self):
        self.budget('negatives')
        result = self.command('lease-contention', [self.manifest['python']['path'], '-B',
            ROOT/'tools/evalscope/gpu_exclusive.py', '--output', OUT/'rejected-lease',
            '--', '/usr/bin/false'], expected=75)
        pkg.require(pkg.read(OUT/'rejected-lease/exit.json')['status'] == 'LEASE_BUSY',
                    'second lease not rejected before child')
        self.budget('negatives')
        self.service_cli('duplicate-runtime', 'run', expected=1)
        self.budget('negatives')
        self.service_cli('changed-manifest', 'run', output=OUT/'must-not-exist',
                         digest='0'*64, expected=1)
        pkg.require(not (OUT/'must-not-exist').exists(), 'bad package created runtime')

    def stop(self):
        self.service_cli('explicit-stop', 'stop')
        # No more model/HTTP actions after the single controller exits.
        until = time.monotonic()+45
        while not (self.run_dir/'terminal.json').exists() and time.monotonic() < until:
            self.budget()
            time.sleep(.05)
        pkg.require((self.run_dir/'terminal.json').exists(), 'stop did not finalize')
        signal.setitimer(signal.ITIMER_REAL, 0)
        self.gpu_finished = time.monotonic()
        pkg.require(self.gpu_finished-self.gpu_started <= GPU_WINDOW,
                    'supervision reservation window exceeded')
        terminal = pkg.read(self.run_dir/'terminal.json')
        pkg.require(terminal['state'] == 'STOPPED' and terminal['cleanup_confirmed'] and
                    terminal['guard']['status'] == 'COMPLETED' and
                    terminal['worker']['attempts'] == 2 and
                    terminal['worker']['restarts'] == 2, 'stop/restart accounting failure')
        self.budget('negatives')
        again = self.service_cli('idempotent-stop', 'stop')
        pkg.require(json.loads(again.stdout) == terminal, 'terminal stop changed record')
        time.sleep(10)  # Two configured backend backoffs; actual unit stays down.
        props = self.owned_properties('unit-stopped')
        pkg.require(props['ActiveState'] == 'inactive' and props['MainPID'] == '0' and
                    props['NRestarts'] == '0' and props['Result'] == 'success',
                    'systemd did not remain stopped')
        pkg.require(len(list(self.run_dir.glob('attempt-*.json'))) == 2,
                    'backend reactivated after stop')
        pkg.require(lock_available(Path(self.controller['lock_path'])), 'lease retained after stop')
        for owner in self.owners:
            try:
                pkg.require(supervisor.process_identity(owner['pid']) != owner,
                            'owned process still exists')
            except (FileNotFoundError, ProcessLookupError):
                pass
        with socket.socket() as client:
            pkg.require(client.connect_ex(('127.0.0.1', self.port)) != 0, 'listener remained')
        require_idle(OUT/'gpu-after-stop.json')

    def cleanup(self):
        signal.setitimer(signal.ITIMER_REAL, 0)
        self.cleaning = True
        if self.unit_owned:
            props = self.owned_properties('cleanup-unit-before')
            if props['ActiveState'] != 'inactive':
                self.command('cleanup-unit-stop', ['systemctl', '--user', 'stop', self.plan['unit']],
                             timeout=100)
            self.command('unit-journal', ['journalctl', '--user', '-u', self.plan['unit'],
                '--no-pager', '--lines=200', '--output=short-monotonic'])
            self.command('unit-reset', ['systemctl', '--user', 'reset-failed', self.plan['unit']],
                         expected=None)
        if self.link_owned:
            pkg.require(self.runtime_link.is_symlink() and self.runtime_link.resolve() == self.unit_file,
                        'unit link changed; do not remove another owner')
            self.runtime_link.unlink()
            self.command('unit-reload', ['systemctl', '--user', 'daemon-reload'])
            after = self.properties('unit-removed')
            pkg.require(after['LoadState'] == 'not-found' and after['MainPID'] == '0',
                        'temporary service not removed')
        pkg.require(self.counts['controls'] <= CONTROL_LIMIT, 'cleanup control budget exceeded')

    def execute(self):
        OUT.mkdir(mode=0o700)
        pkg.save(OUT/'begin.json', dict(monotonic=self.started, unix_seconds=time.time(), plan=self.plan))
        result = dict(status='FAILED', automatic_request_retry=False,
                      performance_acceptance=False, persistent_service_installed=False)
        def expired(*_):
            raise RuntimeError('bounded supervision/control window expired')
        previous = signal.signal(signal.SIGALRM, expired)
        try:
            require_idle(OUT/'gpu-before.json')
            self.launch()
            self.negatives()
            self.plain('ordinary-json')
            self.plain('ordinary-sse', True)
            structured = self.complete('strict-json', strict_json())
            pkg.require(json.loads(structured.message['content']) == {'ok': True} and
                        structured.finish_reason == 'stop', 'strict JSON failure')
            self.tools('first-project')
            self.drain('first-idle', 5)
            first = self.wait_state('READY', 1)
            self.kill_backend(first, 'first')
            second = self.wait_state('READY', 2)
            pkg.require(not supervisor.processes.group_members(first['backend']['pid']),
                        'old group survived replacement')
            self.bind(second, 'second-ready')
            self.plain('recovered-json')
            self.tools('second-project')
            self.drain('second-idle', 3)
            self.kill_backend(second, 'second')
            waiting = self.wait_state('RESTART_WAIT', 2, timeout=20)
            pkg.require(waiting['restarts'] == 2, 'wrong pending restart count')
            pkg.save(OUT/'stop-during-backoff.json', waiting)
            self.stop()
            result.update(status='PASS_MANAGED_SYSTEMD_MTP', starts=2,
                          successful_requests=8, real_tool_executions=2,
                          actual_backend_replacements=1, pending_restarts_cancelled=1)
        except BaseException as error:
            result['error'] = repr(error)
            raise
        finally:
            try:
                self.cleanup()
                require_idle(OUT/'gpu-final.json')
                pkg.require(lock_available(guard.common_lock()), 'shared lease not released')
                result['cleanup_confirmed'] = True
                pkg.require(time.monotonic()-self.started < LIMITS['seconds'],
                            'total phase wall clock exceeded')
            except BaseException as error:
                result.update(status='FAILED', cleanup_error=repr(error))
                raise
            finally:
                signal.signal(signal.SIGALRM, previous)
                result.update(counts=self.counts, elapsed_seconds=time.monotonic()-self.started,
                    supervision_http_upper_bound=188 if result['status'].startswith('PASS') else None,
                    full_observation_upper_bound=188+self.counts['controls']
                        if result['status'].startswith('PASS') else None,
                    gpu_window_elapsed=None if self.gpu_finished is None else
                        self.gpu_finished-self.gpu_started)
                pkg.save(OUT/'result.json', result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'check', 'execute'))
    args = parser.parse_args()
    if args.action == 'prepare':
        prepare()
    elif args.action == 'check':
        print(json.dumps(check()))
    else:
        Run(check()).execute()


if __name__ == '__main__':
    main()
