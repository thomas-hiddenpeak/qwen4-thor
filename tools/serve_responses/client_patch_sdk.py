"""Exercise a model-issued patch in an isolated client workspace over native TLS."""
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import sys

import openai


def digest(data):
    return hashlib.sha256(data).hexdigest()


def exercise(params):
    wire = Path(params["capture"])
    wire.mkdir(mode=0o700)
    workspace = Path(params["workspace"])
    workspace.mkdir(mode=0o700)
    heading = (Path(params["project"]) / "docs/MODEL.md").read_text().splitlines()[0]
    architecture = heading.split("(", 1)[1].split(")", 1)[0]
    filename = "architecture.txt"
    line = "architecture=" + architecture
    selector = {"type": "apply_patch"}
    defaults = {"model": "qwen3.8-flash-next", "max_output_tokens": 384,
                "temperature": 0, "store": True, "tools": [selector]}
    captures = {}

    def keep(name, data):
        (wire / name).write_bytes(data)
        captures[name] = digest(data)

    def reject(operation, error_type, status):
        try:
            operation()
        except error_type as error:
            assert error.status_code == status
        else:
            raise AssertionError(f"Request should have returned HTTP {status}")

    with ExitStack() as stack:
        def connect(key):
            return stack.enter_context(openai.OpenAI(
                base_url=params["base"], api_key=key, max_retries=0, timeout=115,
                http_client=openai.DefaultHttpxClient(verify=params["certificate"])))

        owner, peer = connect(params["key"]), connect(params["other"])
        prompt = (f"Create the project architecture record in the client workspace. Call "
                  f"the apply_patch tool exactly once, with type=create_file, path={filename}, "
                  f"and diff consisting of exactly this one addition line: +{line}.")
        with owner.responses.create(**defaults, input=prompt, tool_choice=selector,
                                    stream=True) as stream:
            frames = [event.model_dump(mode="json", exclude_unset=True) for event in stream]
        keep("events.json", json.dumps(frames, ensure_ascii=False).encode())
        assert frames[-1]["type"] == "response.completed"
        response = frames[-1]["response"]
        calls = [item for item in response["output"] if item["type"] == "apply_patch_call"]
        assert len(calls) == 1 and calls[0]["status"] == "completed"
        call = calls[0]
        assert call["id"] and call["call_id"] != call["id"] and call["call_id"]
        assert call.get("caller") in (None, {"type": "direct"})
        stages = []
        for kind in ("response.output_item.added", "response.output_item.done"):
            items = [f["item"] for f in frames if f["type"] == kind
                     and f["item"]["type"] == "apply_patch_call"]
            assert len(items) == 1
            stages.append(items[0])
        assert stages[0]["id"] == call["id"] and stages[0]["operation"] == call["operation"]
        assert stages[1] == call
        assert not any(f["type"].startswith("response.function_call_arguments") for f in frames)
        op = call["operation"]
        assert set(op) == {"type", "path", "diff"}
        assert op["type"] == "create_file" and op["path"] == filename
        assert op["diff"].splitlines() == ["+" + line], op
        target = workspace / filename
        assert list(workspace.iterdir()) == []
        with target.open("xb") as output:
            output.write((op["diff"].splitlines()[0][1:] + "\n").encode())
        actual = target.read_bytes()
        assert actual == (line + "\n").encode()
        receipt = digest(actual)[:12]
        outcome = {"type": "apply_patch_call_output", "status": "completed",
                   "call_id": call["call_id"], "output": "Receipt: " + receipt}
        follow = dict(defaults, previous_response_id=response["id"], tool_choice="none",
                      instructions="Return the exact receipt from the patch tool result.",
                      text={"format": {"type": "json_schema", "name": "patch_receipt",
                            "strict": True, "schema": {"type": "object", "properties": {
                                "receipt": {"type": "string"}}, "required": ["receipt"],
                                "additionalProperties": False}}})
        reject(lambda: owner.responses.create(**follow, input=[dict(outcome, call_id="unknown")]),
               openai.BadRequestError, 400)
        continued = owner.responses.with_raw_response.create(**follow, input=[outcome])
        keep("follow.json", continued.http_response.content)
        answer = continued.parse()
        assert answer.status == "completed" and json.loads(answer.output_text) == {"receipt": receipt}
        for label, ident in (("call", response["id"]), ("completed", answer.id)):
            raw = owner.responses.with_raw_response.retrieve(ident)
            keep(label + ".json", raw.http_response.content)
            value = raw.http_response.json()
            assert value["id"] == ident and value["status"] == "completed"
            if label == "call":
                assert [i for i in value["output"] if i["type"] == call["type"]] == [call]
        page = owner.responses.input_items.with_raw_response.list(answer.id, order="asc", limit=100)
        keep("inputs.json", page.http_response.content)
        saved = [i for i in page.http_response.json()["data"] if i["type"] == outcome["type"]]
        assert len(saved) == 1 and all(saved[0].get(k) == v for k, v in outcome.items())
        reject(lambda: peer.responses.retrieve(response["id"]), openai.NotFoundError, 404)
        reject(lambda: peer.responses.input_items.list(answer.id), openai.NotFoundError, 404)
        chat = owner.chat.completions.with_raw_response.create(
            model=defaults["model"], temperature=0, max_completion_tokens=64,
            messages=[{"role": "user", "content": "Reply only with 720041."}])
        keep("chat.json", chat.http_response.content)
        assert chat.parse().choices[0].message.content.strip() == "720041"

    return dict(sdk=openai.__version__, response=response["id"], follow=answer.id,
                item=call["id"], call_id=call["call_id"], writes=1, operation=op,
                content_sha256=digest(actual), answer=receipt, captures=captures,
                events=len(frames), wrong_target=400, peer=404, independent_chat="720041")


if __name__ == "__main__":
    print(json.dumps(exercise(json.load(sys.stdin))))
