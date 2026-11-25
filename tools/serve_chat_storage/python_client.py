"""Read a stored input-message page with the official Python SDK and q4t schema."""
import json
from pathlib import Path


def stored_messages_validator(schema_path=None):
    """Compile the service's local application schema once per client."""
    from jsonschema import Draft202012Validator

    path = (Path(schema_path) if schema_path is not None else
            Path(__file__).resolve().parents[2] / 'docs/serve-application-v1.schema.json')
    schema = json.loads(path.read_bytes())
    return Draft202012Validator({
        '$ref': '#/$defs/stored_chat_messages',
        '$defs': schema['$defs'],
    })


def list_stored_messages(client, completion_id, *, validator, **query):
    """Read one page, preserving input roles, tool arguments and SDK HTTP errors."""
    response = client.chat.completions.messages.with_raw_response.list(
        completion_id, **query).http_response
    response.raise_for_status()
    page = response.json()
    validator.validate(page)
    data = page['data']
    first = data[0]['id'] if data else None
    last = data[-1]['id'] if data else None
    if (page['first_id'] != first or page['last_id'] != last or
            (page['has_more'] and not data)):
        raise ValueError('stored message page cursors disagree with its entries')
    return page
