"""One start/eight creates: native video input and real SDK/tool closure."""
import argparse
import base64
import copy
import hashlib
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_responses import background_acceptance as background
from tools.serve_responses import mcp_business_peer as peer
from tools.serve_responses import tool_identity_acceptance as identity
from tools.serve_service.package import read, require, save, sha

foreground, base = identity.foreground, identity.base
LIMITS = dict(starts=1, chats=8, controls=2000, seconds=900)
ASSETS = ROOT/'tests/fixtures/file_video'
CLIPS = ('transition.mp4', 'transition-vp8.webm', 'transition-vp9.webm')
QUESTION = ('Inspect the actual video. What is the dominant color at its start '
            'and at its end? Return lowercase English colors in start and end.')


def inline(name, data=None):
    return dict(type='input_file', filename=name, file_data=base64.b64encode(
        (ASSETS/name).read_bytes() if data is None else data).decode())


def requests():
    schema = base.closed({key: dict(type='string', enum=[
        'red', 'green', 'blue', 'yellow', 'cyan', 'magenta', 'black', 'white'])
        for key in ('start', 'end')})
    common = dict(model=foreground.MODEL, temperature=0, max_output_tokens=128,
        reasoning=dict(effort='none'),
        text=dict(format=dict(type='json_schema', name='video_colors',
                              strict=True, schema=schema)))
    direct = dict(common, input=[dict(role='user', content=[
        dict(type='input_file', file_id='file_actual_upload'),
        dict(type='input_text', text=QUESTION)])])
    stream = dict(common, input=[dict(role='user', content=[
        inline(CLIPS[1]), dict(type='input_text', text=QUESTION)])], stream=True,
        stream_options=dict(include_obfuscation=False))
    tool = dict(common, tools=[dict(type='function', name='read_clip', strict=True,
        description='Read the actual local project video file.',
        parameters=base.closed(dict(path=dict(type='string', const=CLIPS[2]))))],
        tool_choice=dict(type='function', name='read_clip'), parallel_tool_calls=False,
        input='Read '+CLIPS[2]+' with read_clip, then '+QUESTION)
    mcp = dict(common, max_output_tokens=256, max_tool_calls=1,
        parallel_tool_calls=False, input='Read the actual project clip with '
        'read_project_clip, then '+QUESTION,
        tools=[dict(type='mcp', server_label='video',
            server_url='https://localhost/mcp', allowed_tools=['read_project_clip'],
            require_approval='never')],
        tool_choice=dict(type='mcp', server_label='video', name='read_project_clip'))
    follow = dict(common, tools=[], tool_choice='none', input=[])
    replay = dict(common, input='For the same earlier video, '+QUESTION,
                  stream=True, stream_options=dict(include_obfuscation=False))
    bad = dict(common, input=[dict(role='user', content=[
        inline('broken.mp4', (ASSETS/CLIPS[0]).read_bytes()[:16]),
        dict(type='input_text', text=QUESTION)])])
    return dict(direct=direct, stream=stream, tool=tool, follow=follow, mcp=mcp,
                replay=replay, bad=bad, plain=foreground.requests()['plain'])


def color_answer(response):
    background.check_response(response, 'completed')
    value = json.loads(foreground.text(response))
    require(value == dict(start='red', end='blue'),
            'actual video transition differs: '+str(value))
    return value


class VideoSource:
    name = 'read_project_clip'
    arguments = dict(path=CLIPS[1])
    uri, filename, mime = 'project://clip.webm', CLIPS[1], 'video/webm'

    def catalog(self):
        return dict(tools=[dict(name=self.name,
            description='Read the actual project video clip and its resource.',
            inputSchema=base.closed(dict(path=dict(type='string', const=self.filename))),
            annotations=dict(readOnlyHint=True, destructiveHint=False,
                              idempotentHint=True))])

    def read(self):
        raw = (ASSETS/self.filename).read_bytes()
        digest = read(ASSETS/'business-identity.json')['sha256'][self.filename]
        require(len(raw) < 256*1024 and hashlib.sha256(raw).hexdigest() == digest,
                'original clip changed')
        source = dict(path=self.filename, sha256=digest, bytes=len(raw))
        # No color answer, expected value or identity manifest enters the model.
        return dict(ok=True, source=source), dict(uri=self.uri, mimeType=self.mime,
            blob=base64.b64encode(raw).decode())


def prepare(args):
    background.prepare(args, rows=requests(), limits=LIMITS, scope=__doc__)
    path = args.output.resolve()/'plan.json'
    plan = read(path)
    for name in (*CLIPS, 'business-identity.json', 'generate_business.py'):
        p = ASSETS/name
        plan['sources'][str(p.relative_to(ROOT))] = sha(p)
    plan['private_peer_port'] = plan['port']+2
    plan['planned'].update(files_uploads=2, input_counts=1,
        actual_function_reads=1, actual_mcp_calls=1, actual_resource_reads=1)
    plan['mutations'] = ['own media model/HTTPS peer and isolated Files/Responses',
        'delete own uploaded MP4 after preparation; resume frozen original',
        'submit one truncated video; require same-PID ordinary inference afterwards']
    plan['derived_requests'] = [
        'file IDs and function/response IDs come from real prior SDK results',
        'actual local clip bytes are the tool result; no fabricated color labels',
        'MCP target URL and generated credential from owned TLS peer only',
        'no hardware codec, concurrency expansion, cloud account or model restart']
    save(path, plan)


class Stage(identity.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = 'PASS_NATIVE_VIDEO_SDK_BUSINESS'

    def __init__(self, plan):
        self.peer = None
        super().__init__(plan)

    def start(self):
        background.probe_ports((self.plan['port'], self.plan['operations_port'],
                                self.plan['private_peer_port']))
        self.peer = peer.ProjectMcp(ROOT, self.out, self.plan['private_peer_port'],
                                    source=VideoSource())
        save(self.out/'mcp-policy.json', dict(version=1, fetch=self.peer.fetch()))
        args = [self.plan['binary'], 'serve', '--model-dir', self.plan['model'],
            '--host', '127.0.0.1', '--port', str(self.plan['port']),
            '--operations-port', str(self.plan['operations_port']),
            '--no-mtp', '--allow-media', '--max-seq', '1', '--max-len', '8192',
            '--max-prefill', '2048', '--max-tokens', '256',
            '--max-inflight-requests', '8', '--max-queued-requests', '2',
            '--object-store-dir', str(self.out/'objects'),
            '--mcp-policy-file', str(self.out/'mcp-policy.json')]
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
            raise TimeoutError('video model readiness')
        for name in ('capabilities', 'config'):
            status, raw = self.http('/v1/'+name, label='initial-'+name)
            require(status == 200, name+' unavailable')
            save(self.out/(name+'.json'), json.loads(raw))
        caps = read(self.out/'capabilities.json')
        video = caps['file_inputs']['native_video']
        require(video['decoder_compiled'] and video['model_media_enabled'] and
                video['timestamps'] and not video['core_readiness_dependency'],
                'missing native video capability')
        self.client.protocol_sha256 = caps['protocol_source_sha256']

    def files(self, label, action, request=None, file_id=None):
        self.budget('controls')
        command = dict(action=action, request=request, id=file_id,
            base_url=f'http://127.0.0.1:{self.plan["port"]}/v1',
            sdk_directory=self.plan['sdk']['sdk_directory'])
        save(self.out/(label+'.request.json'), command)
        env = {k: v for k, v in os.environ.items() if k not in
               ('NODE_OPTIONS', 'NODE_PATH', 'LD_PRELOAD')}
        run = subprocess.run(['node', str(ROOT/'tools/serve_responses/run_files_sdk.mjs')],
            input=json.dumps(command), capture_output=True, text=True, env=env, timeout=20)
        (self.out/(label+'.stderr.log')).write_text(run.stderr)
        require(not run.stderr and run.stdout, 'Files SDK bridge failed')
        result = json.loads(run.stdout)
        save(self.out/(label+'.sdk.json'), result)
        require(run.returncode == 0 and result['ok'] and len(result['exchanges']) == 1,
                'Files request failed or replayed')
        self.same_model()
        return result['result']

    def upload(self, label, name):
        raw = (ASSETS/name).read_bytes()
        require(sha(ASSETS/name) == self.plan['sources'][str((ASSETS/name).relative_to(ROOT))],
                'clip drift before upload')
        value = self.files(label, 'create', dict(purpose='user_data', filename=name,
                           data_base64=base64.b64encode(raw).decode()))
        require(value['bytes'] == len(raw), 'uploaded video size differs')
        return value['id']

    def business(self):
        rows = copy.deepcopy(self.plan['requests'])
        file_id = self.upload('upload-mp4', CLIPS[0])
        rows['direct']['input'][0]['content'][0]['file_id'] = file_id
        counted = {k: v for k, v in rows['direct'].items() if k not in
                   ('temperature', 'max_output_tokens')}
        self.budget('controls')
        status, raw = self.http('/v1/responses/input_tokens', counted, label='video-count')
        require(status == 200, 'video input count failed: '+raw[:256].decode(errors='replace'))
        count = json.loads(raw)
        direct = self.sdk('direct-mp4', 'create', rows['direct'])
        color_answer(direct)
        require(count['input_tokens'] == direct['usage']['input_tokens'],
                'count and actual generation input tokens differ')
        color_answer(self.sdk('inline-vp8-sse', 'create', rows['stream']))
        tool = self.sdk('read-tool', 'create', rows['tool'])
        background.check_response(tool, 'completed')
        calls = [item for item in tool['output'] if item['type'] == 'function_call']
        require(len(calls) == 1 and calls[0]['name'] == 'read_clip' and
                json.loads(calls[0]['arguments']) == dict(path=CLIPS[2]),
                'wrong actual video tool request')
        tool_file = self.upload('upload-tool-vp9', CLIPS[2])
        save(self.out/'tool-execution.json', dict(call=calls[0], source=CLIPS[2],
            sha256=sha(ASSETS/CLIPS[2]), file_id=tool_file, executions=1))
        rows['follow'].update(previous_response_id=tool['id'], input=[dict(
            type='function_call_output', call_id=calls[0]['call_id'],
            output=[dict(type='input_file', file_id=tool_file)])])
        color_answer(self.sdk('tool-vp9-follow', 'create', rows['follow']))
        rows['mcp']['tools'][0].update(server_url=self.peer.url,
                                      authorization=self.peer.secret)
        mcp = self.sdk('mcp-video', 'create', rows['mcp'])
        color_answer(mcp)
        calls = [item for item in mcp['output'] if item['type'] == 'mcp_call']
        require(len(calls) == 1 and calls[0]['status'] == 'completed' and
                calls[0]['name'] == VideoSource.name and
                json.loads(calls[0]['arguments']) == VideoSource.arguments and
                len(self.peer.calls) == len(self.peer.reads) == 1,
                'MCP video was not read exactly once')
        self.files('delete-original', 'delete', file_id=file_id)
        rows['replay']['previous_response_id'] = direct['id']
        color_answer(self.sdk('frozen-video-sse', 'create', rows['replay']))
        self.sdk('bad-video', 'create', rows['bad'], expected_status=400)
        require(read(self.out/'bad-video.sdk.json')['error']['code'] == 'invalid_file_video',
                'unexpected container rejection')
        plain = self.sdk('plain-after-video-error', 'create', rows['plain'])
        require(plain['status'] == 'completed' and foreground.text(plain).strip() == '42',
                'same-PID text inference did not recover')
        self.drained('final')
        save(self.out/'business-summary.json', dict(starts=1, creates=8,
            complete_generations=7, invalid_container=1, input_count_matches=True,
            actual_function_reads=1, actual_mcp_calls=1, actual_resource_reads=1,
            source_deleted_before_replay=True, same_model_pid=True,
            full_quality_or_performance_claim=False))

    def cleanup(self):
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
    p.add_argument('--port', type=int, default=18186)
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
