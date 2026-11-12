"""Check actual C++ cache health fragments against the published contract."""
import argparse
import copy
import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]


def check(output):
    path = ROOT / 'docs/serve-config-v1.schema.json'
    schema = json.loads(path.read_text())
    Draft202012Validator.check_schema(schema)
    cache_schema = schema['properties']['cache_io']
    validator = Draft202012Validator(cache_schema)
    values = [json.loads(line.removeprefix('[CACHE_HEALTH]'))
              for line in output.read_text().splitlines()
              if line.startswith('[CACHE_HEALTH]')]
    assert len(values) == 2, 'need real disabled/ready C++ health fragments'
    assert values[0]['state'] == 'disabled' and not values[0]['configured']
    assert values[1]['state'] == 'ready' and values[1]['available']
    current = dict(session=values[1], prefix=values[0])
    validator.validate(current)
    mutations = [('readiness_dependency', True), ('workers_active', 2),
                 ('directory', '/private/path'), ('worker_pid', 123),
                 ('buffer_payload_limit_bytes', 0), ('configured', False),
                 ('generation', 0), ('worker_rss_known', False)]
    for key, value in mutations:
        changed = copy.deepcopy(current)
        changed['session'][key] = value
        assert not validator.is_valid(changed), key
    missing = copy.deepcopy(current)
    del missing['prefix']
    assert not validator.is_valid(missing)
    return dict(passed=True, actual_cpp_fragments=len(values),
                negative_cases=len(mutations) + 1,
                schema_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                stdout_sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
                scope='actual C++ cache health and strict published schema; no HTTP/model')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stdout', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(check(args.stdout), indent=2))
