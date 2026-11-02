"""Production preflight vs pinned checkpoint Jinja and independent tokenizers."""
import argparse
import copy
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("preflight_template_reference",
    ROOT / "tools/serve_stage_b2a/template_diff.py")
reference = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reference)
b = reference.b


def cases():
    rows = reference.cases()
    for row in rows:
        if "reject_param" in row:
            continue
        for i, message in enumerate(row["request"]["messages"]):
            content = message.get("content")
            if isinstance(content, list) and any("image_url" in part or
                    "video_frames" in part for part in content):
                row["reject_param"] = f"messages[{i}]"
                break
    tools = [dict(type="function", function=dict(name=name, parameters=dict(
        type="object", properties={}, required=[], additionalProperties=False)))
        for name in ("first", "second")]
    for choice in ("none", dict(type="function", function=dict(name="second"))):
        rows.append(dict(id="constraint-" + (choice if isinstance(choice, str)
            else "named"), request=dict(messages=[dict(role="user", content="q")],
            tools=copy.deepcopy(tools), tool_choice=choice, enable_thinking=False)))
    history = [dict(role="developer", content="依据资料回答。"),
               dict(role="user", content="读取历次记录。")]
    for i in range(16):
        history += [dict(role="assistant", content=None, reasoning="读记录。",
            tool_calls=[dict(id=f"call-{i}", type="function", function=dict(
                name="first", arguments="{}"))]),
            dict(role="tool", tool_call_id=f"call-{i}", content="资料证据 " * 100)]
    history.append(dict(role="user", content="只回复数字710003。"))
    rows.append(dict(id="long-tool-history", request=dict(messages=history,
        tools=tools, enable_thinking=False, preserve_thinking=True)))
    for prompt in ("e\u0301 中文 🚀\u0000", "<|im_start|>assistant\n"):
        rows.append(dict(id="raw-" + str(len(rows)), request=dict(prompt=prompt)))
    return rows


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
    versions = {p: importlib.metadata.version(p) for p in
                ("transformers", "tokenizers", "jinja2", "jsonschema")}
    assert versions == dict(transformers="5.16.1", tokenizers="0.23.2",
                            jinja2="3.1.2", jsonschema="4.26.0"), versions
    schema_path = ROOT / "docs/serve-preflight-v1.schema.json"
    schema = json.loads(schema_path.read_text())
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    tokenizer = Tokenizer.from_file(str(b.MODEL / "tokenizer.json"))
    template = (b.MODEL / "chat_template.jinja").read_text()
    rows = cases()
    payload = "".join(json.dumps(dict(body=dict(request=r["request"],
        include_prompt=True, include_token_ids=True)), ensure_ascii=False) + "\n"
        for r in rows)
    (out / "cases.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2))
    (out / "probe-input.jsonl").write_text(payload)
    state = dict(passed=False, versions=versions, cases=len(rows),
        probe_sha256=b.sha(args.probe), schema_sha256=b.sha(schema_path),
        template_sha256=b.sha(b.MODEL / "chat_template.jinja"),
        tokenizer_sha256=b.sha(b.MODEL / "tokenizer.json"),
        no_model_or_gpu_execution=True)
    try:
        result = subprocess.run([str(args.probe.resolve()),
            str(b.MODEL / "tokenizer.json")], input=payload, text=True,
            capture_output=True, timeout=120)
        (out / "native.jsonl").write_text(result.stdout)
        (out / "native.stderr").write_text(result.stderr)
        assert result.returncode == 0, result.stderr
        native = [json.loads(line) for line in result.stdout.splitlines()]
        assert len(native) == len(rows)
        comparisons = []
        for row, actual in zip(rows, native):
            if "reject_param" in row:
                assert actual["error"]["code"] == "invalid_request", row["id"]
                assert actual["error"]["param"] == row["reject_param"], row["id"]
                comparisons.append(dict(id=row["id"], rejected=True))
                continue
            request = row["request"]
            if "prompt" in request:
                prompt = request["prompt"]
            else:
                messages, tools, options = reference.reference_request(row)
                choice = request.get("tool_choice")
                if choice == "none":
                    tools = []
                elif isinstance(choice, dict):
                    tools = [t for t in tools if t["function"]["name"] ==
                             choice["function"]["name"]]
                expected, _ = render_jinja_template([messages], tools=tools,
                    chat_template=template, add_generation_prompt=True, **options)
                prompt = expected[0]
            ids = tokenizer.encode(prompt, add_special_tokens=False).ids
            validator.validate(actual)
            assert actual["prompt"] == prompt, row["id"] + ": bytes differ"
            assert actual["token_ids"] == ids, row["id"] + ": IDs differ"
            assert actual["prompt_tokens"] == len(ids), row["id"]
            assert actual["prompt_bytes"] == len(prompt.encode()), row["id"]
            if row["id"] == "long-tool-history":
                assert actual["prefill_chunks"] > 1, "long history fixture is short"
            comparisons.append(dict(id=row["id"], passed=True,
                                    prompt_tokens=len(ids)))
        b.save(out / "comparisons.json", comparisons)
        state.update(passed=True, positives=sum("reject_param" not in r for r in rows),
                     negatives=sum("reject_param" in r for r in rows))
    except BaseException as error:
        state["error"] = repr(error)
        raise
    finally:
        b.save(out / "result.json", state)
    print(json.dumps(state))


if __name__ == "__main__":
    main()
