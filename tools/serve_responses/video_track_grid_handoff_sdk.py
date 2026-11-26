"""Read both accepted video inputs and continue the deleted upload over native TLS."""
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import sys

def verify_text_stream(events, resource):
    # This workload requests one structured text part. Resolve its actual slot
    # so a preceding reasoning item cannot be mistaken for the message.
    rid = resource['id']
    outputs = [(i, item) for i, item in enumerate(resource['output'])
               if item['type'] == 'message']
    assert len(outputs) == 1
    index, message = outputs[0]
    assert len(message['content']) == 1 and message['content'][0]['type'] == 'output_text'
    text = message['content'][0]['text']
    assert isinstance(text, str) and text
    types = [event['type'] for event in events]
    assert types[0] == 'response.created' and types[-1] == 'response.completed'
    assert types.count('response.created') == types.count('response.completed') == 1
    assert [event['sequence_number'] for event in events] == list(range(len(events)))
    assert events[0]['response']['id'] == events[-1]['response']['id'] == rid
    assert events[-1]['response']['status'] == resource['status'] == 'completed'
    final = events[-1]['response']['output'][index]
    assert final['id'] == message['id'] and final['content'][0]['text'] == text
    fragments = []
    ended = False
    for event in events:
        if event['type'] not in ('response.output_text.delta', 'response.output_text.done'):
            continue
        assert not ended and (event['item_id'], event['output_index'], event['content_index']) == (
            message['id'], index, 0)
        if event['type'] == 'response.output_text.done':
            assert fragments and ''.join(fragments) == event['text'] == text
            ended = True
        else:
            assert isinstance(event['delta'], str)
            fragments.append(event['delta'])
    assert ended
    return dict(response=rid, item=message['id'], output_index=index, events=len(events),
                deltas=len(fragments), text_sha256=hashlib.sha256(text.encode()).hexdigest())


def exercise(plan):
    import openai
    original = plan['original']
    parent = original['saved']['response']
    capture = Path(plan['capture'])
    capture.mkdir(mode=0o700)
    layered = plan.get('layered', False)
    if layered:
        expected = {'first_bottom_left': 'red', 'last_bottom_right': 'black'}
        question = ('Inspect the earlier video. Name the color at the center of the lower-left '
                    'quadrant in the first frame, and the lower-right quadrant in the final frame.')
    else:
        expected = {'top_right': 'green', 'bottom_right': 'yellow'}
        question = 'Inspect the original video. Name the colors at the center of its top-right and bottom-right quadrants.'

    with ExitStack() as stack:
        def connect(key):
            return stack.enter_context(openai.OpenAI(
                base_url=plan['base'], api_key=key, max_retries=0, timeout=70,
                _strict_response_validation=layered,
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
        wire = []
        answer = None
        with owner.responses.create(model=plan['model'], previous_response_id=parent, store=True,
                temperature=0, reasoning=dict(effort='none'), max_output_tokens=128,
                input=question,
                text=dict(format=dict(type='json_schema', name='right_quadrants', strict=True, schema=schema)),
                stream=True, stream_options=dict(include_obfuscation=False)) as stream:
            for event in stream:
                assert event.sequence_number == len(events)
                events.append(event.type)
                if layered:
                    wire.append(event.model_dump(mode='json', by_alias=True, exclude_unset=True))
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
        trace = {}
        if layered:
            data = (capture/'current.json').read_bytes()
            trace['stream'] = verify_text_stream(wire, json.loads(data))
            (capture/'stream.json').write_text(json.dumps(wire, ensure_ascii=False))
        return dict(**trace, sdk=openai.__version__, history=history, deleted=deleted, restored=restored,
                    saved=current, colors=expected, events=events, independent_chat=independent)


if __name__ == '__main__':
    print(json.dumps(exercise(json.load(sys.stdin))))
