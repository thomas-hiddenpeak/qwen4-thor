"""Check captured wire envelopes against the versioned stage A response subset."""
import importlib.metadata
import json
from pathlib import Path
import sys

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]


def check(directory):
    schema = json.loads((ROOT / "docs/serve-transport-v1.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    records = []
    paths = sorted(set(directory.rglob("*-wire.bin")) |
                   set(directory.rglob("*-response.bin")))
    for path in paths:
        if path.name == "slow-response.bin":
            # Deliberately interrupted partial TCP frame is not a JSON document.
            records.append(dict(path=str(path), scope="partial write: checked by live driver"))
            continue
        raw = path.read_bytes()
        if raw.startswith(b"HTTP/"):
            _, separator, raw = raw.partition(b"\r\n\r\n")
            assert separator, "missing HTTP separator"
        if raw.startswith(b"data:") or raw.startswith(b":"):
            data = [line[6:] for line in raw.decode().splitlines() if line.startswith("data: ")]
            assert data and data[-1] == "[DONE]" and data.count("[DONE]") == 1
            values = [json.loads(item) for item in data[:-1]]
        else:
            values = [json.loads(raw)]
        for value in values:
            validator.validate(value)
        records.append(dict(path=str(path), validated_envelopes=len(values)))
    assert sum(row.get("validated_envelopes", 0) for row in records) > 0
    (directory / "schema-result.json").write_text(json.dumps(dict(passed=True,
        schema_id=schema["$id"], jsonschema_version=importlib.metadata.version("jsonschema"),
        records=records), indent=2) + "\n")


if __name__ == "__main__":
    check(Path(sys.argv[1]))
