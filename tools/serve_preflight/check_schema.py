"""Validate every captured wire, including private preflight/config responses."""
import importlib.metadata
import json
from pathlib import Path
import sys

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]


def validators():
    result = {}
    for key, file in (("preflight", "serve-preflight-v1"),
                      ("config", "serve-config-v1"), ("transport", "serve-transport-v1")):
        schema = json.loads((ROOT / "docs" / (file + ".schema.json")).read_text())
        Draft202012Validator.check_schema(schema)
        result[key] = Draft202012Validator(schema)
    return result


def check_value(value, schemas):
    kind = {"q4t.chat.preflight": "preflight", "q4t.serve.config": "config"}.get(
        value.get("object"), "transport")
    schemas[kind].validate(value)
    if kind == "preflight":
        if "token_ids" in value:
            assert len(value["token_ids"]) == value["prompt_tokens"]
        if "prompt" in value:
            assert len(value["prompt"].encode()) == value["prompt_bytes"]
        assert value["output"]["available_tokens"] <= value["output"]["capped_tokens"]
        assert value["output"]["capped_tokens"] <= value["output"]["requested_tokens"]
        assert value["prefill_chunks"] == (value["prompt_tokens"] +
            value["prefill_chunk_tokens"] - 1) // value["prefill_chunk_tokens"]
    return kind


def check(directory):
    schemas = validators()
    records = []
    paths = sorted(set(directory.rglob("*-wire.bin")) | set(directory.rglob("*-response.bin")))
    for path in paths:
        raw = path.read_bytes()
        if raw.startswith(b"HTTP/"):
            _, separator, raw = raw.partition(b"\r\n\r\n")
            assert separator
        if raw.startswith((b"data:", b":")):
            lines = [r[6:] for r in raw.splitlines() if r.startswith(b"data: ")]
            assert lines[-1] == b"[DONE]" and lines.count(b"[DONE]") == 1
            values = [json.loads(r) for r in lines[:-1]]
        else:
            values = [json.loads(raw)]
        kinds = [check_value(v, schemas) for v in values]
        records.append(dict(path=str(path), envelopes=len(values), kinds=kinds))
    assert records
    with (directory / "schema-result.json").open("x") as out:
        json.dump(dict(passed=True, jsonschema_version=importlib.metadata.version("jsonschema"),
                       records=records), out, indent=2)
        out.write("\n")


if __name__ == "__main__":
    check(Path(sys.argv[1]))
