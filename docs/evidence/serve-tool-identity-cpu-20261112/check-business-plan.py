from pathlib import Path
import ast
from tools.serve_responses.tool_identity_acceptance import requests, LIMITS
from tools.serve_responses.test_contract import errors
ast.parse(Path('tools/serve_responses/tool_identity_acceptance.py').read_text())
for name, request in requests().items():
    issues=errors(request, 'response_create')
    assert not issues, (name, [(list(e.path), e.message[:150]) for e in issues])
print('PASS: four actual business templates match pinned official create schema; budget', LIMITS)
