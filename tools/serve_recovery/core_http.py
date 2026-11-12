"""Bounded generation self-check and health transport; never retries a POST."""
import json
import re
import time

from tools.serve_ops import transport
from tools.serve_recovery import ledger


def self_check_client(backend):
    # One ordinary request is charged to this explicitly eligible client in
    # every generation; no bypass or extra unmetered internal principal.
    for client in backend['clients']:
        tpm = client.get('tokens_per_minute', 0)
        if (client['max_prompt_tokens'] >= 512 and client['max_output_tokens'] >= 64 and
                (tpm == 0 or tpm >= 576)):
            return client['id']
    raise ValueError('recovery self-check needs a client with prompt/output/TPM capacity')


def request(port, method, path, *, body=None, credentials=None, timeout=2,
            stopped=None):
    ledger.require((method, path) in (('GET', '/healthz'),
        ('POST', '/v1/chat/completions')), 'unsupported recovery HTTP operation')
    raw = b'' if body is None else ledger.canonical(body)
    ledger.require(len(raw) <= 16384, 'self-check body bound')
    headers = ''
    if credentials is not None:
        ledger.keys(credentials, ('client', 'token'))
        ledger.require(type(credentials['client']) is str and
            re.fullmatch('[a-z][a-z0-9_-]{0,31}', credentials['client']) and
            type(credentials['token']) is str and ledger.HASH.fullmatch(credentials['token']),
            'invalid self-check credentials')
        headers = (f"X-Q4T-Client-Id: {credentials['client']}\r\n"
                   f"X-Q4T-Gateway-Token: {credentials['token']}\r\n")
    until = time.monotonic()+timeout
    pending = (f'{method} {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n'
        f'{headers}Content-Type: application/json\r\nContent-Length: {len(raw)}\r\n'
        'Connection: close\r\n\r\n').encode()+raw
    response = bytearray()
    with transport._connect(port, until, stopped) as sock:
        sock.setblocking(False)
        while pending:
            transport._wait(sock, True, until, stopped)
            try:
                written = sock.send(pending)
            except BlockingIOError:
                continue
            ledger.require(written > 0, 'self-check write closed')
            pending = pending[written:]
        while True:
            transport._wait(sock, False, until, stopped)
            try:
                part = sock.recv(65536)
            except BlockingIOError:
                continue
            transport._remaining(until, stopped)
            if not part:
                result = transport.parse_response(bytes(response))
                transport._remaining(until, stopped)
                return result
            response.extend(part)
            ledger.require(len(response) <= transport.MAX_HEADER+transport.MAX_BODY,
                           'self-check response bound')


def self_check(port, credentials, *, stopped=None):
    body = dict(messages=[dict(role='user', content='This is a public service health check. '
        'Return exactly {"ok":true}; the boolean confirms you can generate this JSON. '
        'No private information or external action is requested.')],
        max_tokens=64, stream=False, temperature=0, chat_template_kwargs=dict(enable_thinking=False),
        response_format=dict(type='json_schema', json_schema=dict(name='recovery',
            strict=True, schema=dict(type='object', properties=dict(
                ok=dict(type='boolean', enum=[True])), required=['ok'], additionalProperties=False))))
    reply = request(port, 'POST', '/v1/chat/completions', body=body,
                    credentials=credentials, timeout=30, stopped=stopped)
    ledger.require(reply['http_status'] == 200, 'recovery generation failed')
    value = json.loads(reply['body'])
    ledger.require(type(value) is dict, 'recovery response must be an object')
    choices = value.get('choices')
    ledger.require(type(choices) is list and len(choices) == 1 and
                   type(choices[0]) is dict and
                   choices[0].get('finish_reason') == 'stop',
                   'recovery completion incomplete')
    message = choices[0].get('message')
    ledger.require(type(message) is dict and message.get('role') == 'assistant' and
        not message.get('tool_calls') and not message.get('function_call'),
        'recovery assistant response invalid')
    # A model-produced refusal is a valid terminal branch of the schema
    # contract. It proves generation completed, not a device/context failure.
    # Keep its distinct receipt; never report it as strict JSON or replay it.
    if message.get('refusal') is not None:
        refusal = message['refusal']
        ledger.require(message.get('content') is None and type(refusal) is str and
            bool(refusal.strip()) and '<q4t_refusal>' not in refusal and
            '</q4t_refusal>' not in refusal, 'recovery refusal response invalid')
        return dict(id=value['id'], usage=value['usage'], strict_json=False,
                    native_refusal=True)
    ledger.require(type(message.get('content')) is str,
                   'recovery response has no JSON content')
    content = json.loads(message['content'])
    ledger.keys(content, ('ok',))
    ledger.require(content['ok'] is True,
                   'recovery structured output failed')
    return dict(id=value['id'], usage=value['usage'], strict_json=True)
