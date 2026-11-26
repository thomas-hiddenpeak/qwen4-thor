"""Interrupt replay with AsyncOpenAI and resume through a fresh SDK client."""
import asyncio
import hashlib
import json
import sys

import openai


def fingerprint(event):
    row = [event.sequence_number, event.type]
    if event.type in ('response.output_text.delta', 'response.output_text.done'):
        text = event.delta if event.type.endswith('.delta') else event.text
        row.extend([event.item_id, event.output_index, event.content_index, text])
    return row


async def exercise(plan):
    checkpoint = plan['checkpoint']
    rid, cursor = checkpoint['response'], checkpoint['cursor']
    retention = plan['retention']

    def connect(key):
        transport = openai.DefaultAsyncHttpxClient(verify=plan['certificate'], trust_env=False)
        return openai.AsyncOpenAI(base_url=plan['base'], api_key=key,
                                  http_client=transport, max_retries=0,
                                  _strict_response_validation=True, timeout=15)

    async with connect(plan['key']) as first:
        started = await first.responses.retrieve(rid, stream=True, include_obfuscation=False)
        prefix = []
        async with started:
            async for event in started:
                assert event.sequence_number == len(prefix)
                prefix.append(fingerprint(event))
                if event.sequence_number == cursor:
                    assert event.type == 'response.output_text.delta'
                    break
            assert not started.response.is_closed
        assert started.response.is_closed and len(prefix) == cursor + 1

    async with connect(plan['key']) as second:
        raw = await second.responses.with_raw_response.retrieve(rid)
        resource = raw.parse()
        assert resource.id == rid and resource.prompt_cache_retention == retention
        assert hashlib.sha256(resource.output_text.encode()).hexdigest() == checkpoint['text_sha256']
        assert {'x-stainless-lang','x-stainless-package-version'} <= {
            s.strip().lower() for s in raw.headers['vary'].split(',')}
        resumed = await second.responses.retrieve(rid, stream=True, starting_after=cursor,
                                                   include_obfuscation=False)
        async with resumed:
            suffix = [fingerprint(event) async for event in resumed]
        assert [e[0] for e in suffix] == list(range(cursor + 1, checkpoint['last'] + 1))
        digest = hashlib.sha256(json.dumps(suffix, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        assert digest == checkpoint['suffix_sha256']
        assert ''.join(row[-1] for row in prefix + suffix if row[1] == 'response.output_text.delta') == resource.output_text
        async with second.responses.stream(response_id=rid, starting_after=cursor,
                                           extra_query={'include_obfuscation': False}) as stream:
            observed = [fingerprint(event) async for event in stream]
            final = await stream.get_final_response()
        assert observed == suffix and final.id == rid and final.output_text == resource.output_text
        assert final.prompt_cache_retention == retention

    async with connect(plan['other']) as other:
        try:
            await other.responses.retrieve(rid, stream=True, starting_after=cursor)
        except openai.NotFoundError as exc:
            assert exc.status_code == 404
        else:
            raise AssertionError('another client consumed the saved stream')
    return dict(sdk=openai.__version__, response=rid, prefix=len(prefix), cursor=cursor,
                resumed=len(suffix), last=checkpoint['last'], suffix_sha256=digest,
                text_sha256=checkpoint['text_sha256'], retention=retention,
                closed_before_terminal=True, helper_final=True, foreign=404)


if __name__ == '__main__':
    print(json.dumps(asyncio.run(exercise(json.load(sys.stdin))), ensure_ascii=False))
