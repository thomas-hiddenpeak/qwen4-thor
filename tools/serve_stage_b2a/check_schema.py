"""Validate reasoning projections and per-request output selection on raw wire."""
import json
from pathlib import Path
import sys

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "docs/serve-reasoning-v1.schema.json"


def validator(name):
    schema = json.loads(SCHEMA.read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(dict(schema, **{"$ref": "#/$defs/" + name}))


def check(directory):
    request_schema, history, output, hidden = map(validator,
        ("request", "history", "output", "hidden"))
    records = []
    for path in sorted(directory.rglob("*-wire.bin")):
        request = json.loads(path.with_name(path.name.replace("-wire.bin", "-request.json")).read_text())
        request.update(request.pop("extra_body", {}))
        request_schema.validate(request)
        for message in request["messages"]:
            history.validate(message)
        field = request.get("reasoning_field", "reasoning_content")
        include = request.get("include_reasoning", True)
        raw = path.read_bytes()
        if request.get("stream"):
            values = [json.loads(line[6:]) for line in raw.splitlines()
                      if line.startswith(b"data: ") and line != b"data: [DONE]"]
            messages = [choice["delta"] for value in values for choice in value["choices"]]
        else:
            messages = [json.loads(raw)["choices"][0]["message"]]
        for message in messages:
            output.validate(message)
            assert not (set(message) & {"reasoning", "reasoning_content"}) - {field}
            if not include:
                hidden.validate(message)
        records.append(dict(path=str(path), field=field, include_reasoning=include,
                            validated_messages=len(messages)))
    assert records
    with (directory / "reasoning-schema-result.json").open("x") as f:
        json.dump(dict(passed=True, schema_id="urn:q4t:serve-reasoning-v1", records=records),
                  f, indent=2)
        f.write("\n")


if __name__ == "__main__":
    check(Path(sys.argv[1]))
