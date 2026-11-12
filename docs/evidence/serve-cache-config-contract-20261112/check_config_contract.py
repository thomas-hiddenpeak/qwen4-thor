"""Check the missing published fields using captured production responses."""
import copy
import json
from pathlib import Path
from jsonschema import Draft202012Validator
from tools.serve_service.package import sha

ROOT = Path.cwd()
WORK = ROOT / '.q4t-work/serve-main-integration-20261112'
schema = ROOT / 'docs/serve-config-v1.schema.json'
validator = Draft202012Validator(json.loads(schema.read_text()))
validator.check_schema(validator.schema)
rows = []
for path in (WORK / 'profiles-02/primary-config.json',
             WORK / 'profiles-02/light-config.json',
             WORK / 'cache-business-01/initial-config.wire'):
    value = json.loads(path.read_text())
    validator.validate(value)
    cases = []
    for field, replacement in (
            ('readiness_dependency', True), ('state', 'unknown'),
            ('owned', -1), ('active', False), ('recovery_scanned', 1),
            ('private_directory', '/private')):
        changed = copy.deepcopy(value)
        changed['vector_ingestion'][field] = replacement
        assert not validator.is_valid(changed), field
        cases.append(field)
    for field in ('state', 'readiness_dependency', 'active', 'recovery_pending'):
        changed = copy.deepcopy(value)
        del changed['vector_ingestion'][field]
        assert not validator.is_valid(changed), 'missing ' + field
        cases.append('missing ' + field)
    for field, replacement in (('readiness_dependency', True),
                                ('attempts', -1), ('state', 'unknown')):
        changed = copy.deepcopy(value)
        changed['application_startup']['vector_ingestion'][field] = replacement
        assert not validator.is_valid(changed), 'startup ' + field
        cases.append('startup ' + field)
    legacy = copy.deepcopy(value)
    del legacy['vector_ingestion']
    del legacy['application_startup']['vector_ingestion']
    validator.validate(legacy)
    rows.append(dict(path=str(path), sha256=sha(path),
                     state=value['vector_ingestion']['state'], negative=cases))
result = dict(status='PASS_CAPTURED_CONFIG_SCHEMA', schema_sha256=sha(schema),
              actual_configurations=rows, negative_checks=sum(len(r['negative']) for r in rows),
              historical_omission_supported=True, new_gpu_starts=0,
              full_business_accepted=False)
with (WORK / 'config-schema-review-01.json').open('x') as out:
    out.write(json.dumps(result, indent=2) + '\n')
print(json.dumps(result))
