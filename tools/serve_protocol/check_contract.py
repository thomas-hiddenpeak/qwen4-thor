"""Differential structural/lexical and production native input verification."""
import argparse
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys

from jsonschema import Draft202012Validator, ValidationError, validators
from referencing import Registry, Resource
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.serve_protocol import build_contract, cases

ROOT = build_contract.ROOT


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    def number(text):
        value = Decimal(text)
        if not math.isfinite(float(value)):
            raise ValueError("nonfinite or unrepresentable JSON number")
        return value
    def invalid(_):
        raise ValueError("nonfinite JSON constant")
    value = json.loads(raw, object_pairs_hook=pairs, parse_float=number, parse_constant=invalid)
    def utf8(item):
        if isinstance(item, str):
            item.encode("utf-8")
        elif isinstance(item, list):
            for child in item: utf8(child)
        elif isinstance(item, dict):
            for key, child in item.items(): utf8(key); utf8(child)
    utf8(value)
    return value


def integer(checker, value):
    if isinstance(value, bool): return False
    if isinstance(value, Decimal): return value.is_finite() and value == value.to_integral_value()
    return isinstance(value, int) or (isinstance(value, float) and math.isfinite(value) and value.is_integer())


def utf8_bytes(validator, limit, instance, schema):
    if isinstance(instance, str) and len(instance.encode("utf-8")) > limit:
        yield ValidationError("UTF-8 byte limit exceeded")


Validator = validators.extend(Draft202012Validator,
    validators={"x-q4t-max-utf8-bytes": utf8_bytes},
    type_checker=Draft202012Validator.TYPE_CHECKER.redefine("integer", integer))
# Referenced resources contain their own $schema. Register the companion
# profile for this process so evolve() does not silently fall back to the
# unextended class and lose Decimal exactness or byte annotations at $ref.
# Published documents remain ordinary 2020-12 schemas; their x-q4t rules are
# mandatory companion semantics, not standard JSON Schema assertions.
validators.validates("q4t-request-contract")(Validator)


def load_validators():
    registry = Registry()
    for path in (ROOT / "docs").glob("*.schema.json"):
        value = json.loads(path.read_text())
        Draft202012Validator.check_schema(value)
        registry = registry.with_resource(path.name, Resource.from_contents(value))
        if "$id" in value:
            registry = registry.with_resource(value["$id"], Resource.from_contents(value))
    spec = build_contract.openapi()
    request = lambda route: spec["paths"][route]["post"]["requestBody"]["content"]["application/json"]["schema"]
    return {key: Validator(request(route), registry=registry) for key, route in
        (("chat", "/v1/chat/completions"), ("cancel", "/v1/requests/cancel"),
         ("preflight", "/v1/chat/preflight"))}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--probe", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--only-failed", type=Path)
    args = ap.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    rows = cases.cases()
    prior = None
    if args.only_failed:
        prior = json.loads(args.only_failed.read_text())
        wanted = {r["id"] for r in prior["failures"]}
        assert wanted and prior["probe_sha256"] == hashlib.sha256(args.probe.read_bytes()).hexdigest()
        rows = [r for r in rows if r["id"] in wanted]
        assert len(rows) == len(wanted)
    schemas = load_validators()
    data = "".join(json.dumps(r["fixture"], ensure_ascii=False)+"\n" for r in rows)
    (args.output / "cases.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2)+"\n")
    (args.output / "probe-input.jsonl").write_text(data)
    run = subprocess.run([str(args.probe.resolve())], input=data, capture_output=True,
                         text=True, timeout=60)
    (args.output / "native.jsonl").write_text(run.stdout)
    (args.output / "native-stderr.log").write_text(run.stderr)
    assert run.returncode == 0 and not run.stderr, "native probe failed"
    native = [json.loads(line) for line in run.stdout.splitlines()]
    assert len(native) == len(rows), "missing native results"
    records, failures = [], []
    for row, actual in zip(rows, native):
        shape, error = True, None
        try:
            value = strict_json(row["fixture"]["raw"])
            schemas[row["fixture"].get("kind", "chat")].validate(value)
        except (ValueError, ValidationError) as problem:
            shape, error = False, str(problem).splitlines()[0]
        expected = row["expected"]
        okay = actual["accepted"] == expected and (not expected or shape) and (
            row["shape_expected"] is None or shape == row["shape_expected"])
        record = dict(id=row["id"], field=row["field"], expected=expected,
            shape_accepted=shape, shape_error=error, native=actual, passed=okay)
        records.append(record)
        if not okay: failures.append(record)
    fields = {row["field"] for row in rows if row["field"]}
    if not prior:
        assert fields == set(build_contract.chat_schema()["properties"])
    result = dict(passed=not failures, cases=len(rows), fields=len(fields),
        accepted=sum(row["expected"] for row in rows), rejected=sum(not row["expected"] for row in rows),
        failures=failures, probe_sha256=hashlib.sha256(args.probe.read_bytes()).hexdigest(),
        scope="CPU request shapes/lexemes/native semantic initialization; no tokens/GPU/model/grammar compilation")
    if prior:
        result["prior_result"] = dict(path=str(args.only_failed.resolve()),
            sha256=hashlib.sha256(args.only_failed.read_bytes()).hexdigest(),
            unchanged_native_passes_reused=prior["cases"]-len(prior["failures"]))
    (args.output / "comparisons.json").write_text(json.dumps(records, ensure_ascii=False, indent=2)+"\n")
    (args.output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n")
    print(json.dumps({k:v for k,v in result.items() if k != "failures"}))
    for failure in failures: print(json.dumps(failure, ensure_ascii=False))
    raise SystemExit(bool(failures))


if __name__ == "__main__":
    main()
