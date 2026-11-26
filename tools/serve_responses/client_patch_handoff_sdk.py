"""Reopen both patch histories and resume the authenticated program after a handoff."""
from contextlib import ExitStack
import errno
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
            raise AssertionError("Another client could read a patch resource")

    with ExitStack() as stack:
        def connect(key):
            return stack.enter_context(openai.OpenAI(
                base_url=params["base"], api_key=key, max_retries=0, timeout=95,
                http_client=openai.DefaultHttpxClient(verify=params.get("certificate") or True)))

        owner, other = connect(params["key"]), connect(params["other"])
        api = owner.responses
        for prefix, proof, call_file, follow_file in (
                ("direct", direct, "call.json", "completed.json"),
                ("program", program, "pending.json", "stored.json")):
            for key, label, filename in (("response", "call", call_file),
                                          ("follow", "completed", follow_file)):
                name = prefix + "_" + label
                row = capture(name, api.with_raw_response.retrieve(proof[key]))
                assert row["id"] == proof[key] and hashes[name] == proof["captures"][filename]
                private(lambda: other.responses.retrieve(proof[key]))
            name = prefix + "_inputs"
            page = capture(name, api.input_items.with_raw_response.list(
                proof["follow"], order="asc", limit=100))
            assert hashes[name] == proof["captures"]["inputs.json"]
            results = [item for item in page["data"] if item["type"] == "apply_patch_call_output"]
            assert len(results) == 1
            if prefix == "program":
                caller = {"type": "program", "caller_id": program["program"]}
                assert results[0]["call_id"] == program["child"] and results[0]["caller"] == caller
            else:
                assert results[0]["call_id"] == direct["call_id"]
                assert results[0]["output"] == "Receipt: " + direct["answer"]

        work = directory.parent / (directory.name + "-workspace")
        work.mkdir(mode=0o700)
        try:
            (work / "absent-record.txt").unlink()
        except FileNotFoundError as error:
            assert error.errno == errno.ENOENT
            detail = errno.errorcode[error.errno]
        else:
            raise AssertionError("Missing-file delete succeeded")
        reply = dict(type="apply_patch_call_output", status="failed", output=detail,
                     call_id=program["child"], caller=caller)
        expected = "failed " + detail

        def verify(ident, label):
            raw = api.with_raw_response.retrieve(ident)
            record = capture(label + "_response", raw)
            assert record["previous_response_id"] == program["response"]
            assert record["id"] == ident and record["status"] == "completed"
            outputs = [item for item in record["output"] if item["type"] == "program_output"]
            assert len(outputs) == 1 and outputs[0]["call_id"] == program["program"]
            assert outputs[0]["status"] == "completed" and outputs[0]["result"] == expected
            assert not any(item["type"] == "apply_patch_call" for item in record["output"])
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
                   {"type": "apply_patch", "allowed_callers": ["programmatic"]}],
            tool_choice="none", instructions="Copy the complete program output into result.",
            temperature=0, max_output_tokens=384,
            text={"format": {"type": "json_schema", "name": "patch_status", "strict": True,
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
