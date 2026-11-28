"""Exchange a stored project response with a different installed OpenAI SDK."""
from contextlib import ExitStack
import json
from pathlib import Path
import ssl
import sys
from urllib.parse import urlsplit

import openai


def summary(response):
    text = ''.join(part.text for item in response.output if item.type == 'message'
                   for part in item.content if part.type == 'output_text')
    return dict(id=response.id, status=response.status, text=text, metadata=response.metadata,
                previous=response.previous_response_id)


def capture(stream):
    with stream:
        events = []
        for e in stream:
            event = dict(seq=e.sequence_number, type=e.type)
            if e.type == 'response.output_text.delta':
                event['delta'] = e.delta
            if e.type == 'response.completed':
                event['response'] = summary(e.response)
            events.append(event)
    return events


def run(plan):
    base = urlsplit(plan['base'])
    assert base.scheme == 'https' and base.hostname == '127.0.0.1' and base.path == '/v1'
    wire = []

    def observe(reply):
        url = reply.request.url
        assert (url.scheme, url.host, url.port) == (base.scheme, base.hostname, base.port)
        record = dict(method=reply.request.method, path=url.path, query=url.query.decode('ascii'),
                      status=reply.status_code, trace=reply.headers.get('x-request-id'))
        with Path(plan['capture']).open('a') as out:
            out.write(json.dumps(record)+'\n')
        wire.append(record)

    def missing(call):
        try:
            call()
        except openai.NotFoundError as exc:
            return dict(status=exc.status_code, code=exc.code)
        raise ValueError('expected an inaccessible response')

    with ExitStack() as stack:
        def resource(key):
            transport = openai.DefaultHttpxClient(verify=ssl.create_default_context(cafile=plan['ca']),
                trust_env=False, event_hooks={'response': [observe]})
            api = stack.enter_context(openai.OpenAI(api_key=key, base_url=plan['base'],
                                     http_client=transport, max_retries=0, timeout=45))
            return (api.beta if plan['beta'] else api).responses

        owner = resource(plan['key'])
        if plan['action'] == 'write':
            request = plan['request']
            counted = owner.input_tokens.count(model=request['model'], input=request['input'])
            events = capture(owner.create(stream=True, **request))
            result = dict(count=counted.input_tokens, created=events[-1]['response'], events=events)
        else:
            assert plan['action'] == 'read'
            ident = plan['id']
            before = summary(owner.retrieve(ident))
            foreign = missing(lambda: resource(plan['peer']).retrieve(ident))
            pages = []
            query = dict(limit=1, order='asc')
            for _ in range(2):
                page = owner.input_items.list(ident, **query)
                assert len(page.data) == 1
                item = page.data[0]
                pages.append(dict(id=item.id, role=item.role, text=[p.text for p in item.content],
                                  more=page.has_more))
                query['after'] = item.id
            follow = summary(owner.create(previous_response_id=ident, **plan['followup']))
            events = capture(owner.retrieve(ident, stream=True))
            owner.delete(ident)
            gone = missing(lambda: owner.retrieve(ident))
            result = dict(before=before, foreign=foreign, pages=pages, follow=follow,
                          events=events, gone=gone)
        return dict(sdk='python-'+openai.__version__, beta=plan['beta'], requests=wire, **result)


if __name__ == '__main__':
    print(json.dumps(run(json.load(sys.stdin)), ensure_ascii=False))
