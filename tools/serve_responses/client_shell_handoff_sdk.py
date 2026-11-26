"""Reopen shell resources and resume a client command against a changed private document."""
from contextlib import ExitStack
import subprocess
import hashlib
import json
from pathlib import Path
import sys

import openai


def exercise(params):
    directory = Path(params["capture"])
    directory.mkdir(mode=0o700)
    direct, program = params["original"], params["program"]
    hashes = {}

    def capture(label, raw):
        data = raw.http_response.content
        raw.http_response.raise_for_status()
        (directory / (label + ".json")).write_bytes(data)
        hashes[label] = hashlib.sha256(data).hexdigest()
        return raw.http_response.json()

    def private(operation):
        try:
            operation()
        except openai.NotFoundError as error:
            assert error.status_code == 404
        else:
            raise AssertionError("Another client could read a shell resource")

    with ExitStack() as stack:
        def connect(key):
            return stack.enter_context(openai.OpenAI(
                base_url=params["base"], api_key=key, max_retries=0, timeout=95,
                http_client=openai.DefaultHttpxClient(verify=params.get("certificate") or True)))

        owner, other = connect(params["key"]), connect(params["other"])
        api = owner.responses
        for prefix, proof, filenames in (
                ("direct", direct, ("call.json", "completed.json", "inputs.json")),
                ("program", program, ("program-pending.json", "program-stored.json", "program-inputs.json"))):
            for key, label, filename in zip(("response", "follow"), ("call", "completed"), filenames[:2], strict=True):
                name = prefix + "_" + label
                row = capture(name, api.with_raw_response.retrieve(proof[key]))
                assert row["id"] == proof[key] and hashes[name] == direct["captures"][filename]
                private(lambda: other.responses.retrieve(proof[key]))
                if prefix == "program" and label == "call":
                    (child,) = [i for i in row["output"] if i["type"] == "shell_call"]
            name = prefix + "_inputs"
            page = capture(name, api.input_items.with_raw_response.list(proof["follow"], order="asc", limit=100))
            assert hashes[name] == direct["captures"][filenames[2]]
            (original,) = [item for item in page["data"] if item["type"] == "shell_call_output"]
            if prefix == "direct":
                assert original["call_id"] == direct["call_id"]
                heading = original["output"][0]["stdout"].encode()
                assert hashlib.sha256(heading).hexdigest() == direct["document_sha256"]
            else:
                caller = {"type": "program", "caller_id": program["parent"]}
                assert child["caller"] == original["caller"] == caller
                assert child["call_id"] == original["call_id"] == program["child"]
                assert original["output"][0]["outcome"] == {"type": "exit", "exit_code": 1}
        marker = "q4t_acceptance_absent_marker_173"
        argv = ["grep", "-cF", marker, "docs/MODEL.md"]
        assert child["action"]["commands"] == [" ".join(argv)] and child["environment"] == {"type": "local"}
        work = directory.parent / (directory.name + "-workspace")
        work.mkdir(mode=0o700)
        (work / "docs").mkdir()
        # Only this client-owned copy changes. The original pending call must
        # consume a different result when the same command is answered again.
        (work / argv[-1]).write_bytes(heading + (marker + "\n").encode()*2)
        actual = subprocess.run(argv, cwd=work, capture_output=True, timeout=3, check=True)
        assert actual.stdout == b"2\n" and not actual.stderr
        reply = dict(type="shell_call_output", status="completed", call_id=program["child"],
                     caller=caller, output=[dict(stdout=actual.stdout.decode(), stderr="",
                     outcome=dict(type="exit", exit_code=actual.returncode))])
        expected = f"exit={actual.returncode} count={actual.stdout.decode().strip()}"
        assert expected != program["answer"]

        def verify(ident, label):
            raw = api.with_raw_response.retrieve(ident)
            record = capture(label + "_response", raw)
            assert record["previous_response_id"] == program["response"]
            assert record["id"] == ident and record["status"] == "completed"
            outputs = [item for item in record["output"] if item["type"] == "program_output"]
            assert len(outputs) == 1 and outputs[0]["call_id"] == program["parent"]
            assert outputs[0]["status"] == "completed" and outputs[0]["result"] == expected
            assert not any(item["type"] == "shell_call" for item in record["output"])
            assert json.loads(raw.parse().output_text) == {"result": expected}
            raw = api.input_items.with_raw_response.list(ident, order="asc", limit=100)
            page = capture(label + "_inputs", raw)
            results = [item for item in page["data"] if item["type"] == reply["type"]]
            assert len(results) == 1 and all(results[0].get(k) == v for k, v in reply.items())
            private(lambda: other.responses.retrieve(ident))
            return dict(id=ident, response_sha256=hashes[label + "_response"],
                        inputs_sha256=hashes[label + "_inputs"])

        restored = None
        if prior := params.get("previous"):
            restored = verify(prior["id"], "restored")
            assert restored == prior
        created = api.with_raw_response.create(model="qwen3.8-flash-next", store=True,
            previous_response_id=program["response"], input=[reply],
            tools=[{"type": "programmatic_tool_calling"},
                   {"type": "shell", "environment": {"type": "local"}, "allowed_callers": ["programmatic"]}],
            tool_choice="none", instructions="Copy the complete program output into result.",
            temperature=0, max_output_tokens=384,
            text={"format": {"type": "json_schema", "name": "shell_status", "strict": True,
                             "schema": {"type": "object", "properties": {"result": {"type": "string"}},
                                        "required": ["result"], "additionalProperties": False}}})
        result = capture("create", created)
        saved = verify(result["id"], "current")
        independent = owner.chat.completions.with_raw_response.create(model="qwen3.8-flash-next",
            temperature=0, max_completion_tokens=64,
            messages=[{"role": "user", "content": "Reply only with 720041."}])
        capture("independent", independent)
        answer = independent.parse().choices[0].message.content.strip()
        assert answer == "720041"
    return dict(sdk=openai.__version__, original=direct["response"], follow=direct["follow"],
                program=program["response"], program_follow=program["follow"], peer=404,
                saved=saved, restored=restored, result=expected, independent_chat=answer,
                wire_sha256=hashes)


if __name__ == "__main__":
    print(json.dumps(exercise(json.load(sys.stdin))))
