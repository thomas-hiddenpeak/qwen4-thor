"""Exercise two client-result branches of one stored program on native TLS."""
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import sys

import openai


def exercise(params):
    capture = Path(params["capture"])
    capture.mkdir(mode=0o700)
    project = Path(params["project"])
    delivery = json.loads((project / "docs/evidence/serve-client-patch-20261126/delivery.json").read_bytes())
    revisions = (delivery["previous_revision"], delivery["publication"]["publication_revision"])
    receipts = {}
    for scope in ("serve-local-shell-20261126", "serve-client-patch-20261126"):
        path = project / "docs/evidence" / scope / "delivery.json"
        record = json.loads(path.read_bytes())
        receipts[record["publication"]["publication_revision"]] = record
    assert set(receipts) == set(revisions) and len(receipts) == 2
    captures = {}

    def keep(name, raw):
        data = raw.http_response.content
        (capture / (name + ".json")).write_bytes(data)
        captures[name] = hashlib.sha256(data).hexdigest()
        return raw.http_response.json()

    def only(response, kind):
        item, = (i for i in response["output"] if i["type"] == kind)
        return item

    def output(call, value):
        return {"type": "function_call_output", "caller": call["caller"], "call_id": call["call_id"],
                "output": json.dumps(value, separators=(",", ":"))}

    selector = {"type": "programmatic_tool_calling"}
    lookup = {"type": "function", "name": "record", "allowed_callers": ["programmatic"],
              "parameters": {"type": "object", "properties": {"revision": {"type": "integer"}},
                             "required": ["revision"], "additionalProperties": False}}
    options = dict(model="qwen3.8-flash-next", temperature=0, max_output_tokens=384,
                   store=True, parallel_tool_calls=False, tools=[selector, lookup])
    code = ("const a=await tools.record({revision:0});"
            "const b=await tools.record({revision:a.revision});text(a.revision+':'+b.methods);")
    with ExitStack() as resources:
        def connect(key):
            http = openai.DefaultHttpxClient(verify=params["certificate"])
            return resources.enter_context(openai.OpenAI(base_url=params["base"], api_key=key,
                                                          max_retries=0, timeout=90, http_client=http))
        owner, peer = connect(params["key"]), connect(params["other"])
        raw = owner.responses.with_raw_response
        initial = keep("initial", raw.create(**options, tool_choice=selector,
                        input="Run this exact JavaScript using the programmatic tool. Await both results. " + code))
        parent = only(initial, "program")
        first = only(initial, "function_call")
        assert first["name"] == "record" and json.loads(first["arguments"]) == {"revision": 0}
        caller = {"type": "program", "caller_id": parent["call_id"]}
        assert first["caller"] == caller and parent["fingerprint"]
        branches = []
        for revision in revisions:
            branch = keep(f"branch-{revision}", raw.create(
                **options, tool_choice="none", previous_response_id=initial["id"],
                input=[output(first, {"revision": revision})]))
            child = only(branch, "function_call")
            assert branch["status"] == "completed" and child["caller"] == caller
            assert child["name"] == "record" and json.loads(child["arguments"]) == {"revision": revision}
            branches.append((revision, branch, child))
        assert all(branches[0][2][k] != branches[1][2][k] for k in ("id", "call_id"))
        retry = keep("repeat", raw.create(**options, tool_choice="none", previous_response_id=initial["id"],
                                         input=[output(first, {"revision": revisions[0]})]))
        assert only(retry, "function_call") == branches[0][2]
        try:
            owner.responses.create(**options, previous_response_id=branches[1][1]["id"], tool_choice="none",
                                   input=[output(branches[0][2], {"methods": 0})])
        except openai.BadRequestError as error:
            assert error.status_code == 400
        else:
            raise AssertionError("sibling child result accepted")
        summaries = []
        for revision, branch, child in branches:
            methods = receipts[revision]["declared_methods"]
            value = output(child, {"methods": methods})
            result = keep(f"answer-{revision}", raw.create(
                **options, tool_choice="none", previous_response_id=branch["id"], input=[value],
                instructions="Copy the program result into the result field verbatim.",
                text={"format": {"type": "json_schema", "name": "record_result", "strict": True,
                                 "schema": {"type": "object", "properties": {"result": {"type": "string"}},
                                            "required": ["result"], "additionalProperties": False}}}))
            terminal = only(result, "program_output")
            expected = f"{revision}:{methods}"
            assert terminal["status"] == "completed" and terminal["result"] == expected
            assert terminal["call_id"] == parent["call_id"]
            stored = keep(f"stored-{revision}", raw.retrieve(branch["id"]))
            assert only(stored, "function_call") == child
            messages = [part["text"] for item in result["output"] if item["type"] == "message"
                        for part in item["content"] if part["type"] == "output_text"]
            assert json.loads("".join(messages)) == {"result": expected}
            summaries.append(dict(revision=revision, pending=branch["id"], child_id=child["id"],
                                  call_id=child["call_id"], continuation=result["id"], result=expected))
        try:
            peer.responses.retrieve(initial["id"])
        except openai.NotFoundError as error:
            assert error.status_code == 404
        else:
            raise AssertionError("peer read another client's program")
        chat = owner.chat.completions.with_raw_response.create(
            model=options["model"], temperature=0, max_completion_tokens=64,
            messages=[{"role": "user", "content": "Reply only with 720041."}])
        assert keep("chat", chat)["choices"][0]["message"]["content"].strip() == "720041"
    return dict(sdk=openai.__version__, original=initial["id"], parent=parent["call_id"], first=first["call_id"],
                branches=summaries, retry=retry["id"], sibling_status=400, peer=404,
                independent_chat="720041", captures=captures)


if __name__ == "__main__":
    print(json.dumps(exercise(json.load(sys.stdin))))
