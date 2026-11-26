"""Verify a client-owned Responses shell turn against a real project document."""
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import openai


def program_turn(owner, peer, project, defaults, keep, reject):
    argv = ["grep", "-cF", "q4t_acceptance_absent_marker_173", "docs/MODEL.md"]
    command = " ".join(argv)
    selector = {"type": "programmatic_tool_calling"}
    options = dict(defaults, max_output_tokens=384, tools=[selector, {
        "type": "shell", "environment": {"type": "local"}, "allowed_callers": ["programmatic"]}])
    code = ('const r=await tools.shell({commands:[' + json.dumps(command) +
            ']}); text("exit="+r.output[0].outcome.exit_code+" count="+r.output[0].stdout.trim());')
    raw = owner.responses.with_raw_response.create(
        **options, tool_choice=selector, parallel_tool_calls=False, max_tool_calls=1,
        input="Run this exact JavaScript program and await the client's result: " + code)
    keep("program-pending.json", raw.http_response.content)
    original = raw.http_response.json()
    (parent,) = [item for item in original["output"] if item["type"] == "program"]
    (child,) = [item for item in original["output"] if item["type"] == "shell_call"]
    assert original["status"] == "completed" and parent["fingerprint"]
    assert child["status"] == "completed" and child["caller"] == {
        "type": "program", "caller_id": parent["call_id"]}
    assert child["action"] == {"commands": [command], "timeout_ms": None, "max_output_length": None}
    assert child["environment"] == {"type": "local"}
    assert original["output"].index(parent) < original["output"].index(child)
    assert not any(item["type"] == "program_output" for item in original["output"])
    result = subprocess.run(argv, cwd=project, capture_output=True, timeout=3)
    assert (result.returncode, result.stdout.strip(), result.stderr) == (1, b"0", b"")
    outcome = dict(type="shell_call_output", status="completed", call_id=child["call_id"],
                   caller=child["caller"], output=[dict(stdout=result.stdout.decode(), stderr="",
                   outcome=dict(type="exit", exit_code=result.returncode))])
    follow = dict(options, previous_response_id=original["id"], tool_choice="none",
                  instructions="Copy the program result verbatim into the result field.",
                  text={"format": {"type": "json_schema", "name": "program_result", "strict": True,
                        "schema": {"type": "object", "properties": {"result": {"type": "string"}},
                                   "required": ["result"], "additionalProperties": False}}})
    wrong = dict(outcome, caller={"type": "program", "caller_id": "other"})
    reject(lambda: owner.responses.create(**follow, input=[wrong]), openai.BadRequestError, 400)
    raw = owner.responses.with_raw_response.create(**follow, input=[outcome])
    keep("program-follow.json", raw.http_response.content)
    answer = raw.parse()
    (output,) = [item for item in raw.http_response.json()["output"] if item["type"] == "program_output"]
    expected = f"exit={result.returncode} count={result.stdout.decode().strip()}"
    assert output["call_id"] == parent["call_id"] and output["status"] == "completed"
    assert output["result"] == expected and json.loads(answer.output_text) == {"result": expected}
    raw = owner.responses.with_raw_response.retrieve(answer.id)
    keep("program-stored.json", raw.http_response.content)
    assert raw.parse().id == answer.id
    page = owner.responses.input_items.with_raw_response.list(answer.id, order="asc", limit=100)
    keep("program-inputs.json", page.http_response.content)
    (saved,) = [item for item in page.http_response.json()["data"] if item["type"] == outcome["type"]]
    assert all(saved.get(key) == value for key, value in outcome.items())
    reject(lambda: peer.responses.retrieve(original["id"]), openai.NotFoundError, 404)
    return dict(response=original["id"], follow=answer.id, parent=parent["call_id"],
                child=child["call_id"], answer=expected, exit_code=result.returncode, peer=404, wrong_parent=400)


def exercise(params):
    project = Path(params["project"])
    wire = Path(params["capture"])
    wire.mkdir(mode=0o700)
    captures = {}

    def keep(name, data):
        (wire / name).write_bytes(data)
        captures[name] = hashlib.sha256(data).hexdigest()

    def reject(operation, error_type, status):
        try:
            operation()
        except error_type as error:
            assert error.status_code == status
        else:
            raise AssertionError(f"expected HTTP {status}")

    argv = ["head", "-n", "1", "docs/MODEL.md"]
    command = " ".join(argv)
    defaults = dict(model="qwen3.8-flash-next", temperature=0, store=True,
                    max_output_tokens=256, tools=[{"type": "shell", "environment": {"type": "local"}}])
    with ExitStack() as stack:
        def connect(key):
            http = openai.DefaultHttpxClient(verify=params["certificate"])
            return stack.enter_context(openai.OpenAI(base_url=params["base"], api_key=key,
                                                     max_retries=0, timeout=115, http_client=http))
        owner, peer = connect(params["key"]), connect(params["other"])
        choice = {"type": "shell"}
        prompt = (f'Use the local shell tool once. Set commands to ["{command}"], '
                  'timeout_ms to 3000 and max_output_length to 512. '
                  'The client will return the document heading.')
        with owner.responses.create(**defaults, input=prompt, tool_choice=choice, stream=True) as stream:
            events = [event.model_dump(mode="json", exclude_unset=True) for event in stream]
        keep("events.json", json.dumps(events, ensure_ascii=False).encode())
        assert events[-1]["type"] == "response.completed"
        response = events[-1]["response"]
        assert response["tool_choice"] == choice
        (child,) = [i for i in response["output"] if i["type"] == "shell_call"]
        assert child["status"] == "completed" and child["environment"] == {"type": "local"}
        assert child["caller"] == {"type": "direct"} and child["call_id"] != child["id"]
        assert child["action"] == {"commands": [command], "timeout_ms": 3000, "max_output_length": 512}
        assert [e["sequence_number"] for e in events] == list(range(len(events)))
        commands = [e for e in events if e["type"].startswith("response.shell_call_command.")]
        assert [e["type"].rsplit(".", 1)[1] for e in commands] == ["added", "delta", "done"]
        assert all(e["command_index"] == 0 and
                   response["output"][e["output_index"]]["id"] == child["id"] for e in commands)
        assert commands[0]["command"] + commands[1]["delta"] == commands[2]["command"] == command
        assert not any(e["type"].startswith("response.function_call_arguments.") for e in events)
        # This client supports one literal read, with a fixed argv and no shell interpreter.
        result = subprocess.run(argv, cwd=project, capture_output=True, timeout=3, check=True)
        stdout = result.stdout.decode()
        assert stdout == (project / "docs/MODEL.md").read_text().splitlines(keepends=True)[0]
        expected = stdout.split("(", 1)[1].split(")", 1)[0]
        outcome = dict(type="shell_call_output", call_id=child["call_id"], status="completed",
                       max_output_length=child["action"]["max_output_length"],
                       output=[dict(stdout=stdout, stderr=result.stderr.decode(),
                                    outcome=dict(type="exit", exit_code=result.returncode))])
        follow = dict(defaults, previous_response_id=response["id"], tool_choice="none",
                      instructions="Return the exact identifier inside parentheses in the shell output.",
                      text={"format": {"type": "json_schema", "name": "project_record", "strict": True,
                            "schema": {"type": "object", "properties": {"architecture": {"type": "string"}},
                                       "required": ["architecture"], "additionalProperties": False}}})
        reject(lambda: owner.responses.create(**follow, input=[dict(outcome, call_id="unknown_target")]),
               openai.BadRequestError, 400)
        raw = owner.responses.with_raw_response.create(**follow, input=[outcome])
        keep("follow.json", raw.http_response.content)
        answer = raw.parse()
        assert answer.status == "completed" and json.loads(answer.output_text) == {"architecture": expected}
        for label, ident in (("call", response["id"]), ("completed", answer.id)):
            raw = owner.responses.with_raw_response.retrieve(ident)
            keep(label + ".json", raw.http_response.content)
            value = raw.http_response.json()
            assert value["id"] == ident and value["status"] == "completed"
            if label == "call":
                assert [i for i in value["output"] if i["type"] == "shell_call"] == [child]
        page = owner.responses.input_items.with_raw_response.list(answer.id, order="asc", limit=100)
        keep("inputs.json", page.http_response.content)
        (saved,) = [i for i in page.http_response.json()["data"] if i["type"] == "shell_call_output"]
        assert all(saved.get(k) == v for k, v in outcome.items())
        reject(lambda: peer.responses.retrieve(response["id"]), openai.NotFoundError, 404)
        reject(lambda: peer.responses.input_items.list(answer.id), openai.NotFoundError, 404)
        program = program_turn(owner, peer, project, defaults, keep, reject)
        chat = owner.chat.completions.with_raw_response.create(
            model=defaults["model"], temperature=0, max_completion_tokens=64,
            messages=[{"role": "user", "content": "Reply only with 720041."}])
        keep("chat.json", chat.http_response.content)
        assert chat.parse().choices[0].message.content.strip() == "720041"
    return dict(sdk=openai.__version__, response=response["id"], follow=answer.id,
                item=child["id"], call_id=child["call_id"], reads=1, action=child["action"],
                document_sha256=hashlib.sha256(result.stdout).hexdigest(), answer=expected,
                captures=captures, events=len(events), program=program, wrong_target=400, peer=404, independent_chat="720041")


if __name__ == "__main__":
    print(json.dumps(exercise(json.load(sys.stdin))))
