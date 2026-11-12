"""Capture native SSE bytes beside official SDK helper observations.

The SDK adds snapshot/parsed fields to its high-level helper objects. Those
objects are not server wire responses and must not be validated as such.
"""
import json
import ssl
from urllib.parse import urlsplit

MAX_WIRE_BYTES = 4 * 1024**2


def require(value, message):
    if not value:
        raise RuntimeError(message)


def wire_events(wire):
    require(type(wire) is str and 0 < len(wire.encode()) <= MAX_WIRE_BYTES,
            'SDK wire size differs')
    events = []
    frames = wire.replace('\r\n', '\n').split('\n\n')
    require(not frames[-1].strip(), 'incomplete SSE frame')
    for frame in frames[:-1]:
        lines = [line for line in frame.splitlines() if line and not line.startswith(':')]
        if not lines:
            continue
        names = [line[6:].strip() for line in lines if line.startswith('event:')]
        data = '\n'.join(line[5:].lstrip(' ') for line in lines if line.startswith('data:'))
        require(len(names) == 1 and data != '[DONE]' and
                all(line.startswith(('event:', 'data:')) for line in lines),
                'unexpected native SSE framing')
        value = json.loads(data)
        require(type(value) is dict and value.get('type') == names[0],
                'SSE event type differs')
        events.append(value)
    require(events and events[0]['type'] == 'response.created' and
            events[-1]['type'] == 'response.completed' and
            all(type(e.get('sequence_number')) is int for e in events) and
            [e.get('sequence_number') for e in events] == list(range(len(events))) and
            sum(e['type'] == 'response.completed' for e in events) == 1,
            'native SSE did not complete once')
    return events


def validate_observations(record):
    require(record['version'] == '3.42.1' and type(record['requests']) is int and
            record['requests'] == 1, 'SDK version or request replay differs')
    events = wire_events(record['wire'])
    require(events == record['events'], 'captured SDK wire differs')
    response = events[-1]['response']
    require(record['response'] == response, 'final raw response differs')
    final = record['sdk_final']
    text = ''.join(part['text'] for item in response['output']
                   if item['type'] == 'message' for part in item['content']
                   if part['type'] == 'output_text')
    require(final == dict(id=response['id'], status=response['status'],
                         output_text=text, output_types=[i['type'] for i in response['output']]),
            'SDK helper final observation differs from actual wire')
    require(record['sdk_events'] == [dict(type=e['type'], sequence_number=e['sequence_number'])
                                    for e in events],
            'SDK helper event sequence differs from actual wire')
    return events


def validate(record):
    # Schema validation belongs to the acceptance parent. Keep the pinned SDK
    # environment unchanged; it deliberately does not install jsonschema.
    from tools.serve_responses.test_contract import errors
    events = validate_observations(record)
    require(all(not errors(event, 'response_event') for event in events) and
            not errors(record['response'], 'response'),
            'captured SDK wire contract differs')


def stream_response(config, *, transport=None):
    """One verified TLS request; injected transport is for explicit CPU tests."""
    import httpx2 as httpx
    import openai
    require(openai.__version__ == '3.42.1', 'SDK version changed')
    url = urlsplit(config['base_url'])
    require(url.scheme == 'https' and url.hostname == '127.0.0.1' and
            url.path == '/v1', 'SDK must target private native TLS')
    context = ssl.create_default_context(cafile=config['ca'])
    inner = transport if transport is not None else httpx.HTTPTransport(
        verify=context, trust_env=False, retries=0)
    wire = bytearray()
    calls = []

    class Capture(httpx.BaseTransport):
        def handle_request(self, request):
            require(not calls and request.method == 'POST' and
                    request.url.path == '/v1/responses', 'SDK request replay or route differs')
            calls.append(request.url.path)
            response = inner.handle_request(request)
            source = response.stream

            class Body(httpx.SyncByteStream):
                def __iter__(self):
                    for data in source:
                        require(len(wire)+len(data) <= MAX_WIRE_BYTES, 'SDK wire capacity')
                        wire.extend(data)
                        yield data

                def close(self):
                    source.close()

            response.stream = Body()
            return response

        def close(self):
            inner.close()

    request = dict(config['request'])
    request.pop('stream')
    with httpx.Client(transport=Capture(), trust_env=False) as http:
        with openai.OpenAI(base_url=config['base_url'], api_key=config['key'],
                           max_retries=0, timeout=120, http_client=http) as client:
            with client.responses.stream(**request) as stream:
                # Observe supported helper fields directly. Serializing its
                # generic ParsedResponse adds SDK-only fields and warnings.
                observed = [dict(type=e.type, sequence_number=e.sequence_number) for e in stream]
                final = stream.get_final_response()
                sdk_final = dict(id=final.id, status=final.status, output_text=final.output_text,
                                 output_types=[item.type for item in final.output])
    raw = wire.decode('utf-8')
    events = wire_events(raw)
    value = dict(version=openai.__version__, requests=len(calls), wire=raw,
                 events=events, response=events[-1]['response'],
                 sdk_events=observed, sdk_final=sdk_final)
    validate_observations(value)
    return value
