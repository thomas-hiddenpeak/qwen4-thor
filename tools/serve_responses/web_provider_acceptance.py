"""Short Chat and Responses witness on a lease-owned, loopback-only engine."""
import http.client
import json
from pathlib import Path

from tools.serve_service.package import require

MODEL = 'qwen3.8-flash-next'
QUERY = 'RFC 2606 reserved example domains'
TASK = ('Name the three example second-level domains reserved by RFC 2606. '
        'Answer using the retrieved source, with its exact citation_marker.')
DOMAINS = ('example.com', 'example.net', 'example.org')


class Witness:
    def __init__(self, port, directory):
        self.port = port
        self.directory = Path(directory)
        self.calls = []

    def request(self, label, path, body=None, expected=200, stream=False):
        payload = None if body is None else json.dumps(body, ensure_ascii=False).encode()
        if payload:
            (self.directory/(label+'.request.json')).write_bytes(payload)
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=90)
        try:
            conn.request('GET' if body is None else 'POST', path, payload,
                         {'Content-Type': 'application/json'} if payload else {})
            res = conn.getresponse()
            raw = res.read((4 << 20) + 1)
            require(len(raw) <= 4 << 20, 'business response exceeds bound')
            (self.directory/(label+'.wire')).write_bytes(raw)
            mime = res.getheader('Content-Type', '').split(';')[0]
            self.calls.append(dict(label=label, path=path, status=res.status, mime=mime))
            require(res.status == expected, f'{label} returned {res.status}, expected {expected}')
            if stream:
                require(mime == 'text/event-stream', 'missing event stream')
                rows = [json.loads(line[5:]) for line in raw.splitlines()
                        if line.startswith(b'data:') and line[5:].strip() != b'[DONE]']
                require([r['sequence_number'] for r in rows] == list(range(len(rows))) and
                        [r['type'] for r in rows].count('response.completed') == 1 and
                        rows[-1]['type'] == 'response.completed', 'incomplete Responses stream')
                return rows[-1]['response']
            require(mime == 'application/json', 'missing JSON business response')
            return json.loads(raw)
        finally:
            conn.close()

    @staticmethod
    def answer(text, annotations, url, title, chat=False):
        marker = '[source](<' + url + '>)'
        require(all(name in text.lower() for name in DOMAINS), 'incorrect RFC answer')
        require(annotations, 'answer has no source annotation')
        for item in annotations:
            require(item['type'] == 'url_citation', 'unexpected annotation type')
            citation = item['url_citation'] if chat else item
            require(citation['url'] == url and citation['title'] == title and
                    text[citation['start_index']:citation['end_index']] == marker,
                    'citation differs from the acquired page or character offsets')

    def live(self, url, title, digest):
        chat = dict(model=MODEL, temperature=0, enable_thinking=False,
                    max_completion_tokens=192, web_search_options={'search_context_size': 'low'},
                    messages=[{'role': 'system', 'content': TASK},
                              {'role': 'user', 'content': QUERY}])
        prompt = self.request('preflight', '/v1/chat/preflight',
                              dict(request=chat, include_prompt=True))['prompt']
        require(digest in prompt and url in prompt and all(n in prompt for n in DOMAINS),
                'preflight lacks actual acquired source bytes or relevant passage')
        choice = self.request('chat', '/v1/chat/completions', chat)['choices'][0]
        require(choice['finish_reason'] == 'stop', 'incomplete Chat answer')
        self.answer(choice['message']['content'], choice['message']['annotations'], url, title, True)
        instruction = (f'Search once for the exact string "{QUERY}". Then answer from '
                       'that source. Copy its citation_marker exactly, including angle brackets.')
        params = dict(model=MODEL, temperature=0, reasoning={'effort': 'none'}, input=TASK,
                      instructions=instruction, max_output_tokens=512, max_tool_calls=1,
                      parallel_tool_calls=False, tool_choice={'type':'web_search'},
                      tools=[{'type':'web_search','search_context_size':'low'}],
                      include=['web_search_call.action.sources'])
        for offline in (False, True):
            name = 'responses-offline' if offline else 'responses'
            if offline:
                params['tools'][0]['external_web_access'] = False
                params['stream'] = True
            value = self.request(name, '/v1/responses', params, stream=offline)
            calls = [i for i in value['output'] if i['type'] == 'web_search_call']
            parts = [p for i in value['output'] if i['type']=='message'
                     for p in i['content'] if p['type']=='output_text']
            require(value['status']=='completed' and len(calls)==len(parts)==1 and
                    calls[0]['status']=='completed' and calls[0]['action']['type']=='search' and
                    [s['url'] for s in calls[0]['action']['sources']] == [url],
                    'expected one completed search and an answer from the acquired URL')
            self.answer(parts[0]['text'], parts[0]['annotations'], url, title)

    def unavailable(self):
        ordinary = dict(model=MODEL, temperature=0, enable_thinking=False, max_completion_tokens=32,
                        messages=[dict(role='user',content='Compute 17 + 25. Reply only with the decimal number.')])
        failed = self.request('provider-unavailable','/v1/chat/completions',
                              dict(ordinary,web_search_options={}),expected=503)
        require(failed['error']['code'] == 'web_search_provider_unavailable',
                'unexpected provider failure classification')
        self.request('state-after-error','/readyz')
        choice = self.request('healthy','/v1/chat/completions',ordinary)['choices'][0]
        require(choice['message']['content'].strip()=='42' and choice['finish_reason']=='stop',
                'ordinary LLM failed after provider outage')


def main():
    import argparse
    import hashlib
    import os
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'evalscope'))
    import gpu_exclusive
    from tools.serve_recovery.child import verify_lease

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['live', 'outage'])
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--source-url')
    parser.add_argument('--source-title')
    parser.add_argument('--reference', type=Path)
    args = parser.parse_args()
    if args.mode == 'live' and not all((args.source_url, args.source_title, args.reference)):
        parser.error('live mode requires the URL, title and independently acquired reference body')
    verify_lease(int(os.environ['Q4T_MANAGED_LEASE_FD']), gpu_exclusive.common_lock())
    args.output.mkdir(mode=0o700)
    witness = Witness(args.port, args.output)
    try:
        if args.mode == 'live':
            with args.reference.open('rb') as source:
                digest = hashlib.file_digest(source, 'sha256').hexdigest()
            witness.live(args.source_url, args.source_title, digest)
        else:
            witness.unavailable()
    finally:
        (args.output/'requests.json').write_text(json.dumps(witness.calls, indent=2)+'\n')


if __name__ == '__main__':
    main()
