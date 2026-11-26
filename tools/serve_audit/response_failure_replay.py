"""Replay the sealed native failed resource and terminal through an official Python SDK."""
import argparse
import hashlib
import json
from pathlib import Path

import openai
from openai.types.responses import ResponseError
from pydantic import ValidationError
try:
    import httpx2 as httpx
except ImportError:
    import httpx


ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / 'docs/evidence'


def events(wire):
    return [json.loads(line[6:]) for line in wire.splitlines() if line.startswith(b'data: ')]


def accumulated(manager, expected, numbers):
    with manager as stream:
        seen = list(stream)
        assert [e.sequence_number for e in seen] == numbers
        if numbers:
            last = seen[-1]
            assert last.type == 'response.failed' and last.response.id == expected.id
            assert last.response.error == expected.error
            assert last.response.output[0].arguments == '{' and last.response.output[0].status == 'incomplete'
        try:
            stream.get_final_response()
        except RuntimeError as exc:
            assert str(exc) == "Didn't receive a `response.completed` event."
            return dict(delivered=numbers, final_exception=type(exc).__name__, detail=str(exc))
        raise AssertionError('SDK unexpectedly returned a success-only result for the failed stream')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--strict', action='store_true')
    args = parser.parse_args()
    historic = (EVIDENCE / 'serve-responses-core-20261108/cpu-output-01/responses/failure.wire').read_bytes()
    captures = json.loads((EVIDENCE / 'serve-responses-errors-20261126/cpu-captures.json').read_bytes())
    wire = captures['sse'].encode()
    text_wire = (EVIDENCE / 'serve-responses-declarations-20261109/declarations-cpu-01/work/responses/text.wire').read_bytes()
    original = events(historic)[-1]['response']['error']
    assert original['code'] == 'explicit_cancel'
    try:
        ResponseError.model_validate(original)
    except ValidationError:
        pass
    else:
        raise AssertionError('The historical counterexample was accepted by this SDK')

    def respond(request):
        if request.method == 'GET' and request.url.path == '/v1/responses/resp_failure':
            if request.url.params.get('stream') == 'true':
                assert 'starting_after' not in request.url.params
                return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=wire)
            return httpx.Response(200, headers={'content-type': 'application/json'},
                                  content=captures['response_json'].encode())
        assert request.method == 'POST' and request.url.path == '/v1/responses'
        params = json.loads(request.content)
        assert params['stream'] is True
        if params['input'] == 'completed control':
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=text_wire)
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=wire)

    transport = httpx.Client(transport=httpx.MockTransport(respond), trust_env=False)
    with openai.OpenAI(api_key='fixture', base_url='https://sdk.invalid/v1', max_retries=0,
                       _strict_response_validation=args.strict, http_client=transport) as client:
        stored = client.responses.retrieve('resp_failure')
        with client.responses.create(model='org/alias', input='fixture', stream=True) as stream:
            seen = list(stream)
        assert [e.type for e in seen] == [e['type'] for e in events(wire)]
        assert [e.sequence_number for e in seen] == list(range(len(seen)))
        assert seen[-1].type == 'response.failed' and seen[-1].response == stored
        assert stored.error.code == 'server_error' and stored.error.type == original['code']
        assert stored.error.message == original['message']
        assert stored.output[0].status == 'incomplete' and stored.output[0].arguments == '{'
        assert not any('arguments.done' in e.type for e in seen)
        last = seen[-1].sequence_number
        stream = client.responses.stream
        helpers = dict(create=accumulated(stream(input='fixture', model='org/alias'), stored, list(range(last+1))))
        for cursor in (last-1, last):
            manager = stream(response_id=stored.id, starting_after=cursor)
            helpers[str(cursor)] = accumulated(manager, stored, list(range(cursor+1, last+1)))
        with stream(input='completed control', model='org/alias') as completed:
            result = completed.get_final_response()
            assert result.status == 'completed' and result.output_text == '答案是42✅'
    print(json.dumps(dict(aggregates=helpers, success_control=result.output_text, sdk=openai.__version__, strict=args.strict, events=len(seen),
                          terminal=seen[-1].type, code=stored.error.code, native_type=stored.error.type,
                          historical_error_rejected=True, source_sha256=hashlib.sha256(historic).hexdigest(),
                          wire_sha256=hashlib.sha256(wire).hexdigest())))


if __name__ == '__main__':
    main()
