"""Current web-search request fields, shared by Chat and Responses schemas."""


def obj(properties, required=()):
    return dict(type='object', properties=properties, required=list(required),
                additionalProperties=False)


def nullable(value):
    return dict(anyOf=[value, dict(type='null')])


def location(chat):
    text = dict(type='string', pattern=r'^[^\r\n\u0000]*$',
                **{'x-q4t-max-utf8-bytes': 256})
    fields = {name: dict(text) for name in ('city', 'country', 'region', 'timezone')}
    fields['country'] = dict(type='string', pattern='^[A-Z]{2}$')
    if chat:
        return nullable(obj(dict(type=dict(const='approximate'),
                                 approximate=obj(fields)), ('type', 'approximate')))
    return nullable(obj(dict(type=dict(const='approximate'),
                             **{name: nullable(value) for name, value in fields.items()})))


def chat_options():
    return nullable(obj(dict(
        search_context_size=dict(enum=['low', 'medium', 'high']),
        user_location=location(True))))


def update_tools(definitions, graph):
    request = definitions[str(graph['roots']['response_create'])]
    request['properties']['tool_choice'] = dict(anyOf=[
        request['properties']['tool_choice'],
        obj(dict(type=dict(enum=['web_search', 'web_search_2025_08_26'])), ('type',))])
    def literals(index):
        node = graph['nodes'][index]
        if node['kind'] == 'literal':
            return {node['value']}
        if node['kind'] == 'union':
            return set().union(*(literals(i) for i in node['variants']))
        return set()

    for index, node in enumerate(graph['nodes']):
        fields = {f['name']: f['type'] for f in node.get('fields', [])}
        if not {'type', 'search_context_size', 'user_location'} <= fields.keys():
            continue
        types = literals(fields['type'])
        if not types or not all(str(value).startswith('web_search') for value in types):
            continue
        properties = definitions[str(index)]['properties']
        properties['user_location'] = location(False)
        # Preview deliberately retains its documented legacy parameter set.
        if any(str(value).startswith('web_search_preview') for value in types):
            continue
        domain = dict(type='string', minLength=1,
                      pattern=r'^[^/:?#@%\[\]\s]+$')
        domains = nullable(dict(type='array', maxItems=100, items=domain))
        properties.update(dict(
            filters=nullable(obj(dict(allowed_domains=domains, blocked_domains=domains))),
            external_web_access=dict(type='boolean'),
            return_token_budget=dict(enum=['default', 'unlimited']),
            search_content_types=dict(type='array', minItems=1, maxItems=2,
                                      uniqueItems=True, items=dict(enum=['text', 'image'])),
            image_settings=obj(dict(max_results=dict(type='integer', minimum=1, maximum=128),
                                    caption=dict(type='boolean')))))
        definitions[str(index)]['allOf'] = [
            {'if': {'required': ['image_settings']},
             'then': {'required': ['search_content_types'], 'properties': {
                 'search_content_types': {'contains': {'const': 'image'}}}}}]
