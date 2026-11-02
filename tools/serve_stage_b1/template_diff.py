"""CPU native/Jinja byte and token-ID comparison for the frozen role profile."""
import argparse
import copy
import hashlib
import importlib.metadata
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]
MODEL = Path.home() / "models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream"
PROFILE = ROOT / "docs/serve-message-profile-v2.schema.json"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, value):
    with path.open("x") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write("\n")


def negative_cases():
    user = dict(role="user", content="只回复数字710003。")
    developer = dict(role="developer", content="只回复数字。")
    system = dict(role="system", content="只回复数字。")
    return [
        ("mixed-system-first", [system, developer, user], "messages[1].role"),
        ("mixed-developer-first", [developer, system, user], "messages[1].role"),
        ("repeated-developer", [developer, developer, user], "messages[1].role"),
        ("repeated-system", [system, system, user], "messages[1].role"),
        ("late-developer", [user, developer], "messages[1].role"),
        ("late-system", [user, system], "messages[1].role"),
        ("user-name", [dict(user, name="alice")], "messages[0].name"),
        ("empty-name", [dict(user, name="")], "messages[0].name"),
        ("developer-media", [dict(developer, content=[dict(type="image_url",
             image_url=dict(url="invalid-base64"))]), user], "messages[0].content[0]"),
        ("tool-name", [user, dict(role="tool", content="x", name="lookup")],
         "messages[1].name"),
        ("unknown-role", [dict(role="function", content="x")], "messages[0].role"),
        ("unknown-instruction-part", [dict(developer, content=[dict(text="x",
             unknown=True)]), user], "messages[0].content[0]")]


def cases():
    legacy = ROOT / "tools/evalscope/fixtures/chat_template_cases.jsonl"
    rows = [json.loads(line) for line in legacy.read_text().splitlines()]
    result = []
    for row in rows:
        result.append(dict(row, legacy=True))
        adapted = copy.deepcopy(row)
        adapted.pop("expected")
        adapted["id"] = "developer-" + row["id"]
        messages = adapted["request"]["messages"]
        if messages[0]["role"] == "system":
            messages[0]["role"] = "developer"
        else:
            messages.insert(0, dict(role="developer", content="遵循用户请求。"))
            adapted["contents"].insert(0, "遵循用户请求。")
        result.append(adapted)
    for name, value, rendered in [
            ("null", None, ""), ("empty", "", ""),
            ("parts", [dict(type="text", text="\u3000中文"),
                       dict(text=" e\u0301 🚀\u00a0")], "\u3000中文 e\u0301 🚀\u00a0")]:
        result.append(dict(id="developer-" + name, request=dict(messages=[
            dict(role="developer", content=value, name=None),
            dict(role="user", content="问", name=None)], enable_thinking=False),
            contents=[rendered, "问"]))
    calls = [dict(id=key, type="function", function=dict(name=key,
             arguments="{}")) for key in ("first", "second")]
    history = [dict(role="developer", content="读取资料。"),
               dict(role="user", content="读两份。"),
               dict(role="assistant", content=None, reasoning_content="读取。",
                    tool_calls=calls),
               dict(role="tool", content="second结果", tool_call_id="second"),
               dict(role="tool", content="first结果", tool_call_id="first")]
    reference = copy.deepcopy(history)
    reference[3], reference[4] = reference[4], reference[3]
    result.append(dict(id="developer-unordered-identified-history",
        request=dict(messages=history, enable_thinking=False, preserve_thinking=False),
        contents=["读取资料。", "读两份。", "", "second结果", "first结果"],
        reference_messages=reference))
    for label, messages, param in negative_cases():
        result.append(dict(id=label, request=dict(messages=messages),
            contents=["x"] * len(messages), reject_param=param))
    return result


def reference_request(row):
    request = copy.deepcopy(row["request"])
    messages = copy.deepcopy(row.get("reference_messages", request["messages"]))
    if messages[0]["role"] == "developer":
        messages[0]["role"] = "system"
    # The transport carries argument strings; the checkpoint consumes mappings.
    for message in messages:
        for call in message.get("tool_calls") or []:
            fn = call.get("function", call)
            if isinstance(fn.get("arguments"), str):
                fn["arguments"] = json.loads(fn["arguments"])
    options = {k: request[k] for k in ("enable_thinking", "preserve_thinking",
        "reasoning_effort", "add_vision_id") if k in request}
    options.update(request.get("chat_template_kwargs", {}))
    return messages, request.get("tools"), options


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--probe", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    out = args.output.resolve()
    assert out.is_relative_to(ROOT / ".q4t-work")
    out.mkdir(parents=True, exist_ok=False)
    from jsonschema import Draft202012Validator
    from tokenizers import Tokenizer
    from transformers.utils.chat_template_utils import render_jinja_template
    versions = {name: importlib.metadata.version(name) for name in
                ("transformers", "tokenizers", "jinja2", "jsonschema")}
    assert versions == dict(transformers="5.16.1", tokenizers="0.23.2",
                            jinja2="3.1.2", jsonschema="4.26.0"), versions
    fixtures = ROOT / "tools/evalscope/fixtures/chat_template_cases.jsonl"
    meta = json.loads(fixtures.with_suffix(".metadata.json").read_text())
    assert sha(fixtures) == meta["fixtures_sha256"]
    assert sha(MODEL / "chat_template.jinja") == meta["template_sha256"]
    assert sha(MODEL / "tokenizer.json") == meta["tokenizer_sha256"]
    schema = json.loads(PROFILE.read_text())
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    tokenizer = Tokenizer.from_file(str(MODEL / "tokenizer.json"))
    rows = cases()
    payload = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    (out / "cases.jsonl").write_text(payload)
    result = dict(passed=False, versions=versions, cases=len(rows),
        probe_sha256=sha(args.probe), schema_sha256=sha(PROFILE),
        template_sha256=meta["template_sha256"],
        tokenizer_sha256=meta["tokenizer_sha256"], performance_acceptance=False)
    try:
        run = subprocess.run([str(args.probe.resolve()), str(MODEL / "tokenizer.json")],
            input=payload, text=True, capture_output=True, timeout=120)
        (out / "native.jsonl").write_text(run.stdout)
        (out / "native.stderr").write_text(run.stderr)
        assert run.returncode == 0, run.stderr
        native = [json.loads(line) for line in run.stdout.splitlines()]
        assert len(native) == len(rows)
        comparisons = []
        for row, actual in zip(rows, native):
            valid = validator.is_valid(row["request"])
            assert actual["profile"] == valid, row["id"]
            if "reject_param" in row:
                assert not actual["contract"] and not actual["rendered"], row["id"]
                assert actual["param"] == row["reject_param"], row["id"]
                comparisons.append(dict(id=row["id"], rejected=True))
                continue
            messages, tools, options = reference_request(row)
            expected, _ = render_jinja_template([messages], tools=tools,
                chat_template=(MODEL / "chat_template.jinja").read_text(),
                add_generation_prompt=True, **options)
            prompt = expected[0]
            ids = tokenizer.encode(prompt, add_special_tokens=False).ids
            if row.get("legacy"):
                assert prompt == row["expected"], row["id"] + ": old fixture changed"
            assert actual["rendered"] and actual["contract"], row["id"]
            assert actual["prompt"] == prompt, row["id"] + ": prompt bytes differ"
            assert actual["ids"] == ids, row["id"] + ": token IDs differ"
            comparisons.append(dict(id=row["id"], passed=True, prompt=prompt, ids=ids))
        save(out / "reference.json", comparisons)
        result.update(passed=True, positives=sum("reject_param" not in r for r in rows),
                      negatives=sum("reject_param" in r for r in rows))
    except BaseException as error:
        result["error"] = repr(error)
        raise
    finally:
        save(out / "result.json", result)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
