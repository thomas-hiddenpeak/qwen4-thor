"""Exercise the private project's read-only local shell client with the OpenAI SDK."""
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import openai


def exercise(params):
    work = Path(params["capture"])
    work.mkdir(mode=0o700)
    project = Path(params["project"])
    argv = ["head", "-n", "1", "docs/MODEL.md"]
    sdk = {"model": "qwen3.8-flash-next", "max_output_tokens": 256,
           "temperature": 0, "store": True, "tools": [{"type": "local_shell"}]}
    with ExitStack() as stack:
        def client(key):
            return stack.enter_context(openai.OpenAI(
                base_url=params["base"], api_key=key, max_retries=0, timeout=115,
                http_client=openai.DefaultHttpxClient(verify=params["certificate"])))

        owner, other = client(params["key"]), client(params["other"])
        prompt = ("Request exactly one local_shell action. Set command to "
                  '["head","-n","1","docs/MODEL.md"] and env to {}. '
                  "After the tool result, report the identifier inside the parentheses.")
        stream = owner.responses.create(**sdk, input=prompt, tool_choice={"type": "local_shell"},
                                        stream=True)
        with stream as events:
            frames = [event.model_dump(mode="json", exclude_unset=True) for event in events]
        (work / "events.json").write_text(json.dumps(frames, ensure_ascii=False))
        assert frames[-1]["type"] == "response.completed"
        response = frames[-1]["response"]
        calls = [i for i in response["output"] if i["type"] == "local_shell_call"]
        assert len(calls) == 1 and calls[0]["status"] == "completed"
        call = calls[0]
        assert call["id"] != call["call_id"]
        opened = [e["item"] for e in frames if e["type"] == "response.output_item.added"
                  and e["item"]["type"] == "local_shell_call"]
        closed = [e["item"] for e in frames if e["type"] == "response.output_item.done"
                  and e["item"]["type"] == "local_shell_call"]
        assert len(opened) == 1 and opened[0]["id"] == call["id"] and closed == [call]
        assert not any(e["type"].startswith("response.function_call_arguments") for e in frames)
        action = call["action"]
        assert action["type"] == "exec" and action["command"] == argv and action["env"] == {}
        assert not action.get("user") and not action.get("working_directory")
        # The acceptance client implements precisely this project read. Execute
        # the fixed argv only after matching the complete requested operation.
        receipt = subprocess.run(argv, cwd=project, capture_output=True, check=True, timeout=3)
        header = receipt.stdout.decode().strip()
        assert header == (project / "docs/MODEL.md").read_text().splitlines()[0]
        expected = header.split("(", 1)[1].split(")", 1)[0]
        tool_result = {"type": "local_shell_call_output", "id": call["call_id"], "output": header}
        follow = dict(sdk, previous_response_id=response["id"], tool_choice="none",
                      instructions="Return the exact identifier inside parentheses from the tool output.",
                      text={"format": {"type": "json_schema", "name": "project_record",
                            "strict": True, "schema": {"type": "object", "properties": {
                                "architecture": {"type": "string"}}, "required": ["architecture"],
                                "additionalProperties": False}}})
        try:
            owner.responses.create(**follow, input=[dict(tool_result, call_id="unknown_target")])
        except openai.BadRequestError as error:
            assert error.status_code == 400
        else:
            raise AssertionError("an unrelated shell result advanced the turn")
        followup = owner.responses.with_raw_response.create(**follow, input=[tool_result])
        (work / "follow.json").write_bytes(followup.http_response.content)
        answer = followup.parse()
        assert answer.status == "completed" and json.loads(answer.output_text) == {
            "architecture": expected}
        raw = owner.responses.input_items.with_raw_response.list(answer.id, order="asc", limit=100)
        (work / "inputs.json").write_bytes(raw.http_response.content)
        saved = [i for i in raw.http_response.json()["data"] if i["type"] == tool_result["type"]]
        assert saved == [dict(tool_result, call_id=call["call_id"])]
        for read in (lambda: other.responses.retrieve(response["id"]),
                     lambda: other.responses.input_items.list(answer.id)):
            try:
                read()
            except openai.NotFoundError as error:
                assert error.status_code == 404
            else:
                raise AssertionError("another client saw the local shell history")
        chat = owner.chat.completions.create(model=sdk["model"], temperature=0,
            max_completion_tokens=64, messages=[{"role": "user", "content": "Reply only with 720041."}])
        independent = chat.choices[0].message.content.strip()
        assert independent == "720041"
    return dict(sdk=openai.__version__, response=response["id"], follow=answer.id,
                item=call["id"], call_id=call["call_id"], reads=1, peer=404, wrong_target=400,
                answer=expected, independent_chat=independent, events=len(frames),
                document_sha256=hashlib.sha256(receipt.stdout).hexdigest(),
                captures={p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in work.iterdir()})


if __name__ == "__main__":
    print(json.dumps(exercise(json.load(sys.stdin))))
