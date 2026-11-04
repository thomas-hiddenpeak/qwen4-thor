"""CPU-only sizing and native request initialization for frozen strict payload.

No HTTP, model weights, grammar compilation or GPU work. Canonical tokenizer
encoding is a planning estimate; constrained generation may use other tokens.
"""
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/examples"))
sys.path.insert(0, str(ROOT))
from chat_client import make_request
from tools.serve_protocol.check_contract import load_validators
from tokenizers import Tokenizer


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / ".q4t-work"):
        raise ValueError("artifacts must be in .q4t-work")
    output.mkdir(parents=True, exist_ok=False)
    tokenizer_path = Path.home() / "models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream/tokenizer.json"
    tokenizer = Tokenizer.from_file(str(tokenizer_path))
    value = "=" * 256
    expected = json.dumps(dict(value=[value]*48), separators=(",", ":"))
    schema = dict(type="object", properties=dict(value=dict(type="array",
        minItems=48, maxItems=48, items=dict(type="string", enum=[value]))),
                  required=["value"], additionalProperties=False)
    schema_bytes = len(json.dumps(schema, separators=(",", ":")).encode())
    assert schema_bytes <= 16384
    ids = tokenizer.encode(expected, add_special_tokens=False).ids
    assert tokenizer.decode(ids, skip_special_tokens=False) == expected
    # An early preparation failure must not consume a model start.
    assert 0 < len(ids) < 1024
    requests = []
    for stream in (False, True):
        body = make_request([dict(role="user", content="仅输出符合给定schema的JSON对象。")],
            [], stream=stream, max_tokens=1024, timeout_ms=180000)
        body["response_format"] = dict(type="json_schema", json_schema=dict(
            name="backpressure_literal", strict=True, schema=schema))
        requests.append(body)
    for name, body in zip(("json-request.json", "sse-request.json"), requests):
        (output / name).write_text(json.dumps(body, ensure_ascii=False, indent=2)+"\n")
    (output / "expected-content.json").write_text(expected)
    validator = load_validators()["chat"]
    for body in requests:
        validator.validate(body)
    probe = ROOT / ".q4t-work/serve-protocol-20261103/cpu/contract-build/contract_probe"
    data = "".join(json.dumps(dict(raw=json.dumps(body, ensure_ascii=False)))+"\n" for body in requests)
    (output / "native-input.jsonl").write_text(data)
    process = subprocess.run([str(probe)], input=data, text=True, capture_output=True, timeout=30)
    (output / "native.jsonl").write_text(process.stdout)
    (output / "native-stderr.log").write_text(process.stderr)
    assert process.returncode == 0 and not process.stderr
    assert [json.loads(line) for line in process.stdout.splitlines()] == [dict(accepted=True)]*2
    result = dict(passed=True, scope="CPU planning only; no real HTTP/model/grammar acceptance",
        payload_characters=len(value)*48, content_bytes=len(expected.encode()), schema_bytes=schema_bytes,
        canonical_tokens=len(ids), max_tokens=1024, request_deadline_ms=180000,
        tokenizers_version=importlib.metadata.version("tokenizers"),
        tokenizer=dict(path=str(tokenizer_path), sha256=digest(tokenizer_path)),
        probe=dict(path=str(probe), sha256=digest(probe)),
        planner=dict(path=str(Path(__file__).resolve()), sha256=digest(__file__)),
        actual_generation_tokens_guaranteed=False)
    (output / "result.json").write_text(json.dumps(result, indent=2)+"\n")
    print(json.dumps({k:result[k] for k in ("passed", "scope", "content_bytes", "schema_bytes", "canonical_tokens")}))


if __name__ == "__main__":
    main()
