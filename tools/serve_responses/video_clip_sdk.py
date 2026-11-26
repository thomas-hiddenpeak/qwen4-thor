"""Exercise uploaded and inline clips, frozen Responses history and owner isolation."""
import base64
import hashlib
import json
from pathlib import Path
import ssl
import struct
import sys

import openai


def sample(source, track, movie):
    data = bytearray(source)
    offset = 0
    for rect in (track, movie):
        offset = data.index(b'crgn', offset) + 4
        if data[offset:offset + 2] != b'\x00\x0a':
            raise ValueError('expected short QuickDraw region in original fixture')
        struct.pack_into('>hhhh', data, offset + 2, *rect)
        offset += 10
    return bytes(data)


def region_samples(source):
    # Change only the three rows of inversion points. Both masks have exactly
    # the same bounding box and the same compressed video.
    at = source.rindex(b'clip') + 12
    end = 32767
    diagonal = (0, 0, 64, end, 32, 0, 128, end, 64, 64, 128, end, end)
    other = (0, 64, 128, end, 32, 0, 128, end, 64, 0, 64, end, end)
    if source[at - 4:at] != b'crgn' or struct.unpack_from('>Hhhhh', source, at) != (36, 0, 0, 64, 128):
        raise ValueError('unexpected original QuickDraw fixture')
    if struct.unpack_from('>13h', source, at + 10) != diagonal:
        raise ValueError('unexpected original inversion points')
    def change(words):
        data = bytearray(source)
        struct.pack_into('>13h', data, at + 10, *words)
        return bytes(data)
    return source, change(other), change((*diagonal[:-1], 0))


def hidden(get):
    try:
        get()
    except openai.NotFoundError as error:
        assert error.status_code == 404
        return error.status_code
    raise AssertionError('resource unexpectedly accessible')


def saved_resource(owner, peer, rid, file_id, destination):
    raw = owner.responses.with_raw_response.retrieve(rid)
    resource = raw.parse()
    assert raw.status_code == 200 and resource.id == rid and resource.status == 'completed'
    assert json.loads(resource.output_text) == {'top_left': 'red'}
    destination.write_bytes(raw.http_response.content)
    return dict(response=rid, file=file_id, resource_sha256=hashlib.sha256(
        raw.http_response.content).hexdigest(), get=raw.status_code,
        foreign=hidden(lambda: peer.responses.retrieve(rid)),
        deleted=hidden(lambda: owner.files.retrieve(file_id)))


def main():
    plan = json.load(sys.stdin)
    capture = Path(plan['capture'])
    capture.mkdir(mode=0o700)
    context = ssl.create_default_context(cafile=plan['certificate'])
    with openai.DefaultHttpxClient(verify=context, trust_env=False) as client:
        def sdk(key):
            return openai.OpenAI(base_url=plan['base'], api_key=key, http_client=client,
                                 max_retries=0, timeout=60, _strict_response_validation=True)
        owner, peer = sdk(plan['key']), sdk(plan['other'])
        source = Path(plan['fixture']).read_bytes()
        if plan.get('quickdraw_region', False):
            left, right, invalid = region_samples(source)
            left_colors = ('red', 'black', 'black', 'yellow')
            right_colors = ('black', 'green', 'blue', 'black')
        else:
            whole = (0, 0, 64, 128)
            left = sample(source, whole, (0, 0, 64, 64))
            right = sample(source, (0, 64, 64, 128), whole)
            invalid = sample(source, (32, 0, 0, 64), whole)
            left_colors = ('red', 'black', 'blue', 'black')
            right_colors = ('black', 'green', 'black', 'yellow')
        original = owner.files.create(purpose='user_data',file=('sample-a.mov',left,'video/quicktime'))
        assert original.bytes == len(left)
        corner = ('top_left','top_right','bottom_left','bottom_right')
        question = ('Inspect the video. Name the color at the center of each quadrant. '
                    'Return lowercase English color names for top_left, top_right, bottom_left '
                    'and bottom_right. Return only the requested object.')
        props = {k: {'type':'string','enum':['red','green','blue','yellow','black','white',
                                              'cyan','magenta']} for k in corner}
        schema = dict(type='object',properties=props,required=list(corner),additionalProperties=False)
        common = dict(model='qwen3.8-flash-next',temperature=0,reasoning=dict(effort='none'),
                      max_output_tokens=192,store=True,text=dict(format=dict(type='json_schema',
                          name='frame_colors',strict=True,schema=schema)))
        def content(part):
            return [dict(role='user',content=[dict(type='input_file',detail='high',**part),
                                               dict(type='input_text',text=question)])]
        handoff = plan.get('handoff', False)
        first_common = common
        uploaded = content(dict(file_id=original.id))
        if handoff:
            # The retained answer contains only one color. Later turns must
            # recover the remaining colors from the stored video input.
            uploaded[0]['content'][1]['text'] = 'Name the color at the center of the top-left quadrant.'
            first_common = dict(common, text=dict(format=dict(type='json_schema',name='corner',
                strict=True,schema=dict(type='object',properties={'top_left':props['top_left']},
                                        required=['top_left'],additionalProperties=False))))
        counted = owner.responses.input_tokens.count(model=common['model'],input=uploaded,
            reasoning=first_common['reasoning'],text=first_common['text'])
        first = owner.responses.create(**first_common,input=uploaded)
        def check(label,response,expected):
            assert response is not None
            (capture/(label+'.json')).write_text(response.model_dump_json())
            values=json.loads(response.output_text)
            assert response.status=='completed' and values==dict(zip(corner,expected)),(label,values)
            return dict(response=response.id,status=response.status,colors=values,
                        input_tokens=response.usage.input_tokens)
        summary=dict(uploaded=check('uploaded',first,('red',) if handoff else left_colors))
        assert counted.input_tokens==first.usage.input_tokens
        hidden(lambda: peer.responses.retrieve(first.id))
        def streamed(label,kwargs,expected):
            final=None;seen=[]
            with owner.responses.create(**common,**kwargs,stream=True,
                    stream_options=dict(include_obfuscation=False)) as stream:
                for event in stream:
                    seen.append((event.sequence_number,event.type))
                    if event.type=='response.completed':final=event.response
            assert seen[-1][1]=='response.completed' and [i for i,_ in seen]==list(range(len(seen)))
            row=check(label,final,expected)
            row['events']=seen
            return row
        inline=content(dict(filename='sample-b.mov',file_data='data:video/quicktime;base64,'+
                            base64.b64encode(right).decode()))
        summary['inline']=streamed('inline',dict(input=inline),right_colors)
        assert owner.files.delete(original.id).deleted
        summary['replay']=streamed('replay',dict(previous_response_id=first.id,input='For the earlier video: '+question),left_colors)
        transfer = {}
        if handoff:
            transfer['saved'] = saved_resource(owner, peer, first.id, original.id, capture/'saved-get.json')
            previous = plan.get('previous')
            if previous is not None:
                restored = saved_resource(owner, peer, previous['response'], previous['file'],
                                          capture/'restored-get.json')
                assert restored == previous and restored['response'] != first.id
                summary['restored'] = streamed('restored', dict(previous_response_id=restored['response'],
                    input='For the earlier video: '+question), left_colors)
                transfer['restored'] = restored
        try:owner.responses.input_tokens.count(model=common['model'],input=content(dict(
            filename='sample-c.mov',file_data='data:video/quicktime;base64,'+base64.b64encode(invalid).decode())))
        except openai.BadRequestError as err:assert err.code=='invalid_file_video',err.code
        else:raise AssertionError('invalid track clip accepted')
        chat=peer.chat.completions.create(model=common['model'],temperature=0,max_tokens=32,
            reasoning_effort='none',messages=[dict(role='user',content='Return only the integer product of 6 and 7.')])
        assert chat.choices[0].message.content.strip()=='42'
        print(json.dumps(dict(**transfer,sdk=openai.__version__,responses=summary,foreign=404,malformed=400,
            counted=counted.input_tokens,deleted_before_replay=True,independent_chat='42',
            sources=[hashlib.sha256(raw).hexdigest() for raw in (left,right)])))


if __name__ == '__main__':
    main()
