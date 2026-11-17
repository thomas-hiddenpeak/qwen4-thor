"""Exact retained service: selfcheck, affine video SSE and same-PID text.

Reuse the bounded service lifecycle and postflight. Only the business requests
and their oracle change; production imports remain pinned to the sealed pack.
"""
import copy
import hashlib
import json
from pathlib import Path
import runpy
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
BASE = runpy.run_path(str(ROOT / 'tools/serve_responses/web_service_acceptance.py'))
GLOBALS = BASE['main'].__globals__
require, save = BASE['require'], BASE['save']
EXPECTED = dict(top_left='red', top_right='green', bottom_left='blue',
                bottom_right='yellow', layout='landscape')
ACCEPTED_PLAN = 'docs/evidence/serve-video-affine-business-20261117/plan.json'


def requests():
    # Reuse the frozen actual inputs without importing another driver's test
    # fixtures into a legacy package's sealed production namespaces.
    frozen = json.loads((ROOT / ACCEPTED_PLAN).read_text())
    approved = copy.deepcopy(frozen['requests']['shear'])
    approved['store'] = True
    plain = dict(model='qwen3.8-flash-next', temperature=0,
        enable_thinking=False, max_completion_tokens=64,
        messages=[dict(role='user', content='Compute 17+25. Reply only 42.')])
    return dict(approved=approved, invalid=frozen['invalid_count_request'], plain=plain)


def check_sse(raw):
    events = [json.loads(line[6:]) for line in raw.decode().splitlines()
              if line.startswith('data: ') and line[6:] != '[DONE]']
    require(events and events[0]['type'] == 'response.created' and
            events[-1]['type'] == 'response.completed', 'incomplete affine SSE')
    require(sum(e['type'] == 'response.completed' for e in events) == 1 and
            not any(e['type'] in ('error', 'response.failed') for e in events),
            'affine SSE error or duplicate terminal')
    value = events[-1]['response']
    require(value['status'] == 'completed' and value['error'] is None,
            'affine response failed')
    content = [p for item in value['output'] if item['type'] == 'message'
               for p in item['content']]
    require(content and all(p['type'] == 'output_text' for p in content) and
            json.loads(''.join(p['text'] for p in content)) == EXPECTED,
            'actual affine video answer differs')
    require(value['usage']['input_tokens'] > 1000, 'affine high input usage')
    return value


def check_business(execution):
    read = lambda n: json.loads((execution / n).read_text())
    value = check_sse((execution / 'approved-sse.wire').read_bytes())
    stored = read('stored-approved.wire')
    require(all(stored[k] == value[k] for k in ('id', 'output', 'usage', 'status')),
            'stored affine response changed')
    require(read('other-client-http.json')['status'] == 404, 'client isolation')
    rows = requests()
    for name, key in (('approved-sse', 'approved'), ('invalid', 'invalid'),
                      ('plain', 'plain')):
        require(read(name + '-request.json') == rows[key], 'request drift')
    require(read('invalid-http.json')['status'] == 400 and
            read('invalid.wire')['error']['code'] == 'invalid_file_video',
            'singular matrix was not rejected locally')
    plain = read('plain.wire')['choices'][0]
    require(plain['finish_reason'] == 'stop' and
            plain['message']['content'].strip() == '42', 'same-PID text failed')
    declared = read('capabilities.wire')
    plan = json.loads((execution.parent / 'plan.json').read_text())
    package = Path(plan['package']['package'])
    expected = json.loads((package / 'engine/declarations/capabilities.json').read_text())
    native = declared['file_inputs']['native_video']
    for key in ('affine_display', 'detail'):
        require(native[key] == expected['file_inputs']['native_video'][key],
                'qualified affine/detail declaration differs')
    require(native['detail']['high_min_frame_pixels'] == 65536 and
            native['affine_display']['frozen_conversion'] == 'q4t.cpu_video_png.v4',
            'new video implementation absent')
    continuity = read('core-continuity.json')
    require(continuity['before'] == continuity['after'] and
            continuity['before']['model_pid'] > 0, 'healthy core restarted')
    return dict(usage=value['usage'], actual_affine_answer=EXPECTED,
                singular_error_isolated=True, same_pid_text='42')


def business_record(version):
    require(not version, 'unplanned version switch')
    return dict(status='PASS', actual_affine_high_sse=True,
        stored_response=True, client_isolation=True, singular_error_isolated=True,
        same_pid_text=True, default_replaced=False,
        automatic_request_or_tool_replay=False)


def prepare(args):
    BASE['prepare'](args)
    path = args.output.resolve() / 'plan.json'
    plan = json.loads(path.read_text())
    for fixture in ('tests/fixtures/file_video/geometry.mp4', ACCEPTED_PLAN):
        plan['sources'][fixture] = hashlib.sha256((ROOT / fixture).read_bytes()).hexdigest()
    plan['scope'] = __doc__
    plan['planned'].update(user_generations=2, singular_input_counts=1)
    save(path, plan)


class Stage(BASE['Stage']):
    def exercise(self):
        self.engine.require(self.plan['scenario'] == 'tool_retained', 'retained only')
        before = self.ready(1, 'primary')
        self.request('capabilities', path='/v1/capabilities')
        value = check_sse(self.request('approved-sse', self.plan['requests']['approved'],
            path='/v1/responses', model_work=True, raw_result=True))
        self.request('stored-approved', path='/v1/responses/' + value['id'])
        self.request('other-client', client='beta', expected=404,
                     path='/v1/responses/' + value['id'])
        self.request('invalid', self.plan['requests']['invalid'],
                     path='/v1/responses/input_tokens', expected=400)
        self.request('plain', self.plan['requests']['plain'],
                     path='/v1/chat/completions', model_work=True)
        after = self.ready(1, 'primary')
        fields = ('model_pid', 'model_start_ticks', 'controller_pid', 'controller_start_ticks')
        save(self.output / 'core-continuity.json', dict(
            before={k: before[k] for k in fields}, after={k: after[k] for k in fields}))
        save(self.output / 'media-usage.json', check_business(self.output))
        save(self.output / 'business-final.json', business_record(False))


GLOBALS.update(__file__=__file__, requests=requests, check_sse=check_sse,
    check_business=check_business, business_record=business_record,
    Stage=Stage, prepare=prepare, ACCEPTANCE_PREFIX='VIDEO_AFFINE_SERVICE_')
for function in (BASE['VERSION']['launcher'], BASE['MANAGED']['launcher']):
    function.__globals__['__file__'] = __file__


if __name__ == '__main__':
    BASE['main']()
