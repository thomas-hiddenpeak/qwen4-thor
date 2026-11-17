"""One start/four creates: actual projective video, frozen history and isolation."""
import argparse
import base64
import copy
import hashlib
import json
from pathlib import Path
import struct
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_responses import video_geometry_acceptance as geometry
from tools.serve_service.package import read, require, save, sha

video, background, foreground, base = (geometry.video, geometry.background,
                                      geometry.foreground, geometry.base)
LIMITS, PROFILE = geometry.LIMITS, geometry.PROFILE
IDENTITY = (65536, 0, 0, 0, 65536, 0, 0, 0, 1 << 30)
MATRICES = dict(homogeneous=(65536, 0, 0, 0, 65536, 0, 0, 0, 1 << 29),
                perspective=(65536, 0, 1 << 20, 0, 65536, 0, 0, 0, 1 << 30),
                singular=(65536, 65536, 0, 65536, 65536, 0, 0, 0, 1 << 30))
EXPECTED = dict(zip((*geometry.CORNERS, 'layout'),
                    ('red', 'green', 'blue', 'yellow', 'landscape')))


def clip(name):
    """Only mutate MP4 display metadata; keep original compressed pixels."""
    data = bytearray((video.ASSETS/'geometry.mp4').read_bytes())
    for atom, offset, matrix in (('mvhd', 36, IDENTITY),
                                 ('tkhd', 40, MATRICES[name])):
        at = data.index(atom.encode()) + 4
        require(data[at] == 0, 'fixture is no longer a v0 video')
        data[at+offset:at+offset+36] = struct.pack('>9i', *matrix)
    return bytes(data)


def requests():
    common = copy.deepcopy(geometry.requests()['rotation'])
    common['input'][0]['content'][0]['detail'] = 'high'
    rows = dict(homogeneous=common)
    rows['homogeneous']['store'] = True
    rows['perspective'] = copy.deepcopy(common)
    rows['perspective']['input'][0]['content'][0] = video.inline('perspective.mp4', clip('perspective'))
    rows['perspective']['input'][0]['content'][0]['detail'] = 'high'
    rows['perspective'].update(stream=True, stream_options=dict(include_obfuscation=False))
    rows['replay'] = copy.deepcopy(common)
    rows['replay']['input'] = 'For the same earlier video, '+geometry.QUESTION
    rows['replay'].update(stream=True, stream_options=dict(include_obfuscation=False))
    rows['plain'] = foreground.requests()['plain']
    return rows


def invalid_count():
    value = requests()['homogeneous']
    value['input'][0]['content'][0] = video.inline('singular.mp4', clip('singular'))
    return {k: v for k, v in value.items()
            if k not in ('temperature', 'max_output_tokens', 'store')}


def answer(response):
    background.check_response(response, 'completed')
    require(not any(part['type'] == 'refusal' for item in response['output']
        if item['type'] == 'message' for part in item['content']),
        'actual projective video refused')
    value = json.loads(foreground.text(response))
    require(value == EXPECTED, 'actual projective video answer differs: '+str(value))
    return value


def prepare(args):
    background.prepare(args, rows=requests(), limits=LIMITS, scope=__doc__)
    path = args.output.resolve()/'plan.json'
    plan = read(path)
    for name in ('tools/serve_responses/video_projective_acceptance.py',
                 'tools/serve_responses/video_geometry_acceptance.py',
                 'tools/serve_service/capacity.py', 'tools/serve_service/package.py',
                 'tests/fixtures/file_video/geometry.mp4'):
        plan['sources'][name] = sha(ROOT/name)
    plan['profile'] = PROFILE
    plan['clips'] = {name: dict(sha256=hashlib.sha256(clip(name)).hexdigest(),
        bytes=len(clip(name)), track_matrix=list(matrix))
        for name, matrix in MATRICES.items()}
    plan['invalid_count_request'] = invalid_count()
    plan['planned'] = dict(starts=1, chats=4, uploads=1, input_counts=2)
    plan['mutations'] = ['own model, upload and isolated objects only',
        'delete own original after completed generation; then use frozen history',
        'one invalid singular input-token request; require same-PID text afterwards']
    plan['derived_requests'] = ['real file ID and previous_response_id only',
        'source is actual original H264 with display-matrix metadata replaced',
        'no expected colors, transformed PNG or fixture oracle enters the model',
        'no hardware decoding, restart, generation retries or performance claim']
    save(path, plan)


class Stage(geometry.Stage):
    request_templates = staticmethod(requests)
    success_status = 'PASS_NATIVE_VIDEO_PROJECTIVE_BUSINESS'

    def start(self):
        super().start()
        projective = read(self.out/'capabilities.json')['file_inputs']['native_video']
        require(projective['projective_display'] == dict(source='movie_after_track',
            arithmetic='signed-integer512-exact', sampling='pixel-center-bilinear16',
            bounds='finite-tight-local-origin', infinite_horizon='capacity_reject',
            frozen_conversion='q4t.cpu_video_png.v5'), 'missing projective contract')

    def business(self):
        for name in MATRICES:
            require(hashlib.sha256(clip(name)).hexdigest() ==
                self.plan['clips'][name]['sha256'], 'derived clip changed')
        require(self.plan['invalid_count_request'] == invalid_count(),
                'invalid-count request changed')
        rows = copy.deepcopy(self.plan['requests'])
        data = clip('homogeneous')
        upload = self.files('upload-homogeneous', 'create', dict(purpose='user_data',
            filename='homogeneous.mp4', data_base64=base64.b64encode(data).decode()))
        require(upload['bytes'] == len(data), 'original upload size changed')
        rows['homogeneous']['input'][0]['content'][0]['file_id'] = upload['id']
        counted = {k: v for k, v in rows['homogeneous'].items()
                   if k not in ('temperature', 'max_output_tokens', 'store')}
        self.budget('controls')
        status, raw = self.http('/v1/responses/input_tokens', counted,
                                label='homogeneous-count')
        require(status == 200, 'homogeneousd video input count failed')
        count = json.loads(raw)
        direct = self.sdk('homogeneous-json', 'create', rows['homogeneous'])
        answers = dict(homogeneous=answer(direct))
        require(count['input_tokens'] == direct['usage']['input_tokens'],
                'homogeneousd video count and generation differ')
        self.drained('homogeneous')
        answers['perspective'] = answer(self.sdk('perspective-sse', 'create', rows['perspective']))
        self.drained('perspective')
        self.files('delete-original', 'delete', file_id=upload['id'])
        rows['replay']['previous_response_id'] = direct['id']
        answers['replay'] = answer(self.sdk('frozen-projective-sse', 'create', rows['replay']))
        self.drained('replay')
        self.budget('controls')
        status, raw = self.http('/v1/responses/input_tokens',
            self.plan['invalid_count_request'], label='singular-count')
        require(status == 400 and json.loads(raw)['error']['code'] == 'invalid_file_video',
                'singular matrix did not fail at the request boundary')
        plain = self.sdk('plain-after-projective-error', 'create', rows['plain'])
        require(plain['status'] == 'completed' and foreground.text(plain).strip() == '42',
                'same-PID ordinary text failed')
        self.drained('final')
        save(self.out/'business-summary.json', dict(starts=1, creates=4,
            answers=answers, input_count_matches=True, source_deleted_before_replay=True,
            singular_request_rejected=True, same_model_pid=True,
            ordinary_text_answer='42', full_quality_or_performance_claim=False))


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
