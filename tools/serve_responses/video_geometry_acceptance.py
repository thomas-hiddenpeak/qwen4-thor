"""One start/four creates: actual displayed video geometry, JSON/SSE and text."""
import argparse
import copy
import http.client
import json
from pathlib import Path
import subprocess
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_responses import video_acceptance as video
from tools.serve_service import capacity, package
from tools.serve_service.package import read, require, save, sha

background, foreground, base = video.background, video.foreground, video.base
LIMITS = dict(starts=1, chats=4, controls=2000, seconds=600)
CLIPS = dict(rotation='rotate-90.mp4', reflection='mirror-x.mp4',
             combined='wide-rotate-90.mp4')
CORNERS = ('top_left', 'top_right', 'bottom_left', 'bottom_right')
EXPECTED = dict(rotation=('blue', 'red', 'yellow', 'green', 'portrait'),
                reflection=('green', 'red', 'yellow', 'blue', 'landscape'),
                combined=('blue', 'red', 'yellow', 'green', 'portrait'))
PROFILE = capacity.normalize(dict(version=2, allow_media=True, max_seq=1,
    max_len=8192, max_prefill=2048, max_tokens=256,
    max_inflight_requests=8, max_queued_requests=2))
QUESTION = ('Inspect the displayed video frame. Identify the color of each '
    'quadrant as top_left, top_right, bottom_left and bottom_right. Also report '
    'whether the displayed frame is portrait, landscape or square as layout. '
    'Use lowercase English color names. Return only the requested JSON object.')


def requests():
    properties = {key: dict(type='string', enum=['red', 'green', 'blue',
        'yellow', 'cyan', 'magenta', 'black', 'white']) for key in CORNERS}
    properties['layout'] = dict(type='string', enum=[
        'portrait', 'landscape', 'square'])
    rows = {}
    for name, filename in CLIPS.items():
        content = (dict(type='input_file', file_id='file_actual_upload')
                   if name == 'rotation' else video.inline(filename))
        rows[name] = dict(model=foreground.MODEL, temperature=0,
            max_output_tokens=192, reasoning=dict(effort='none'),
            input=[dict(role='user', content=[content,
                dict(type='input_text', text=QUESTION)])],
            text=dict(format=dict(type='json_schema', name='displayed_frame',
                strict=True, schema=base.closed(properties))))
    rows['reflection'].update(stream=True,
        stream_options=dict(include_obfuscation=False))
    rows['plain'] = foreground.requests()['plain']
    return rows


def answer(response, name):
    background.check_response(response, 'completed')
    refusals = [part['refusal'] for item in response['output']
        if item['type'] == 'message' for part in item['content']
        if part['type'] == 'refusal']
    require(not refusals, 'display task refused: '+str(refusals))
    value = json.loads(foreground.text(response))
    expected = dict(zip((*CORNERS, 'layout'), EXPECTED[name]))
    require(value == expected, name+' displayed geometry differs: '+str(value))
    return value


def prepare(args):
    background.prepare(args, rows=requests(), limits=LIMITS, scope=__doc__)
    path = args.output.resolve()/'plan.json'
    plan = read(path)
    for filename in (*CLIPS.values(), 'geometry-identity.json',
                     'generate_geometry.py'):
        source = video.ASSETS/filename
        plan['sources'][str(source.relative_to(ROOT))] = sha(source)
    for name in ('tools/serve_responses/video_geometry_acceptance.py',
                 'tools/serve_service/capacity.py',
                 'tools/serve_service/package.py'):
        plan['sources'][name] = sha(ROOT/name)
    plan['profile'] = PROFILE
    plan['planned'] = dict(starts=1, chats=4, uploads=1, input_counts=1)
    plan['mutations'] = ['own model and isolated objects only; graceful cleanup',
        'no restarts, generation retries, performance or full quality matrix']
    plan['derived_requests'] = [
        'rotation file ID from actual original upload; other inputs are original MP4 bytes',
        'no expected corner colors, transformed PNG or fixture manifest enters the model',
        'all four creates are frozen; final ordinary text must succeed on the same PID']
    save(path, plan)


class Stage(background.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = 'PASS_NATIVE_VIDEO_GEOMETRY_BUSINESS'
    files = video.Stage.files
    upload = video.Stage.upload

    def start(self):
        background.probe_ports((self.plan['port'], self.plan['operations_port']))
        require(self.plan['profile'] == PROFILE, 'managed profile drift')
        settings = package.settings(self.plan['port'], 0,
            operations_port=self.plan['operations_port'], capacity_profile=PROFILE)
        args = package.command(Path(self.plan['binary']).parent,
            dict(settings=settings, model_dir=self.plan['model']))
        args += ['--object-store-dir', str(self.out/'objects')]
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
            raise TimeoutError('display geometry readiness')
        for name in ('capabilities', 'config'):
            status, raw = self.http('/v1/'+name, label='initial-'+name)
            require(status == 200, name+' unavailable')
            save(self.out/(name+'.json'), json.loads(raw))
        caps = read(self.out/'capabilities.json')
        native = caps['file_inputs']['native_video']
        require(native['source_orientation'] and native['square_pixel_display'] and
                native['model_media_enabled'] and caps['runtime']['mtp_enabled'],
                'display geometry and joint media runtime not active')
        self.client.protocol_sha256 = caps['protocol_source_sha256']

    def business(self):
        rows = copy.deepcopy(self.plan['requests'])
        identity = self.upload('upload-rotation', CLIPS['rotation'])
        rows['rotation']['input'][0]['content'][0]['file_id'] = identity
        counted = {k: v for k, v in rows['rotation'].items()
                   if k not in ('temperature', 'max_output_tokens')}
        self.budget('controls')
        status, raw = self.http('/v1/responses/input_tokens', counted,
                                label='rotation-count')
        require(status == 200, 'rotation input count failed')
        count, answers = json.loads(raw), {}
        for name in CLIPS:
            response = self.sdk(name, 'create', rows[name])
            answers[name] = answer(response, name)
            if name == 'rotation':
                require(count['input_tokens'] == response['usage']['input_tokens'],
                        'rotation input count differs from actual generation')
            self.drained(name)
        self.files('delete-original', 'delete', file_id=identity)
        plain = self.sdk('plain-after-geometry', 'create', rows['plain'])
        require(plain['status'] == 'completed' and foreground.text(plain).strip() == '42',
                'same-PID ordinary text failed')
        self.drained('final')
        save(self.out/'business-summary.json', dict(starts=1, creates=4,
            actual_display_answers=answers, input_count_matches=True,
            original_upload_and_inline=True, reflection_sse=True,
            source_deleted=True, same_model_pid=True, ordinary_text_answer='42',
            full_quality_or_performance_claim=False))


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
