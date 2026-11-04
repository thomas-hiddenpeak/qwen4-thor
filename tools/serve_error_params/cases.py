"""Reviewed malformed requests and unchanged supported output contracts."""
from copy import deepcopy


def obj(properties=None, required=None):
    properties = properties or {}
    return dict(type='object', properties=properties,
                required=list(properties) if required is None else required,
                additionalProperties=False)


def body(**fields):
    return dict(messages=[dict(role='user', content='Return the requested value.')],
                enable_thinking=False, **fields)


def tool(**fields):
    return dict(type='function', function=dict(name='read', **fields))


def response(schema, strict=True):
    return dict(type='json_schema', json_schema=dict(name='result',
                strict=strict, schema=schema))


def cases():
    result = []

    def add(name, request, param=None):
        result.append(dict(name=name, request=request, param=param,
                           accepted=param is None))

    for name, fields, param in [
        ('choice-string', dict(tool_choice='force'), 'tool_choice'),
        ('choice-shape', dict(tool_choice=[]), 'tool_choice'),
        ('choice-type', dict(tool_choice=dict(type='custom', function={})), 'tool_choice.type'),
        ('choice-function', dict(tool_choice=dict(type='function')), 'tool_choice.function'),
        ('choice-function-shape', dict(tool_choice=dict(type='function', function=[])), 'tool_choice.function'),
        ('choice-name', dict(tool_choice=dict(type='function', function={})), 'tool_choice.function.name'),
        ('choice-extra', dict(tool_choice=dict(type='function', function=dict(name='f', extra=1))), 'tool_choice.function.extra'),
        ('choice-undeclared', dict(tools=[tool()], tool_choice=dict(type='function', function=dict(name='absent'))), 'tool_choice.function.name'),
        ('choice-no-tools', dict(tool_choice='required'), 'tools'),
        ('choice-raw', dict(tool_choice='none', parse_response=False), 'tool_choice'),
        ('parallel-shape', dict(parallel_tool_calls=0), 'parallel_tool_calls'),
        ('format-shape', dict(response_format=[]), 'response_format'),
        ('format-type', dict(response_format=dict(type='other')), 'response_format.type'),
        ('format-extra', dict(response_format=dict(type='text', extra=True)), 'response_format.extra'),
        ('format-missing', dict(response_format=dict(type='json_schema')), 'response_format.json_schema'),
        ('format-spec-shape', dict(response_format=dict(type='json_schema', json_schema=[])), 'response_format.json_schema'),
        ('format-name', dict(response_format=dict(type='json_schema', json_schema=dict(name='', schema=obj()))), 'response_format.json_schema.name'),
        ('format-strict', dict(response_format=dict(type='json_schema', json_schema=dict(name='x', strict=1, schema=obj()))), 'response_format.json_schema.strict'),
        ('format-description', dict(response_format=dict(type='json_schema', json_schema=dict(name='x', description=1, schema=obj()))), 'response_format.json_schema.description'),
        ('format-schema-missing', dict(response_format=dict(type='json_schema', json_schema=dict(name='x'))), 'response_format.json_schema.schema'),
        ('format-raw', dict(response_format=dict(type='json_object'), parse_response=False), 'parse_response'),
        ('format-tools', dict(response_format=dict(type='json_object'), tools=[tool()]), 'tool_choice'),
        ('format-session', dict(response_format=dict(type='json_object'), session_id='a'*64), 'session_id'),
        ('format-prefix', dict(response_format=dict(type='json_object'), prefix_cache_scope='a'*64), 'prefix_cache_scope')]:
        add(name, body(**fields), param)
    add('format-prompt', dict(prompt='x', response_format=dict(type='json_object')), 'messages')

    for name, tools, param in [
        ('tools-shape', False, 'tools'),
        ('tool-shape', [None], 'tools[0]'),
        ('tool-type', [dict(type='custom', function={})], 'tools[0].type'),
        ('tool-missing', [dict(type='function')], 'tools[0].function'),
        ('function-shape', [dict(type='function', function=[])], 'tools[0].function'),
        ('function-name', [dict(type='function', function={})], 'tools[0].function.name'),
        ('function-duplicate', [tool(), tool()], 'tools[1].function.name'),
        ('function-description', [tool(description=1)], 'tools[0].function.description'),
        ('function-strict', [tool(strict=1)], 'tools[0].function.strict'),
        ('function-parameters', [tool(parameters=None)], 'tools[0].function.parameters'),
        ('function-properties', [tool(parameters=dict(properties=[]))], 'tools[0].function.parameters.properties'),
        ('function-extra', [tool(**{'a.b[0]': 1})], 'tools[0].function["a.b[0]"]')]:
        add(name, body(tools=tools), param)
    add('constrained-name', body(tools=[dict(type='function', function=dict(name='a.b'))],
                                 tool_choice='required'), 'tools[0].function.name')
    add('constrained-count', body(tools=[dict(type='function', function=dict(name=f'f{i}'))
                                          for i in range(17)], tool_choice='required'), 'tools')
    add('constrained-nullable-xml', body(tools=[tool(strict=True, parameters=obj(
        dict(x=dict(type=['string', 'null']))))]), 'tools[0].function.parameters.properties.x.type')
    add('constrained-unused-schema', body(tools=[tool(), dict(type='function',
        function=dict(name='unused', parameters=obj(dict(x=dict(type='string', pattern='x')))))],
        tool_choice=dict(type='function', function=dict(name='read'))),
        'tools[1].function.parameters.properties.x.pattern')

    prefix = 'response_format.json_schema.schema'
    root_cases = [
        ('root-shape', [], ''), ('root-type', dict(type='array'), '.type'),
        ('properties-missing', dict(type='object', additionalProperties=False), '.properties'),
        ('properties-shape', dict(type='object', properties=[], additionalProperties=False), '.properties'),
        ('properties-count', obj({f'p{i}': dict(type='string') for i in range(33)}), '.properties'),
        ('additional-missing', dict(type='object', properties={}), '.additionalProperties'),
        ('additional-true', dict(obj(), additionalProperties=True), '.additionalProperties'),
        ('required-shape', dict(obj(), required=True), '.required'),
        ('required-unknown', obj({}, ['missing']), '.required[0]'),
        ('required-duplicate', obj(dict(x=dict(type='string')), ['x', 'x']), '.required[1]'),
        ('required-incomplete', obj(dict(x=dict(type='string')), []), '.required'),
        ('keyword-quoted', dict(obj(), **{'a.b[0]': 1}), '["a.b[0]"]')]
    for name, schema, suffix in root_cases:
        add('schema-'+name, body(response_format=response(schema)), prefix+suffix)
    for name, child, suffix in [
        ('node-shape', False, ''), ('type-missing', {}, '.type'),
        ('type-invalid', dict(type='anything'), '.type'),
        ('type-union', dict(type=['string', 'number']), '.type'),
        ('type-element', dict(type=['wrong', 'null']), '.type[0]'),
        ('annotation-type', dict(type='string', title=False), '.title'),
        ('annotation-length', dict(type='string', description='a'*2049), '.description'),
        ('object-keyword', dict(type='string', properties={}), '.properties'),
        ('array-keyword', dict(type='string', minItems=0), '.minItems'),
        ('enum-shape', dict(type='string', enum='x'), '.enum'),
        ('enum-empty', dict(type='string', enum=[]), '.enum'),
        ('enum-count', dict(type='string', enum=[str(i) for i in range(33)]), '.enum'),
        ('enum-type', dict(type='string', enum=[True]), '.enum[0]'),
        ('enum-duplicate', dict(type='string', enum=['x', 'x']), '.enum[1]'),
        ('enum-long', dict(type='string', enum=['x'*257]), '.enum[0]'),
        ('keyword', dict(type='string', pattern='x'), '.pattern'),
        ('items-missing', dict(type='array'), '.items'),
        ('items-shape', dict(type='array', items=[]), '.items'),
        ('items-bound', dict(type='array', items=dict(type='string'), maxItems=65), '.maxItems'),
        ('items-bound-order', dict(type='array', items=dict(type='string'), minItems=3, maxItems=2), '.minItems')]:
        add('schema-'+name, body(response_format=response(obj(dict(x=child)))), prefix+'.properties.x'+suffix)
    special = obj({'a.b[0]': dict(type='array', items=obj({'q"\n中文': dict(type='string', enum=['x', 'x'])}))})
    add('schema-escaped-nesting', body(response_format=response(special)),
        prefix+'.properties["a.b[0]"].items.properties["q\\"\\n中文"].enum[1]')
    depth, path = dict(type='string'), prefix
    for _ in range(9):
        depth = obj(dict(x=depth)); path += '.properties.x'
    add('schema-depth', body(response_format=response(depth)), path)
    wide = obj({f'p{i}': obj({f'q{j}': dict(type='string') for j in range(32)}) for i in range(4)})
    add('schema-nodes', body(response_format=response(wide)), prefix+'.properties.p3.properties.q27')
    large = obj({f'p{i}': dict(type='string', description='a'*2048) for i in range(8)})
    add('schema-bytes', body(response_format=response(large)), prefix)

    # Successes compare the exact constraint grammar and rendered prompt with
    # the installed release's source. Advisory auto/raw and none stay advisory.
    for name, request in [
        ('plain', body()), ('raw-prompt', dict(prompt='hello')),
        ('auto', body(tools=[tool(parameters=dict(properties={}, custom=True))])),
        ('raw', body(tools=[tool(parameters=dict(custom=True))], parse_response=False)),
        ('none', body(tools=[tool(parameters=dict(custom=True))], tool_choice='none')),
        ('required', body(tools=[tool(parameters=obj(dict(x=dict(type='integer'))))], tool_choice='required')),
        ('named', body(tools=[tool()], tool_choice=dict(type='function', function=dict(name='read')))),
        ('serial', body(tools=[tool()], parallel_tool_calls=False)),
        ('strict-tool', body(tools=[tool(parameters=obj(dict(x=dict(type='integer'))), strict=True)])),
        ('text', body(response_format=dict(type='text'))),
        ('object', body(response_format=dict(type='json_object'))),
        ('schema', body(response_format=response(obj(dict(x=dict(type='string', enum=['ok'])))))),
        ('schema-unicode', body(response_format=response(obj({'中文.a': dict(type='string')})))),
        ('schema-nullable', body(response_format=response(obj(dict(x=dict(type=['string', 'null']))))))]:
        add('accepted-'+name, request)
    assert len({row['name'] for row in result}) == len(result)
    return deepcopy(result)


HTTP_CASES = ('choice-string', 'choice-undeclared', 'parallel-shape',
              'format-schema-missing', 'function-duplicate',
              'function-extra', 'constrained-nullable-xml',
              'constrained-unused-schema', 'schema-required-duplicate',
              'schema-items-bound-order', 'schema-escaped-nesting',
              'schema-depth')
