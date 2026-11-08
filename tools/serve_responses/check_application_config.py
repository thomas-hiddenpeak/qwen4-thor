"""Validate captured native configuration and affected strict schema boundaries."""
import argparse
import copy
import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]


def check(wire):
    schema = ROOT/'docs/serve-config-v1.schema.json'
    value = json.loads(wire.read_bytes())
    validator = Draft202012Validator(json.loads(schema.read_bytes()))
    validator.check_schema(validator.schema)
    validator.validate(value)
    assert value['application_state']['isolated_io'] is True
    assert value['application_state']['core_readiness_dependency'] is False
    media = validator.schema['properties']['runtime']['properties']['media_limits']
    legacy_fields = set(media['oneOf'][0]['properties'])
    legacy = copy.deepcopy(value)
    legacy['runtime']['media_limits'] = {k: v for k, v in
        value['runtime']['media_limits'].items() if k in legacy_fields}
    legacy['runtime']['media_limits']['profile'] = 'q4t-media-admission-v1'
    del legacy['application_state']
    validator.validate(legacy)
    failures = []
    for key, change in [('profile', 'q4t-media-admission-unknown'),
                        ('mtp', True), ('max_execution_slots', 2)]:
        invalid = copy.deepcopy(value)
        invalid['runtime']['media_limits'][key] = change
        assert not validator.is_valid(invalid), key
        failures.append('media_limits.'+key)
    invalid = copy.deepcopy(value)
    del invalid['runtime']['media_limits']['video_time_unit']
    assert not validator.is_valid(invalid)
    failures.append('media_limits.missing_video_time_unit')
    for key, change in [('max_object_mib', 33), ('directory', '/private'),
                        ('core_readiness_dependency', True)]:
        invalid = copy.deepcopy(value)
        invalid['application_state'][key] = change
        assert not validator.is_valid(invalid), key
        failures.append('application_state.'+key)
    return dict(passed=True, schema_sha256=hashlib.sha256(schema.read_bytes()).hexdigest(),
                wire_sha256=hashlib.sha256(wire.read_bytes()).hexdigest(),
                current_native=True, historical_v1=True, negative_cases=failures)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wire', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = check(args.wire)
    with args.output.open('x') as out:
        out.write(json.dumps(result, indent=2)+'\n')
