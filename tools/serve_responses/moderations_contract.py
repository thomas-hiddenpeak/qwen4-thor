"""Standalone moderation wire contract, native finite capacity and SDK source."""
import hashlib
from pathlib import Path

CATEGORIES = ['harassment', 'harassment/threatening', 'hate', 'hate/threatening',
    'illicit', 'illicit/violent', 'self-harm', 'self-harm/instructions',
    'self-harm/intent', 'sexual', 'sexual/minors', 'violence', 'violence/graphic']
IMAGE_CATEGORIES = set(CATEGORIES[6:10] + CATEGORIES[11:])
SDK_MODERATIONS_SHA256 = '049998cc81901c26ebaa88f6e039996bb7182db50d1a596a7cbcd95c125c201f'


def definitions():
    from tools.serve_responses.build_contract import obj, ref
    text = dict(type='string', maxLength=1048576)
    part = dict(anyOf=[obj(dict(type=dict(const='text'), text=text), ['type', 'text']),
        obj(dict(type=dict(const='image_url'), image_url=obj(dict(
            url=dict(type='string', minLength=1)), ['url'])), ['type', 'image_url'])])
    request = obj(dict(model=dict(type='string', minLength=1), input=dict(anyOf=[text,
        dict(type='array', minItems=1, maxItems=32, items=text),
        dict(type='array', minItems=1, maxItems=256, items=part)])), ['input'])
    flags = obj({key:dict(type='boolean') for key in CATEGORIES}, CATEGORIES)
    scores = obj({key:dict(type='number', minimum=0, maximum=1) for key in CATEGORIES}, CATEGORIES)
    applied = obj({key:dict(type='array', uniqueItems=True, maxItems=2,
        items=dict(enum=['text','image'] if key in IMAGE_CATEGORIES else ['text']))
        for key in CATEGORIES}, CATEGORIES)
    result = obj(dict(flagged=dict(type='boolean'), categories=flags,
        category_scores=scores, category_applied_input_types=applied),
        ['flagged','categories','category_scores','category_applied_input_types'])
    response = obj(dict(id=dict(type='string', minLength=1, maxLength=128),
        model=dict(type='string', minLength=1), results=dict(type='array',
        minItems=1, maxItems=32, items=ref('moderation_result'))), ['id','model','results'])
    return dict(moderations_create=request, moderations_response=response,
                moderation_result=result)


def source(sdk):
    path = Path(sdk)/'node_modules/openai/src/resources/moderations.ts'
    if hashlib.sha256(path.read_bytes()).hexdigest() != SDK_MODERATIONS_SHA256:
        raise ValueError('moderations SDK source drift')
    return path


def add_openapi(paths, response, error):
    external = lambda name: {'$ref': 'serve-application-v1.schema.json#/$defs/'+name}
    paths['/v1/moderations'] = dict(post=dict(operationId='v1_moderations',
        requestBody=dict(required=True, content={'application/json':dict(
            schema=external('moderations_create'))}),
        responses={'200':response(external('moderations_response')),
                   'default':response(error, 'Finite native classification error')},
        **{'x-q4t':dict(executor='actual_served_model', default_model='served_model',
            cardinality='string array has one result per string; multimodal array is one content group',
            max_total_text_utf8_bytes=1048576, output_tokens_per_classifier=512,
            threshold=0.5, scores='local model estimates; no cloud calibration claim',
            quota='one RPM and cumulative actual native work; independent Batch pool',
            stream=False)}))
