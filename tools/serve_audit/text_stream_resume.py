"""Check a successful Python Responses stream through an independently installed Node SDK."""
import hashlib
import json
import os
from pathlib import Path
import subprocess

from tools.serve_service import package

ACTOR = Path(__file__).with_suffix('.mjs')


def middle_delta(types):
    deltas = [i for i, kind in enumerate(types) if kind == 'response.output_text.delta']
    package.require(len(deltas) >= 2, 'the test needs a cursor inside the text generation')
    return deltas[(len(deltas) - 1) // 2]


def checkpoint(events, result):
    cursor = middle_delta(result['events'])
    suffix = []
    for event in events[cursor + 1:]:
        kind = event['type']
        fields = [event['sequence_number'], kind]
        if kind in ('response.output_text.delta', 'response.output_text.done'):
            fields.extend([event['item_id'], event['output_index'], event['content_index'],
                           event['delta' if kind.endswith('.delta') else 'text']])
        suffix.append(fields)
    data = json.dumps(suffix, ensure_ascii=False, separators=(',', ':')).encode()
    trace = result['stream']
    return dict(response=result['saved']['response'], cursor=cursor, last=len(events) - 1,
                resource_sha256=result['saved']['sha256'], item=trace['item'],
                output_index=trace['output_index'], text_sha256=trace['text_sha256'],
                suffix_sha256=hashlib.sha256(data).hexdigest())


def check(result, previous):
    trace, saved, point = result['stream'], result['saved'], result['checkpoint']
    package.require(point['response'] == saved['response'] and point['resource_sha256'] == saved['sha256']
                    and point['cursor'] == middle_delta(result['events'])
                    and point['last'] == len(result['events']) - 1
                    and all(point[k] == trace[k] for k in ('item', 'output_index', 'text_sha256'))
                    and len(point['suffix_sha256']) == 64
                    and all(c in '0123456789abcdef' for c in point['suffix_sha256']),
                    'cursor is not bound to the original Python response and text')
    source = previous['checkpoint'] if previous else point
    package.require(package.same_json(result['node_resume'], dict(
        sdk='7.47.0', expected=source, delivered=source['last'] - source['cursor'],
        aggregate=True, past_terminal_events=0, invalid_cursor_status=400, foreign_status=404)),
        'Node SDK did not resume the original response at the Python cursor')


def observe(params, events, result, previous, errors):
    result['checkpoint'] = checkpoint(events, result)
    source = previous['checkpoint'] if previous else result['checkpoint']
    plan = {k: params[k] for k in ('base', 'key', 'other', 'sdk')}
    env = os.environ.copy()
    certificate = params['certificate']
    if isinstance(certificate, str):
        env['NODE_EXTRA_CA_CERTS'] = certificate
    run = subprocess.run(['node', ACTOR], input=json.dumps(dict(plan, expected=source)),
                         env=env, text=True, stdout=subprocess.PIPE, stderr=errors,
                         check=True, timeout=65)
    result['node_resume'] = json.loads(run.stdout)
    check(result, previous)
