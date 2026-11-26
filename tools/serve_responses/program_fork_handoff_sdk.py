"""Reopen both program branches and ask the original program for a third release."""
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import sys

import openai


def exercise(params):
    capture = Path(params["capture"])
    capture.mkdir(mode=0o700)
    old = params["original"]
    revision, methods = params["revision"], params["methods"]
    hashes = {}

    def keep(name, raw):
        data = raw.http_response.content
        (capture / (name + ".json")).write_bytes(data)
        hashes[name] = hashlib.sha256(data).hexdigest()
        return raw.http_response.json()

    def one(row, kind):
        item, = (entry for entry in row["output"] if entry["type"] == kind)
        return item

    with ExitStack() as stack:
        def connect(key):
            return stack.enter_context(openai.OpenAI(base_url=params["base"], api_key=key,
                max_retries=0, timeout=80,
                http_client=openai.DefaultHttpxClient(verify=params.get("certificate") or True)))
        owner, peer = connect(params["key"]), connect(params["other"])
        api = owner.responses.with_raw_response
        caller = {"type": "program", "caller_id": old["parent"]}

        def deny(ident):
            try:
                peer.responses.retrieve(ident)
            except openai.NotFoundError as error:
                assert error.status_code == 404
            else:
                raise AssertionError("another client retrieved the program")
        original = keep("original", api.retrieve(old["original"]))
        seed = one(original, "function_call")
        assert seed["caller"] == caller and seed["call_id"] == old["first"]
        assert one(original, "program")["call_id"] == old["parent"]
        deny(old["original"])
        for branch in old["branches"]:
            n = branch["revision"]
            pending = keep(f"pending_{n}", api.retrieve(branch["pending"]))
            child = one(pending, "function_call")
            assert hashes[f"pending_{n}"] == old["captures"][f"stored-{n}"]
            assert child["caller"] == caller and child["call_id"] == branch["call_id"]
            answer = keep(f"answer_{n}", api.retrieve(branch["continuation"]))
            terminal = one(answer, "program_output")
            assert answer["previous_response_id"] == branch["pending"]
            assert terminal["call_id"] == old["parent"] and terminal["result"] == branch["result"]
            deny(branch["pending"])
        options = dict(model="qwen3.8-flash-next", store=True, temperature=0, max_output_tokens=384,
            tool_choice="none", parallel_tool_calls=False, tools=[{"type": "programmatic_tool_calling"},
                {"type": "function", "name": "record", "allowed_callers": ["programmatic"],
                 "parameters": {"type": "object", "properties": {"revision": {"type": "integer"}},
                                "required": ["revision"], "additionalProperties": False}}])

        def result(call, value):
            return dict(type="function_call_output", caller=caller, call_id=call,
                        output=json.dumps(value, separators=(",", ":")))
        pending = keep("branch", api.create(**options, previous_response_id=original["id"],
                       input=[result(seed["call_id"], {"revision": revision})]))
        child = one(pending, "function_call")
        assert child["caller"] == caller and json.loads(child["arguments"]) == {"revision": revision}
        assert all(child[key] != b[ref] for b in old["branches"]
                   for key, ref in (("call_id", "call_id"), ("id", "child_id")))
        reply = result(child["call_id"], {"methods": methods})
        expected = f"{revision}:{methods}"

        def verify(ident, label, previous):
            raw = api.retrieve(ident)
            response = keep(label + "_response", raw)
            assert response["status"] == "completed" and response["previous_response_id"] == previous
            terminal = one(response, "program_output")
            assert terminal["call_id"] == old["parent"] and terminal["result"] == expected
            assert json.loads(raw.parse().output_text) == {"result": expected}
            page = keep(label + "_inputs", api.input_items.list(ident, order="asc", limit=100))
            matches = [i for i in page["data"] if i["type"] == "function_call_output" and
                       i.get("call_id") == child["call_id"]]
            assert len(matches) == 1 and all(matches[0].get(k) == v for k, v in reply.items())
            deny(ident)
            return dict(id=ident, pending=previous, response_sha256=hashes[label + "_response"],
                        inputs_sha256=hashes[label + "_inputs"])
        restored = None
        if prior := params.get("previous"):
            restored = verify(prior["id"], "restored", prior["pending"])
            assert restored == prior
        completed = keep("create", api.create(**options, previous_response_id=pending["id"], input=[reply],
            instructions="Copy the program output into result verbatim.",
            text={"format": {"type": "json_schema", "name": "record_result", "strict": True,
                             "schema": {"type": "object", "properties": {"result": {"type": "string"}},
                                        "required": ["result"], "additionalProperties": False}}}))
        saved = verify(completed["id"], "current", pending["id"])
        chat = owner.chat.completions.with_raw_response.create(model=options["model"], temperature=0,
            max_completion_tokens=64, messages=[{"role": "user", "content": "Reply only with 720041."}])
        assert keep("independent", chat)["choices"][0]["message"]["content"].strip() == "720041"
    return dict(original=old["original"], follow=old["branches"][0]["continuation"],
                child=child["call_id"], child_item=child["id"], result=expected,
                saved=saved, restored=restored, peer=404, independent_chat="720041",
                wire_sha256=hashes, sdk=openai.__version__)


if __name__ == "__main__":
    print(json.dumps(exercise(json.load(sys.stdin))))
