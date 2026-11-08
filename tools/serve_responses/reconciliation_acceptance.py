"""Foreground state recovery: 1 start/4 creates/600 seconds, one own worker stop."""
import argparse
import copy
import http.client
import json
from pathlib import Path
import signal
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_responses import background_acceptance as background
from tools.serve_service.package import read, require, save

foreground = background.foreground
base = background.base
LIMITS = dict(starts=1, chats=4, controls=2000, seconds=600)


def requests():
    original = foreground.requests()
    fault = background.requests()['crash_running']
    fault.update(background=False, stream=True, stream_options=dict(include_obfuscation=False))
    return dict(tool=original['tool'], follow=original['follow'], fault=fault, chat=original['chat'])


def own_object_worker(records, model_pid):
    found = [(pid, row) for pid, row in records.items() if row.get('ppid') == model_pid and
             '--q4t-object-worker' in row.get('argv', []) and row.get('start_ticks') and
             row.get('executable') and not row.get('metadata_missing')]
    require(len(found) == 1, 'exact own object worker unavailable or ambiguous')
    return found[0]


def check_fault(events):
    require(events and all(type(e.get('sequence_number')) is int and
            e['sequence_number'] == i for i, e in enumerate(events)), 'fault stream sequence gap')
    require(any(e['type'] == 'response.output_text.delta' for e in events), 'no real partial text')
    require([e['type'] for e in events].count('error') == 1 and events[-1]['type'] == 'error' and
            events[-1]['code'] == 'object_commit_unknown', 'missing honest unknown-commit terminal')
    require(not any(e['type'] in ('response.completed', 'response.incomplete', 'response.failed')
                    for e in events), 'unknown commit claimed terminal resource')


def prepare(args):
    background.prepare(args, rows=requests(), limits=LIMITS, scope=__doc__)
    path = args.output.resolve()/'plan.json'
    plan = read(path)
    plan['mutations'] = ['SIGSTOP one identity-verified object worker child of the owned model',
                         'engine performs bounded worker recovery; never signal or restart the model']
    plan['derived_requests'] = ['actual document function result and response/call IDs feed follow-up',
        'fault reads actual partial persisted output before stopping its worker',
        'raw fault SSE must retain contiguous original events and honest unknown-commit error',
        'same-PID Chat, foreground reconciliation/worker cleanup, defaults and strict lease checked']
    save(path, plan)


class Stage(background.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = 'PASS_RESPONSES_FOREGROUND_RECOVERY_BUSINESS'

    def reconciled(self, label, minimum=0):
        end = time.monotonic()+10
        samples = []
        while time.monotonic() < end:
            self.same_model()
            status, raw = self.http('/v1/config', timeout=3)
            require(status == 200, 'config unavailable')
            value = json.loads(raw)
            jobs = value['responses_reconciliation']
            samples.append(value)
            if jobs['owned'] == 0 and jobs['pending'] == 0 and jobs['reconciled'] >= minimum:
                save(self.out/(label+'.config.json'), samples)
                return value
            time.sleep(.05)
        save(self.out/(label+'.config.json'), samples)
        raise TimeoutError('foreground metadata did not reconcile')

    def fault(self):
        body = self.plan['requests']['fault']
        self.budget('chats')
        save(self.out/'fault.request.json', body)
        connection = http.client.HTTPConnection('127.0.0.1', self.plan['port'], timeout=10)
        events, identity, partial, stopped = [], None, None, None
        total = 0
        try:
            connection.request('POST', '/v1/responses', json.dumps(body).encode(),
                               headers={'Content-Type': 'application/json'})
            response = connection.getresponse()
            save(self.out/'fault.http.json', dict(status=response.status, headers=dict(response.getheaders())))
            require(response.status == 200, 'fault did not enter real SSE')
            with (self.out/'fault.wire').open('xb') as wire:
                while True:
                    line = response.readline(4*1024*1024+1)
                    if not line:
                        break
                    total += len(line)
                    require(len(line) <= 4*1024*1024 and total <= 32*1024*1024, 'fault wire bound')
                    wire.write(line)
                    require(b'data: [DONE]' not in line, 'Chat sentinel in Responses')
                    if not line.startswith(b'data: '):
                        continue
                    event = json.loads(line[6:])
                    require(not background.wire_errors(event, 'response_event'), 'invalid fault SSE shape')
                    events.append(event)
                    if event['type'] == 'response.created':
                        identity = event['response']['id']
                    if event['type'] == 'response.output_text.delta' and stopped is None:
                        require(identity, 'text before response identity')
                        partial = self.wait_response('pre-fault-partial', identity,
                            lambda v: v['status'] == 'in_progress' and bool(foreground.text(v)))
                        self.same_model()
                        pid, record = own_object_worker(base.lifecycle.guard.processes(), self.model.pid)
                        require(record['executable'] == self.plan['binary'], 'unexpected worker executable')
                        self.history[pid] = record
                        save(self.out/'fault-injection.json', dict(pid=pid, identity=record,
                            model_pid=self.model.pid, partial=partial, mutation='SIGSTOP'))
                        base.lifecycle.guard.signal_identity(pid, record, signal.SIGSTOP)
                        stopped = (pid, record)
        finally:
            connection.close()
            save(self.out/'fault.events.json', events)
        require(stopped is not None, 'fault not injected')
        check_fault(events)
        # Config is independent of object I/O. Wait for the owned coordinator
        # to confirm its commit before asking the recovering store to read it.
        config = self.reconciled('post-fault', 1)
        actual = self.wait_response('fault-reconciled', identity, lambda v: v['status'] == 'failed')
        require(actual['error']['code'] == 'response_execution_interrupted' and
                foreground.text(actual).startswith(foreground.text(partial)), 'partial recovery was lost or replayed')
        require(config['responses_reconciliation']['active'] == 0 and
                config['application_state_health']['controller_starts'] == 1, 'controller ownership changed')
        require(base.lifecycle.guard.processes().get(stopped[0], {}).get('start_ticks') !=
                stopped[1]['start_ticks'], 'stopped worker not reaped')
        require(self.http('/readyz')[0] == 200, 'auxiliary fault changed readiness')

    def business(self):
        rows = self.plan['requests']
        tool = self.sdk('tool', 'create', rows['tool'])
        calls = [item for item in tool['output'] if item['type'] == 'function_call']
        require(tool['status'] == 'completed' and len(calls) == 1 and
                calls[0]['name'] == 'read_project_record', 'real tool missing')
        args = json.loads(calls[0]['arguments'])
        require(args == dict(path=foreground.DOCUMENT, start_line=1, line_count=3), 'tool args')
        result = base.ProjectRecords(ROOT).read(args)
        require(result['ok'] and result['source']['sha256'] == self.plan['sources'][foreground.DOCUMENT], 'document identity')
        save(self.out/'tool-execution.json', dict(call=calls[0], result=result))
        follow = copy.deepcopy(rows['follow'])
        follow.update(previous_response_id=tool['id'], input=[dict(type='function_call_output',
            call_id=calls[0]['call_id'], output=json.dumps(result, ensure_ascii=False))])
        answer = json.loads(foreground.text(self.sdk('follow', 'create', follow)))
        title = result['source']['text'].splitlines()[0].split(': ', 1)[1]
        require(answer['title'] in (title, title.lstrip('# ')) and answer['citation'] ==
                result['source']['citation'], 'real tool and constrained JSON closure')
        self.drained('tool-closure')
        self.reconciled('pre-fault')
        self.fault()
        chat = self.sdk_request('same-pid-chat', rows['chat'])
        require(chat.finish_reason == 'stop' and chat.message['content'].strip() == '42', 'same-PID Chat')
        metrics = self.drained('final')
        require(metrics['q4t_requests_total'] == 4 and metrics['q4t_requests_success_total'] == 3 and
                metrics['q4t_requests_error_total'] == 1, 'generation accounting')
        self.reconciled('final', 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='mode', required=True)
    command = modes.add_parser('prepare')
    command.add_argument('--binary', type=Path, required=True)
    command.add_argument('--output', type=Path, required=True)
    command.add_argument('--port', type=int, default=18164)
    command = modes.add_parser('run')
    command.add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    if args.mode == 'prepare':
        prepare(args)
    else:
        def deadline(_signal, _frame):
            raise TimeoutError('foreground recovery deadline')
        previous = signal.signal(signal.SIGALRM, deadline)
        try:
            signal.setitimer(signal.ITIMER_REAL, LIMITS['seconds']-40)
            with base.lifecycle.child_subreaper():
                Stage(read(args.plan)).run()
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)


if __name__ == '__main__':
    main()
