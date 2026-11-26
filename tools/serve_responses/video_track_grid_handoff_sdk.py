"""Read both accepted video inputs and continue the deleted upload over native TLS."""
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import sys

import openai


def exercise(plan):
    original = plan['original']
    parent = original['saved']['response']
    capture = Path(plan['capture'])
    capture.mkdir(mode=0o700)
    expected = {'top_right': 'green', 'bottom_right': 'yellow'}

    with ExitStack() as stack:
        def connect(key):
            return stack.enter_context(openai.OpenAI(
                base_url=plan['base'], api_key=key, max_retries=0, timeout=70,
                http_client=openai.DefaultHttpxClient(verify=plan['certificate'], trust_env=False)))
        owner, peer = connect(plan['key']), connect(plan['other'])

        def missing(fetch):
            try:
                fetch()
            except openai.NotFoundError as error:
                assert error.status_code == 404
                return 404
            raise AssertionError('expected a missing resource')

        def snapshot(label, rid, colors):
            raw = owner.responses.with_raw_response.retrieve(rid)
            resource = raw.parse()
            assert resource.id == rid and resource.status == 'completed'
            assert json.loads(resource.output_text) == colors
            data = raw.http_response.content
            (capture / (label + '.json')).write_bytes(data)
            return dict(response=rid, previous=resource.previous_response_id,
                        sha256=hashlib.sha256(data).hexdigest(),
                        peer=missing(lambda: peer.responses.retrieve(rid)))

        history = {}
        for label in ('uploaded', 'inline', 'replay'):
            old = original['responses'][label]
            history[label] = snapshot(label, old['response'], old['colors'])
            assert history[label]['sha256'] == original['resources'][label]
        deleted = missing(lambda: owner.files.retrieve(original['saved']['file']))
        restored = None
        if plan.get('previous'):
            prior = plan['previous']
            restored = snapshot('restored', prior['response'], expected)
            assert restored == prior
        colors = dict(type='string', enum=['red','green','blue','yellow','black','white'])
        schema = dict(type='object', properties={k:colors for k in expected},
                      required=list(expected), additionalProperties=False)
        events = []
        answer = None
        with owner.responses.create(model=plan['model'], previous_response_id=parent, store=True,
                temperature=0, reasoning=dict(effort='none'), max_output_tokens=128,
                input='Inspect the original video. Name the colors at the center of its top-right and bottom-right quadrants.',
                text=dict(format=dict(type='json_schema', name='right_quadrants', strict=True, schema=schema)),
                stream=True, stream_options=dict(include_obfuscation=False)) as stream:
            for event in stream:
                assert event.sequence_number == len(events)
                events.append(event.type)
                if event.type == 'response.completed':
                    answer = event.response
        assert events[0] == 'response.created' and events[-1] == 'response.completed'
        assert events.count('response.completed') == 1 and 'response.output_text.delta' in events
        assert answer and answer.previous_response_id == parent and json.loads(answer.output_text) == expected
        current = snapshot('current', answer.id, expected)
        assert current['previous'] == parent
        chat = peer.chat.completions.create(model=plan['model'], temperature=0,
            reasoning_effort='none', max_tokens=32, messages=[
                dict(role='user', content='Return only the integer product of 6 and 7.')])
        independent = chat.choices[0].message.content.strip()
        assert independent == '42'
        return dict(sdk=openai.__version__, history=history, deleted=deleted, restored=restored,
                    saved=current, colors=expected, events=events, independent_chat=independent)


if __name__ == '__main__':
    print(json.dumps(exercise(json.load(sys.stdin))))
