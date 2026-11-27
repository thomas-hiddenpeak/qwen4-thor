"""OpenAPI media objects for the decoded payload of server-sent events."""


def event_stream(schema, *, error=None, **metadata):
    # Existing q4t consumers also read the extension.
    payload = schema if error is None else {'anyOf': [schema, error]}
    return {'schema': payload, 'x-q4t-event-schema': schema, **metadata}
