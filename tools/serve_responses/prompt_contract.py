"""q4t native Prompt administration; Responses.prompt remains the SDK type."""
import copy


def definitions(request_properties):
    def obj(properties, required):
        return dict(type='object', properties=properties,
                    required=list(required), additionalProperties=False)

    def ref(name):
        return {'$ref': '#/$defs/'+name}

    version = dict(type='string', pattern='^[1-9][0-9]{0,8}$')
    revision = dict(type='integer', minimum=1, maximum=9007199254740991)
    identifier = dict(type='string', pattern='^[A-Za-z0-9_-]{1,128}$')
    generation_fields = ('model instructions input reasoning text tools tool_choice '
        'parallel_tool_calls temperature top_p top_logprobs max_output_tokens '
        'max_tool_calls truncation include context_management prompt_cache_key '
        'prompt_cache_retention prompt_cache_options moderation access_programs').split()
    definition = obj(dict(request=obj({key: copy.deepcopy(request_properties[key])
        for key in generation_fields}, ()), variables=dict(type='object', maxProperties=64,
        propertyNames=dict(type='string', minLength=1, maxLength=64,
                           pattern=r'^[^\s{}\x00-\x1f\x7f]+$'),
        additionalProperties=dict(enum=['string', 'input_text', 'input_image', 'input_file']))),
        ('request', 'variables'))
    brief = obj(dict(version=version, created_at=dict(type='integer', minimum=0)),
                ('version', 'created_at'))
    resource = obj(dict(id=identifier, object=dict(const='prompt'),
        created_at=dict(type='integer', minimum=0), revision=revision,
        default_version=version, versions=dict(type='array', minItems=1, maxItems=64,
                                               items=brief)),
        ('id', 'object', 'created_at', 'revision', 'default_version', 'versions'))
    full = obj(dict(id=identifier, object=dict(const='prompt.version'),
        version=version, created_at=dict(type='integer', minimum=0), definition=definition),
        ('id', 'object', 'version', 'created_at', 'definition'))
    return dict(
        prompt_definition=definition,
        prompt_create=obj(dict(definition=ref('prompt_definition')), ('definition',)),
        prompt_append=obj(dict(definition=ref('prompt_definition'), expected_revision=revision),
                          ('definition', 'expected_revision')),
        prompt_select=obj(dict(default_version=version, expected_revision=revision),
                          ('default_version', 'expected_revision')),
        prompt_resource=resource, prompt_version=full,
        prompt_deleted=obj(dict(id=identifier, object=dict(const='prompt.deleted'),
            deleted=dict(const=True)), ('id', 'object', 'deleted')),
        prompt_list=obj(dict(object=dict(const='list'), data=dict(type='array', items=ref('prompt_resource')),
            first_id=dict(anyOf=[identifier, dict(type='null')]),
            last_id=dict(anyOf=[identifier, dict(type='null')]), has_more=dict(type='boolean')),
            ('object', 'data', 'first_id', 'last_id', 'has_more')),
        prompt_query=obj(dict(limit=dict(type='integer', minimum=1, maximum=100),
            order=dict(enum=['asc', 'desc']), after=identifier, before=identifier), ()))
