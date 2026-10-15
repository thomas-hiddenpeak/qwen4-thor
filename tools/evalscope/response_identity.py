"""Join recorded HTTP/SSE messages by their actual response ID.

Selected from offload diagnostic commit 0df355a. This module needs neither
runtime diagnostics nor an offload server. Evalscope may omit usage-only
messages; the result describes only the messages present in its database.
"""
import re


def response_identity(messages):
    """Reject missing, malformed or inconsistent IDs on response messages."""
    values = [message.get('id') for message in messages
              if message.get('choices') or 'id' in message]
    valid = bool(values) and all(isinstance(value, str) and
        re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value) for value in values)
    valid = bool(valid and len(set(values)) == 1)
    return {'response_id': values[0] if valid else None,
            'response_id_valid': valid, 'response_ids_observed': values,
            'response_id_source': 'actual HTTP/SSE response id fields'}
