"""Replay sealed Assistants Runs events through the installed Python streaming helpers."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re

import openai

try:
    import httpx2 as httpx
except ImportError:
    import httpx


ROOT = Path(__file__).resolve().parents[2]
RECEIPT = ROOT / 'docs/evidence/serve-assistants-20261124/runs.json'
ACTOR = ROOT / 'tools/serve_responses/assistant_runs_sdk.mjs'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def captures(workspace):
    source = json.loads(RECEIPT.read_bytes())
    # The same public receipt also references the earlier 01 pilot.
    prefix = '.q4t-work/serve-responses-agents-20261123/'
    files = {row['path']: row['sha256'] for row in source['receipts']}

    def read(relative):
        key = prefix + relative
        path = (workspace / key).resolve(strict=True)
        if not path.is_relative_to(workspace) or digest(path) != files[key]:
            raise ValueError(f'Unexpected capture identity for {relative}')
        return json.loads(path.read_bytes())

    run, plan, sdk = (read('runs-business-02/' + name) for name in
                      ('result.json', 'plan.json', 'create/sdk-result.json'))
    assert run == source['run'] and run['status'] == 'PASS' and run['starts'] == 1
    assert sdk == source['sdk'] and sdk['sdk'] == '7.47.0'
    assert read('runs-business-lease-02/exit.json')['status'] == 'COMPLETED'
    assert (plan['binary'], plan['ingress']) == (source['engine'], source['ingress'])
    assert plan['sdk_driver'] == sdk['driver_sha256'] == digest(ACTOR)
    events = read('runs-business-02/create/run-events.json')
    assert len(events) == source['wire']['validated_stream_events']
    assert Counter(row['phase'] for row in events) == sdk['event_counts']
    return events, sdk


def projection(value):
    return value.model_dump(mode='json', by_alias=True, exclude_unset=True)


def differences(expected, actual, path=()):
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in sorted(expected.keys() | actual.keys()):
            if key not in expected or key not in actual:
                yield path + (key,)
            else:
                yield from differences(expected[key], actual[key], path + (key,))
    elif isinstance(expected, list) and isinstance(actual, list) and len(expected) == len(actual):
        for left, right in zip(expected, actual):
            yield from differences(left, right, path + ('[]',))
    elif expected != actual or isinstance(expected, bool) != isinstance(actual, bool):
        yield path


def route(path):
    return re.sub(r'/(thread|run|step)_[A-Za-z0-9_-]+(?=/|$)',
                  lambda match: '/{' + match[1] + '_id}', path)


class Replay:
    def __init__(self):
        self.pending = None

    def expect(self, path, body):
        if self.pending is not None:
            raise AssertionError('Previous response was not consumed')
        self.pending = (path, body)

    def respond(self, request):
        assert self.pending is not None
        path, body = self.pending
        assert request.url.host == 'sdk.invalid' and request.url.path == path
        assert request.method == 'POST' and request.headers['OpenAI-Beta'] == 'assistants=v2'
        self.pending = None
        return httpx.Response(200, headers={'Content-Type':'text/event-stream'}, content=body)


def check_stream(client, replay, phase, events, initial, task_result):
    runs = client.beta.threads.runs
    thread, rid = initial['thread_id'], initial['id']
    args = dict(thread_id=thread)
    path = f'/v1/threads/{thread}/runs'
    call, = initial['required_action']['submit_tool_outputs']['tool_calls']
    if phase == 'create':
        manager = runs.stream(**args, assistant_id=initial['assistant_id'])
    else:
        manager = runs.submit_tool_outputs_stream(**args, run_id=rid, tool_outputs=[
            {'tool_call_id':call['id'], 'output':str(task_result['release_revision'])}])
        path += '/' + rid + '/submit_tool_outputs'
    # Reframe the recorded Node event and data pairs for Python's SSE reader.
    wire = ''.join('event: '+e['event']+'\ndata: '+json.dumps(e['data'])+'\n\n' for e in events)
    replay.expect(path, (wire+'data: [DONE]\n\n').encode())
    mismatches = set()
    completed_calls = []
    with manager as stream:
        for actual, expected in zip(stream, events, strict=True):
            assert actual.event == expected['event']
            if actual.event == 'thread.run.step.completed' and actual.data.step_details.type == 'tool_calls':
                completed_calls.extend(actual.data.step_details.tool_calls)
            mismatches.update((actual.event,) + p for p in differences(expected['data'], projection(actual.data)))
        final = stream.get_final_run()
        assert final.id == rid and final.status == events[-1]['data']['status']
        if phase == 'create':
            recovered, = final.required_action.submit_tool_outputs.tool_calls
            assert recovered.id == call['id'] and projection(recovered.function) == call['function']
        else:
            recovered, = completed_calls
            assert recovered.id == call['id'] and recovered.function.name == call['function']['name']
            assert recovered.function.arguments == call['function']['arguments']
            assert recovered.function.output == str(task_result['release_revision'])
            answer, = stream.get_final_messages()
            assert answer.content[0].text.value.strip() == task_result['answer']
        created = {e['data']['id'] for e in events if e['event'] == 'thread.run.step.created'}
        helper_ids = {s.id for s in stream.get_final_run_steps()}
        assert helper_ids == created
    return dict(step_snapshots=len(helper_ids), completed_tool_calls=len(completed_calls), phase=phase, operation='POST '+route(path), events=len(events),
                final_status=final.status, differing_fields=sorted(mismatches))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, required=True, help='Project root holding the sealed acceptance files')
    parser.add_argument('--strict', action='store_true', help='Enable the private SDK response validation option')
    args = parser.parse_args()
    events, task = captures(args.workspace.resolve(strict=True))
    replay = Replay()
    transport = httpx.Client(transport=httpx.MockTransport(replay.respond), trust_env=False)
    options = {'_strict_response_validation': True} if args.strict else {}
    with openai.OpenAI(api_key='fixture', base_url='https://sdk.invalid/v1', max_retries=0,
                       http_client=transport, **options) as client:
        initial, = [e['data'] for e in events if e['event'] == 'thread.run.requires_action']
        streams = []
        for phase in ('create','submit'):
            group = [e for e in events if e['phase'] == phase]
            streams.append(check_stream(client, replay, phase, group, initial, task))
        assert replay.pending is None
    print(json.dumps(dict(sdk=openai.__version__, strict=args.strict, source_sha256=digest(RECEIPT),
                          streams=streams)))
    return int(any(r['differing_fields'] for r in streams))


if __name__ == '__main__':
    raise SystemExit(main())
