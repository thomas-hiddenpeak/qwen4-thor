"""Validate budget projection plus integer accounting on captured successful wire."""
import json
from pathlib import Path
import sys

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/examples"))
from chat_client import _usage, require
SCHEMA = ROOT / "docs/serve-thinking-v1.schema.json"


def validator(name):
    schema = json.loads(SCHEMA.read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(dict(schema, **{"$ref": "#/$defs/" + name}))


def check_usage(value, limit):
    _usage(value)
    if limit is None:
        require("q4t_thinking_budget" not in value and
                "completion_tokens_details" not in value, "disabled budget usage drift")
    else:
        validator("usage").validate(value)
        require(value["q4t_thinking_budget"]["limit"] == limit, "request/usage limit mismatch")


def check(directory):
    request_schema = validator("request")
    records = []
    for path in sorted(directory.rglob("*-wire.bin")):
        request = json.loads(path.with_name(path.name.replace("-wire.bin", "-request.json")).read_text())
        request.update(request.pop("extra_body", {}))
        request_schema.validate(request)
        raw = path.read_bytes()
        values = ([json.loads(line[6:]) for line in raw.splitlines()
                   if line.startswith(b"data: ") and line != b"data: [DONE]"]
                  if request.get("stream") else [json.loads(raw)])
        usage = [value["usage"] for value in values if value.get("usage") is not None]
        require(len(usage) == 1, "exactly one final usage required")
        check_usage(usage[0], request.get("thinking_token_budget"))
        records.append(dict(path=str(path), usage=usage[0]))
    require(bool(records), "no budget wire evidence")
    with (directory / "thinking-schema-result.json").open("x") as out:
        json.dump(dict(passed=True, schema_id="urn:q4t:serve-thinking-v1",
                       accounting_checked=True, records=records), out, indent=2)
        out.write("\n")


if __name__ == "__main__":
    check(Path(sys.argv[1]))
