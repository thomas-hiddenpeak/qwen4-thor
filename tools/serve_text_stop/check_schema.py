"""Text stop projection plus the byte-bound validation JSON Schema cannot express."""
import json
from pathlib import Path
import sys

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "docs/serve-stop-v1.schema.json"


def check_request(body):
    schema = json.loads(SCHEMA.read_text())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(dict(schema, **{"$ref": "#/$defs/request"})).validate(body)
    stop = body.get("stop")
    values = [stop] if isinstance(stop, str) else stop or []
    assert all(1 <= len(value.encode("utf-8", errors="strict")) <= 256 for value in values)


def check(directory):
    rows = []
    for path in sorted(directory.rglob("*-request.json")):
        # Rejected inputs are captured as .bin, validated by the HTTP driver.
        body = json.loads(path.read_text())
        body.update(body.pop("extra_body", {}))
        check_request(body)
        rows.append(str(path))
    assert rows
    with (directory / "stop-schema-result.json").open("x") as out:
        json.dump(dict(passed=True, schema_id="urn:q4t:serve-stop-v1",
                       utf8_byte_bound_checked=True, requests=rows), out, indent=2)
        out.write("\n")


if __name__ == "__main__":
    check(Path(sys.argv[1]))
