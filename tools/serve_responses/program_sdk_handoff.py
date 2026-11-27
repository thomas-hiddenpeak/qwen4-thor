"""Read an issued function stream with a stock SDK and continue the actual tool."""
from contextlib import ExitStack
import json
from pathlib import Path
import sys

import openai


def exercise(options):
    folder = Path(options['capture'])
    folder.mkdir(mode=0o700)
    original = options['original']
    with ExitStack() as stack:
        def connect(key):
            return stack.enter_context(openai.OpenAI(
                base_url=options['base'], api_key=key, max_retries=0, timeout=75,
                _strict_response_validation=True,
                http_client=openai.DefaultHttpxClient(verify=options['certificate'])))
        owner, peer = connect(options['key']), connect(options['other'])
        api = owner.responses.with_raw_response
        for label, ident in [('call', original['response_id']), ('answer', original['followup'])]:
            raw = api.retrieve(ident)
            assert raw.parse().to_dict() == raw.http_response.json()
            (folder/(label+'.json')).write_bytes(raw.http_response.content)
            try:
                peer.responses.retrieve(ident)
            except openai.NotFoundError as error:
                assert error.status_code == 404
            else:
                raise ValueError('another client retrieved the stored tool history')

        options_stream = dict(stream=True, include_obfuscation=False)
        for label, query in [('events', {}), ('resumed', {'starting_after': original['cursor']})]:
            with owner.responses.retrieve(original['response_id'], **options_stream, **query) as stream:
                events = [event.to_dict() for event in stream]
            (folder/(label+'.json')).write_text(json.dumps(events, separators=(',', ':')))

        if options['continue_tool']:
            # The caller verified this real publication record in its package.
            revision = options['revision']
            raw = api.create(model=options['model'], previous_response_id=original['response_id'],
                input=[{'type': 'function_call_output', 'call_id': original['call_id'],
                        'output': json.dumps({'revision': revision})}],
                tool_choice='none', temperature=0, store=True, max_output_tokens=140,
                reasoning={'effort': 'none'},
                instructions='Reply with just the publication revision number returned by the tool.')
            reply = raw.parse()
            assert reply.status == 'completed' and reply.output_text.strip() == str(revision)
            (folder/'continuation.json').write_bytes(raw.http_response.content)
    return dict(sdk=openai.__version__, foreign_reads=[404, 404])


if __name__ == '__main__':
    print(json.dumps(exercise(json.load(sys.stdin))))
