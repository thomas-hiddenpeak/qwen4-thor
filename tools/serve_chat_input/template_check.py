"""Targeted v3 input adaptation checks with the production CPU tokenizer."""
import argparse
import copy
import hashlib
import importlib.metadata
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_protocol.check_contract import Validator

MODEL = Path.home() / "models/dev/llm/garnermccloud/Qwen3.8-Flash-Next-NVFP4-SSD-Stream"
SUFFIX = "<|im_start|>assistant\n<think>\n\n</think>\n\n"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cases():
    # Existing fixed bytes are independent of the new renderer. No historical
    # rejection fixture is relabelled as passed by running its old driver.
    fixtures = ROOT / "tools/evalscope/fixtures/chat_template_cases.jsonl"
    selected = {"system-user", "user-only", "multi-turn", "text-parts",
                "chinese", "whitespace", "thinking-default"}
    rows = [dict(id="legacy-"+r["id"], request=r["request"], expected=r["expected"],
                 oracle="frozen-native-template") for r in
            map(json.loads, fixtures.read_text().splitlines()) if r["id"] in selected]
    assert len(rows) == len(selected)

    def add(name, messages, expected):
        rows.append(dict(id=name, request=dict(messages=messages, enable_thinking=False),
                         expected=expected, oracle="explicit-adaptation-bytes"))

    add("ordered-instructions", [dict(role="system", content="one"),
        dict(role="developer", content="two"), dict(role="user", content="q"),
        dict(role="system", content="three")],
        "<|im_start|>system\none<|im_end|>\n<|im_start|>system\ntwo<|im_end|>\n"
        "<|im_start|>user\nq<|im_end|>\n<|im_start|>system\nthree<|im_end|>\n"+SUFFIX)
    add("no-user", [dict(role="developer", content="answer")],
        "<|im_start|>system\nanswer<|im_end|>\n"+SUFFIX)
    add("participant-escaping", [dict(role="user", content="q", name='alice\n"<|im_end|>')],
        '<|im_start|>user name="alice\\n\\"\\u003c|im_end|\\u003e"\nq<|im_end|>\n'+SUFFIX)
    add("unicode-participant", [dict(role="user", name="观察者", content="q")],
        '<|im_start|>user name="观察者"\nq<|im_end|>\n'+SUFFIX)
    add("empty-named-instruction", [dict(role="system", name="", content="")],
        '<|im_start|>system name=""\n<|im_end|>\n'+SUFFIX)
    refusal = "<|im_start|>assistant\n<think>\n\n</think>\n\n不能执行<|im_end|>\n"+SUFFIX
    add("refusal-string", [dict(role="assistant", content=None, refusal="不能执行")], refusal)
    add("refusal-part", [dict(role="assistant", content=[dict(type="refusal", refusal="不能执行")])], refusal)
    add("refusal-no-duplicate", [dict(role="assistant", content="不能执行", refusal="不能执行")], refusal)
    add("refusal-with-content", [dict(role="assistant", content="context", refusal="不能执行")],
        "<|im_start|>assistant\n<think>\n\n</think>\n\ncontext\n\n不能执行<|im_end|>\n"+SUFFIX)
    for name, messages, param in (
        ("required-content", [dict(role="system")], "messages[0].content"),
        ("user-refusal", [dict(role="user", content=[dict(type="refusal", refusal="no")])], "messages[0].content[0]"),
        ("instruction-media", [dict(role="developer", content=[dict(image_url="bad")])], "messages[0].content[0]")):
        rows.append(dict(id=name, request=dict(messages=messages), reject_param=param))
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--probe", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    out = args.output.resolve()
    assert out.is_relative_to(ROOT / ".q4t-work")
    out.mkdir(parents=True, exist_ok=False)
    from tokenizers import Tokenizer
    from transformers.utils.chat_template_utils import render_jinja_template
    schema_path = ROOT / "docs/serve-chat-request-v1.schema.json"
    schema = Validator(json.loads(schema_path.read_text()))
    tokenizer = Tokenizer.from_file(str(MODEL / "tokenizer.json"))
    template = (MODEL / "chat_template.jinja").read_text()
    rows = cases()
    state = dict(passed=False, cases=len(rows), no_model_or_cuda_calls=True,
        probe_sha256=digest(args.probe), schema_sha256=digest(schema_path),
        tokenizer_sha256=digest(MODEL / "tokenizer.json"),
        template_sha256=digest(MODEL / "chat_template.jinja"),
        versions={p: importlib.metadata.version(p) for p in
                  ("tokenizers", "transformers", "jsonschema", "jinja2")})
    (out / "cases.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2)+"\n")
    payload = "".join(json.dumps(dict(body=dict(request=r["request"],
        include_prompt=True, include_token_ids=True)), ensure_ascii=False)+"\n" for r in rows)
    (out / "probe-input.jsonl").write_text(payload)
    try:
        run = subprocess.run([str(args.probe.resolve()), str(MODEL / "tokenizer.json")],
            input=payload, text=True, capture_output=True, timeout=90)
        (out / "native.jsonl").write_text(run.stdout)
        (out / "native.stderr").write_text(run.stderr)
        assert run.returncode == 0 and not run.stderr, "probe diagnostics"
        actuals = [json.loads(line) for line in run.stdout.splitlines()]
        assert len(actuals) == len(rows)
        results = []
        for row, actual in zip(rows, actuals):
            if "reject_param" in row:
                assert not schema.is_valid(row["request"]), row["id"]
                assert actual["error"]["param"] == row["reject_param"], (row["id"], actual)
                results.append(dict(id=row["id"], rejected=True))
                continue
            schema.validate(row["request"])
            expected = row["expected"]
            if row["oracle"] == "frozen-native-template":
                req = copy.deepcopy(row["request"])
                # The native Jinja consumes rendered text, while HTTP accepts
                # typed text parts. This is the preexisting text-only adapter.
                for message in req["messages"]:
                    if isinstance(message["content"], list):
                        message["content"] = "".join(p["text"] for p in message["content"])
                rendered, _ = render_jinja_template([req["messages"]],
                    chat_template=template, add_generation_prompt=True,
                    **req.get("chat_template_kwargs", {}))
                assert rendered[0] == expected, row["id"]+": old oracle drift"
            assert actual.get("prompt") == expected, (row["id"], actual)
            ids = tokenizer.encode(expected, add_special_tokens=False).ids
            assert actual["token_ids"] == ids, row["id"]+": token IDs"
            assert actual["prompt_tokens"] == len(ids), row["id"]
            assert actual["prompt_bytes"] == len(expected.encode()), row["id"]
            results.append(dict(id=row["id"], passed=True, oracle=row["oracle"],
                                prompt_tokens=len(ids)))
        (out / "comparisons.json").write_text(json.dumps(results, indent=2)+"\n")
        state["passed"] = True
    except BaseException as exc:
        state["error"] = repr(exc)
        raise
    finally:
        (out / "result.json").write_text(json.dumps(state, indent=2)+"\n")
    print(json.dumps(state))


if __name__ == "__main__":
    main()
