"""Stored Chat shapes; official wire roles differ from the SDK's assistant type."""
import copy
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def metadata():
    return dict(anyOf=[dict(type='null'), dict(type='object', maxProperties=16,
        propertyNames=dict(maxLength=64),
        additionalProperties=dict(type='string', maxLength=512))])


def definitions():
    def rewrite(value):
        if isinstance(value, dict):
            return {key: child.replace('#/$defs/', '#/$defs/chat_')
                    if key == '$ref' else rewrite(child)
                    for key, child in value.items()}
        if isinstance(value, list):
            return [rewrite(child) for child in value]
        return value

    transport = json.loads((ROOT/'docs/serve-transport-v1.schema.json').read_text())
    result = {'chat_'+key: rewrite(value) for key, value in transport['$defs'].items()}
    ref = lambda name: {'$ref': '#/$defs/'+name}
    def obj(properties, required=()):
        return dict(type='object', properties=properties, required=list(required),
                    additionalProperties=False)
    identifier = dict(type='string', minLength=1, maxLength=128,
                      pattern='^[A-Za-z0-9_-]+$')
    query = dict(after=identifier, limit=dict(type='integer', minimum=1, maximum=100,
                 default=20), order=dict(enum=['asc', 'desc'], default='asc'))
    result['stored_chat_query'] = obj(dict(query, model=dict(type='string'),
                                          metadata=metadata()))
    result['stored_chat_messages_query'] = obj(query)
    result['stored_chat_update'] = obj(dict(metadata=metadata()), ('metadata',))
    result['stored_chat'] = ref('chat_completion')
    result['stored_chat_deleted'] = obj(dict(id=identifier,
        object=dict(const='chat.completion.deleted'), deleted=dict(const=True)),
        ('id', 'object', 'deleted'))
    # Original input messages, not the generated assistant output. Preserve
    # fields as accepted by the shared message contract, including tool links.
    message = json.loads((ROOT/'docs/serve-vision-request-v1.schema.json').read_text())
    # The public stored message projection adds IDs, nullable names, text plus
    # original multimodal parts. Keep the shared tool-call definitions intact.
    def message_rewrite(value):
        if isinstance(value, dict):
            return {key: child.replace('#/$defs/', '#/$defs/chat_input_')
                    if key == '$ref' else message_rewrite(child)
                    for key, child in value.items()}
        if isinstance(value, list):
            return [message_rewrite(child) for child in value]
        return value
    for name in ('message', 'textPart', 'refusalPart', 'historyCall',
                 'imagePart', 'videoPart'):
        shape = message['$defs'][name]
        result['chat_input_'+name] = message_rewrite(shape)
    props = copy.deepcopy(message['$defs']['message']['properties'])
    props = message_rewrite(props)
    props.update(id=identifier, name=dict(type=['string', 'null']),
                 content=dict(type=['string', 'null']),
                 content_parts=dict(anyOf=[dict(type='null'), dict(type='array',
                     items=dict(oneOf=[ref('chat_input_'+name) for name in
                         ('textPart', 'refusalPart', 'imagePart', 'videoPart')]))]))
    result['stored_chat_message'] = obj(props, ('id', 'role', 'content', 'name'))
    for name, item in [('stored_chat_list', 'stored_chat'),
                       ('stored_chat_messages', 'stored_chat_message')]:
        result[name] = obj(dict(object=dict(const='list'), data=dict(type='array',
            maxItems=100, items=ref(item)), first_id=dict(type=['string', 'null']),
            last_id=dict(type=['string', 'null']), has_more=dict(type='boolean')),
            ('object', 'data', 'first_id', 'last_id', 'has_more'))
    return result
