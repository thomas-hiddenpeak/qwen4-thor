"""Exercise native TLS compaction with two distinguishable inline images."""
import base64
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import random
import struct
import sys
import zlib

import openai

from tools.serve_responses.compact_client import CompactWindow


def image_url(channel):
    """Create a PNG with one dominant channel and nonuniform low-order bits."""
    size = 96
    rng = random.Random(560)
    rows = bytearray()
    for _ in range(size):
        rows.append(0)
        for _ in range(size):
            pixel = [rng.randrange(32) for _ in range(3)]
            pixel[channel] += 208
            rows.extend(pixel)

    def chunk(kind, data):
        return (struct.pack('!I', len(data)) + kind + data
                + struct.pack('!I', zlib.crc32(kind + data)))

    header = struct.pack('!IIBBBBB', size, size, 8, 2, 0, 0, 0)
    png = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', header)
           + chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b''))
    return 'data:image/png;base64,' + base64.b64encode(png).decode('ascii')


def verify(params):
    folder = Path(params['capture'])
    folder.mkdir(mode=0o700)
    model = 'qwen3.8-flash-next'
    report = ' '.join(f'Image review {i} recorded a complete scan of 96 rows, '
                      '96 columns and three channels. The inspection log stores '
                      'the image dimensions and pixel layout for the next review.'
                      for i in range(1, 13))
    result = {}
    with ExitStack() as clients:
        def connect(key):
            return clients.enter_context(openai.OpenAI(
                base_url=params['base'], api_key=key, max_retries=0, timeout=90,
                _strict_response_validation=True,
                http_client=openai.DefaultHttpxClient(verify=params['certificate'])))
        owner, foreign = connect(params['key']), connect(params['other'])
        for label, channel in [('Blue', 2), ('Red', 0)]:
            uri = image_url(channel)
            original = dict(type='message', role='user', id='msg_uploaded_image',
                            status='completed', content=[
                                dict(type='input_image', image_url=uri, detail='low'),
                                dict(type='input_text', text='Please retain this image for review.')])
            question = dict(type='message', role='user', id='msg_image_question',
                            status='completed', content=[dict(
                                type='input_text',
                                text='Which primary color dominates the uploaded image? '
                                     'Reply only with the color in English.')])
            archive = dict(type='message', role='assistant', id='msg_image_archive',
                           status='completed', content=[
                               dict(type='output_text', text=report, annotations=[])])
            raw = owner.responses.with_raw_response.compact(
                model=model, input=[original, archive, question])
            wire = raw.http_response.content
            window = CompactWindow.model_validate_json(wire, strict=True).to_dict()
            assert window == raw.http_response.json()
            (folder/(label+'-compact.json')).write_bytes(wire)
            retained = [i for i in window['output'] if i.get('role') == 'user']
            assert retained == [original, question]
            assert sum(i['type'] == 'compaction' for i in window['output']) == 1
            body = dict(model=model, input=window['output'], store=False, temperature=0,
                        max_output_tokens=32, reasoning={'effort':'none'})
            try:
                foreign.responses.create(**body)
            except openai.BadRequestError as error:
                assert error.code == 'invalid_encrypted_content'
            else:
                raise ValueError('Foreign customer opened the saved image')
            reply = owner.responses.with_raw_response.create(**body)
            (folder/(label+'-resumed.json')).write_bytes(reply.http_response.content)
            finished = reply.parse()
            text = finished.output_text.strip()
            assert finished.status == 'completed' and text.casefold() == label.casefold(), text
            result[label] = dict(answer=text, response_id=finished.id, image_url_bytes=len(uri),
                                 image_sha256=hashlib.sha256(uri.encode()).hexdigest(),
                                 retained_ids=[i['id'] for i in retained],
                                 foreign_code='invalid_encrypted_content',
                                 summary_usage=window['usage'], answer_usage=finished.usage.to_dict())
    assert result['Blue']['response_id'] != result['Red']['response_id']
    return dict(sdk=openai.__version__, images=result)


if __name__ == '__main__':
    print(json.dumps(verify(json.load(sys.stdin))))
