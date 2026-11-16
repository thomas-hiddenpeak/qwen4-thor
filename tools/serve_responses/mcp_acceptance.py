"""One start/eight creates: actual SDK, HTTPS MCP and native tunnel business."""
import argparse
import copy
import http.client
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_responses import tool_identity_acceptance as identity
from tools.serve_responses import background_acceptance as background
from tools.serve_responses import mcp_business_peer as peer
from tools.serve_service.package import read, require, save, sha

foreground, base = identity.foreground, identity.base
LIMITS = dict(starts=1, chats=8, controls=2000, seconds=900)


def requests():
    common = dict(model=foreground.MODEL, temperature=0, max_output_tokens=384,
        reasoning=dict(effort='none'), max_tool_calls=1, parallel_tool_calls=False)
    tool = dict(type='mcp', server_label='project', server_url='https://localhost/mcp',
                allowed_tools=['read_project_record'])
    direct = dict(common, tools=[tool], tool_choice=dict(type='mcp', server_label='project',
        name='read_project_record'), input='Read the first three lines of docs/MODEL.md using '
        'read_project_record with path="docs/MODEL.md", start_line=1, line_count=3. '
        'Use the actual tool result to return its document title and exact citation.',
        text=foreground.requests()['follow']['text'])
    tunnel = copy.deepcopy(direct)
    tunnel['tools'][0].pop('server_url')
    tunnel['tools'][0].update(tunnel_id='tunnel_project', require_approval='never')
    tunnel.update(stream=True, stream_options=dict(include_obfuscation=False))
    follow = dict(common, input=[], tools=[tool], tool_choice='none',
        instructions='Use only the actual tool result to give its first-line document title '
        'and exact citation. If denied or unavailable, state that; do not invent a document.',
        text=foreground.requests()['follow']['text'])
    unknown = copy.deepcopy(direct)
    unknown['tools'][0]['require_approval'] = 'never'
    unknown.pop('text')
    unknown['instructions'] = 'If the tool fails, state that its result is unavailable; do not guess.'
    replay = dict(common, tools=[], tool_choice='none',
                  input='Was the previous tool result received? Answer briefly without another tool.')
    return dict(approval=direct, follow=follow, tunnel=tunnel, unknown=unknown,
                replay=replay, plain=dict(foreground.requests()['plain'], max_output_tokens=64))


def prepare(args):
    background.prepare(args, rows=requests(), limits=LIMITS, scope=__doc__)
    path = args.output.resolve()/'plan.json'
    plan = read(path)
    plan['private_peer_port'] = plan['port']+2
    plan['model_output_capacity'] = 384
    plan['mutations'] = ['only owned model/agent/HTTPS peer and new object store',
        'bounded actual reads of docs/MODEL.md lines1-3',
        'one private MCP connection dropped after a real read; no model restart']
    plan['derived_requests'] = ['actual approval and previous-response IDs only',
        'private peer URL and generated request credential replace test declaration placeholders',
        'direct JSON approval/deny, native tunnel SSE, unknown result and no history replay',
        'trusted same-host backend scope local:anonymous; scoped TLS frontend isolation reuses CPU evidence',
        'no retries, full performance/quality matrix or default replacement']
    save(path, plan)


def one_item(response, kind):
    values = [item for item in response['output'] if item['type'] == kind]
    require(len(values) == 1, 'expected one '+kind)
    return values[0]


def actual_call(response, source):
    background.check_response(response, 'completed')
    call = one_item(response, 'mcp_call')
    require(call['server_label'] == 'project' and call['name'] == 'read_project_record'
        and json.loads(call['arguments']) == peer.ARGUMENTS
        and call['status'] == 'completed' and not call.get('error'), 'MCP call identity/result differs')
    require(source['citation'] in call['output'], 'MCP source was not published')
    answer = json.loads(foreground.text(response))
    title = source['text'].splitlines()[0].split(': ', 1)[1]
    require(answer['title'] in (title, title.lstrip('# ')) and
            answer['citation'] == source['citation'], 'MCP final answer differs from actual document')
    return call


class Stage(identity.Stage):
    limits = LIMITS
    max_output_tokens = 384
    request_templates = staticmethod(requests)
    success_status = 'PASS_MCP_HTTPS_TUNNEL_BUSINESS'

    def __init__(self, plan):
        self.peer = self.agent = None
        self.agent_identity = None
        super().__init__(plan)

    def start(self):
        background.probe_ports((self.plan['port'], self.plan['operations_port'],
                                self.plan['private_peer_port']))
        self.peer = peer.ProjectMcp(ROOT, self.out, self.plan['private_peer_port'])
        policy = dict(version=1, fetch=self.peer.fetch(), targets=[dict(kind='tunnel_id',
            id='tunnel_project', transport='native', generation='business-1',
            scopes=['local:anonymous'], agents=['local:anonymous'], channel='main')])
        save(self.out/'mcp-private-policy.json', policy)
        args = [self.plan['binary'], 'serve', '--model-dir', self.plan['model'],
            '--host', '127.0.0.1', '--port', str(self.plan['port']),
            '--operations-port', str(self.plan['operations_port']), '--mtp',
            '--max-seq', '1', '--max-len', '8192', '--max-prefill', '2048',
            '--max-tokens', '384', '--max-inflight-requests', '8', '--max-queued-requests', '2',
            '--object-store-dir', str(self.out/'objects'),
            '--mcp-policy-file', str(self.out/'mcp-private-policy.json')]
        save(self.out/'model-command.json', args)
        self.budget('starts')
        with (self.out/'model.log').open('xb', buffering=0) as log:
            self.model = subprocess.Popen(args, cwd=ROOT, stdout=log,
                stderr=subprocess.STDOUT, pass_fds=(self.lease,))
        self.model_identity = base.lifecycle.guard.processes()[self.model.pid]
        self.history[self.model.pid] = self.model_identity
        save(self.out/'model-identity.json', dict(pid=self.model.pid, **self.model_identity))
        end = time.monotonic()+180
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
        for name in ('capabilities', 'config'):
            status, raw = self.http('/v1/'+name, label='initial-'+name)
            require(status == 200, name+' unavailable')
            save(self.out/(name+'.json'), json.loads(raw))
        self.client.protocol_sha256 = read(self.out/'capabilities.json')['protocol_source_sha256']
        agent = dict(version=1, api_base=f'http://127.0.0.1:{self.plan["port"]}/v1',
            api_key=secrets.token_urlsafe(32), tunnel_id='tunnel_project', restart_limit=0,
            control_fetch=dict(allow_http=True, public_networks=False,
                allowed_networks=['127.0.0.1/32'], ports=[self.plan['port']], timeout_ms=5000),
            target_fetch=self.peer.fetch(), target=dict(url=self.peer.url))
        save(self.out/'agent-private-config.json', agent)
        with (self.out/'agent.log').open('xb', buffering=0) as log:
            self.agent = subprocess.Popen([self.plan['binary'], 'mcp-tunnel-agent', 'run',
                str(self.out/'agent-private-config.json')], cwd=ROOT, stdout=log,
                stderr=subprocess.STDOUT, pass_fds=(self.lease,))
        self.agent_identity = base.lifecycle.guard.processes()[self.agent.pid]
        self.history[self.agent.pid] = self.agent_identity

    def request(self, name):
        value = copy.deepcopy(self.plan['requests'][name])
        for tool in value.get('tools', []):
            if tool.get('type') == 'mcp':
                if 'server_url' in tool:
                    tool['server_url'] = self.peer.url
                tool['authorization'] = self.peer.secret
        return value

    def follow(self, previous, approved):
        approval = one_item(previous, 'mcp_approval_request')
        require(approval['server_label'] == 'project' and approval['name'] == 'read_project_record'
            and json.loads(approval['arguments']) == peer.ARGUMENTS, 'approval identity differs')
        request = self.request('follow')
        request.update(previous_response_id=previous['id'], input=[dict(type='mcp_approval_response',
            approval_request_id=approval['id'], approve=approved)])
        if not approved:
            request.pop('text')
        return request

    def business(self):
        approval = self.sdk('approval', 'create', self.request('approval'))
        background.check_response(approval, 'completed')
        require(not self.peer.calls, 'tool executed before approval')
        completed = self.sdk('approved', 'create', self.follow(approval, True))
        require(len(self.peer.calls) == 1 and len(self.peer.reads) == 1, 'approved call/resource count')
        actual_call(completed, self.peer.calls[0]['source'])
        stored = self.sdk('approved-get', 'retrieve', response_id=completed['id'])
        require(one_item(stored, 'mcp_call') == one_item(completed, 'mcp_call'), 'stored MCP call differs')
        denied = self.sdk('approval-for-denial', 'create', self.request('approval'))
        rejected = self.sdk('denied', 'create', self.follow(denied, False))
        background.check_response(rejected, 'completed')
        require(len(self.peer.calls) == 1, 'denied approval executed a tool')
        require(one_item(rejected, 'mcp_call')['status'] == 'failed', 'denial lacks failed call')
        tunneled = self.sdk('native-tunnel', 'create', self.request('tunnel'))
        require(len(self.peer.calls) == 2 and len(self.peer.reads) == 2, 'native tunnel did not read once')
        actual_call(tunneled, self.peer.calls[-1]['source'])
        events = read(self.out/'native-tunnel.sdk.json')['chunks']
        for kind in ['response.mcp_list_tools.completed', 'response.mcp_call_arguments.delta',
                     'response.mcp_call.completed', 'response.output_text.delta']:
            require(any(row['type'] == kind for row in events), 'missing native MCP SSE '+kind)
        require(self.agent.poll() is None, 'agent unexpectedly exited')
        self.peer.drop_next = True
        lost = self.sdk('lost-tool-receipt', 'create', self.request('unknown'))
        call = one_item(lost, 'mcp_call')
        require(call['status'] == 'failed' and 'mcp_result_unknown' in json.dumps(call['error'])
                and len(self.peer.calls) == 3, 'unknown result incorrectly retried or treated as known')
        request = self.request('replay')
        request['previous_response_id'] = lost['id']
        background.check_response(self.sdk('unknown-history', 'create', request), 'completed')
        require(len(self.peer.calls) == 3, 'unknown historical call was replayed')
        ordinary = self.sdk('ordinary-after-mcp-failure', 'create', self.request('plain'))
        background.check_response(ordinary, 'completed')
        require(foreground.text(ordinary).strip() == '42', 'ordinary inference stopped after MCP failure')
        for path in [*self.out.glob('*.wire'), self.out/'model.log',
                     *(p for p in (self.out/'objects').rglob('*') if p.is_file())]:
            require(self.peer.secret.encode() not in path.read_bytes(), 'private MCP credential leaked')
        self.drained('final')
        save(self.out/'business-summary.json', dict(model_starts=1, create_requests=8,
            actual_tool_reads=3, resource_reads=2, denied_dispatches=0,
            lost_receipt_executions=1, automatic_tool_replays=0,
            same_model_pid=True, full_quality_or_performance_claim=False))

    def cleanup(self):
        try:
            if self.agent is not None and self.agent.poll() is None:
                base.lifecycle.guard.signal_identity(self.agent.pid, self.agent_identity, signal.SIGTERM)
                try:
                    self.agent.wait(5)
                except subprocess.TimeoutExpired:
                    base.lifecycle.guard.signal_identity(self.agent.pid, self.agent_identity, signal.SIGKILL)
                    self.agent.wait(3)
        finally:
            try:
                super().cleanup()
            finally:
                if self.peer is not None:
                    self.peer.close()
                    save(self.out/'private-peer-results.json', self.peer.snapshot())
                    background.probe_ports((self.plan['private_peer_port'],))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='mode', required=True)
    p = modes.add_parser('prepare')
    p.add_argument('--binary', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--port', type=int, default=18176)
    p = modes.add_parser('run')
    p.add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    if args.mode == 'prepare':
        prepare(args)
    else:
        with base.lifecycle.child_subreaper():
            Stage(read(args.plan)).run()


if __name__ == '__main__':
    main()
