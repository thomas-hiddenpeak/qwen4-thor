"""Compare production history planning with the frozen checkpoint corpus."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import subprocess

from tokenizers import Tokenizer
from transformers.utils.chat_template_utils import render_jinja_template

from tools.serve_preflight.template_diff import reference

FIXTURES = (Path(__file__).resolve().parents[2] / "tools/evalscope/fixtures/"
            "chat_template_cases.jsonl")
SYSTEM = "<|im_start|>system\n"
END = "<|im_end|>\n"
REFUSAL = ("# Response encoding\n\nReturn ordinary answers in the requested text, "
           "JSON, or tool format. Only when you choose to refuse the task, "
           "output <q4t_refusal> followed by your refusal and </q4t_refusal>, "
           "with no other final-answer content. This is a formatting rule, "
           "not a reason to refuse.")
ARGUMENTS = (" Supply the complete JSON argument object in the single "
             "arguments parameter. Preserve null, numbers and property names; "
             "encode each less-than sign as \\u003c.")


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def render(template, messages, tools, options):
    prompts, _ = render_jinja_template([messages], tools=tools,
        chat_template=template, add_generation_prompt=True, **options)
    return prompts[0]


def expected_prompt(template, row, parsed):
    messages, tools, options = reference.reference_request(row)
    trained = render(template, messages, tools, options)
    assert trained == row["expected"], row["id"]
    if parsed:
        # The public API's default effort is medium. The checkpoint's is xhigh.
        options.setdefault("reasoning_effort", "medium")
    if tools:
        # The five frozen tool histories use the lossless JSON arguments format.
        # Construct its data independently, letting Jinja render the turn order.
        for tool in tools:
            function = tool["function"]
            schema = function["parameters"]
            function["description"] += ARGUMENTS
            function["parameters"] = dict(type="object",
                properties=dict(arguments=schema), required=["arguments"],
                additionalProperties=False)
        for message in messages:
            for call in message.get("tool_calls", []):
                function = call["function"]
                data = compact(function["arguments"]).replace("<", "\\u003c")
                function["arguments"] = {"arguments": data}
    prompt = render(template, messages, tools, options)
    if tools:
        prefix, marker, tail = prompt.partition("<tools>\n")
        definitions, end, history = tail.partition("\n</tools>")
        assert marker and end and list(map(json.loads, definitions.splitlines())) == tools
        prompt = prefix + marker + "\n".join(map(compact, tools)) + end + history
    if parsed:
        # Insert the API refusal instructions after the checkpoint's own
        # reasoning guidance and before tools or user-provided system content.
        guidance = render(template, [dict(role="user", content="q")], None, options)
        if guidance.startswith(SYSTEM):
            instructions = guidance[len(SYSTEM):guidance.index(END)]
            start = SYSTEM + instructions
            assert prompt.startswith(start)
            prompt = start + "\n\n" + REFUSAL + prompt[len(start):]
        elif prompt.startswith(SYSTEM):
            prompt = SYSTEM + REFUSAL + "\n\n" + prompt[len(SYSTEM):]
        else:
            prompt = SYSTEM + REFUSAL + END + prompt
    return trained, prompt


def compare(probe, model):
    meta = json.loads(FIXTURES.with_suffix(".metadata.json").read_bytes())
    assets = dict(fixtures=FIXTURES, template=model/"chat_template.jinja",
                  tokenizer=model/"tokenizer.json")
    for key, path in assets.items():
        assert digest(path) == meta[key+"_sha256"], path
    tokenizer = Tokenizer.from_file(str(assets["tokenizer"]))
    template = assets["template"].read_text()
    inputs, expected = [], []
    for raw in FIXTURES.read_bytes().splitlines():
        row = json.loads(raw)
        for parsed in (False, True):
            request = copy.deepcopy(row["request"])
            if not parsed:
                request["parse_response"] = False
                request.setdefault("chat_template_kwargs", {}).setdefault(
                    "reasoning_effort", "xhigh")
            trained, prompt = expected_prompt(template, row, parsed)
            assert len(tokenizer.encode(trained, add_special_tokens=False).ids) == row["prompt_tokens"]
            inputs.append(dict(body=dict(request=request, include_prompt=True,
                                         include_token_ids=True)))
            expected.append((row["id"], parsed, prompt, trained))
    run = subprocess.run([probe.resolve(), assets["tokenizer"]],
        input="".join(json.dumps(value)+"\n" for value in inputs),
        text=True, capture_output=True, timeout=120, check=True)
    results = []
    for (name, parsed, prompt, trained), line in zip(expected, run.stdout.splitlines(), strict=True):
        actual = json.loads(line)
        ids = tokenizer.encode(prompt, add_special_tokens=False).ids
        assert actual.get("prompt") == prompt, (name, parsed, "prompt", actual.get("error"))
        assert actual["token_ids"] == ids and actual["prompt_tokens"] == len(ids), (name, parsed, "tokens")
        assert actual["prompt_bytes"] == len(prompt.encode()), (name, parsed, "bytes")
        results.append(dict(case=name, parsed=parsed, tokens=len(ids),
            checkpoint_bytes=trained == prompt, sha256=hashlib.sha256(prompt.encode()).hexdigest()))
    return dict(cases=meta["cases"], comparisons=len(results), rows=results,
                sources={key:digest(path) for key,path in assets.items()},
                probe_sha256=digest(probe))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(compare(args.probe, args.model_dir), indent=2))
