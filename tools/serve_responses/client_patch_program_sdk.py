"""Verify an actual failed patch result is delivered to its issuing program."""
import errno
import hashlib
import json
from pathlib import Path
import sys

import openai


def exercise(params):
    wire = Path(params["capture"])
    wire.mkdir(mode=0o700)
    workspace = Path(params["workspace"])
    workspace.mkdir(mode=0o700)
    name = "absent-record.txt"
    selector = {"type": "programmatic_tool_calling"}
    model = "qwen3.8-flash-next"
    code = ('const r = await tools.apply_patch({type:"delete_file",'
            'path:"absent-record.txt"}); text(r.status + " " + r.output);')
    options = dict(model=model, temperature=0, max_output_tokens=384, store=True,
                   tools=[selector, {"type": "apply_patch", "allowed_callers": ["programmatic"]}])
    hashes = {}

    def save(name, data):
        (wire / name).write_bytes(data)
        hashes[name] = hashlib.sha256(data).hexdigest()

    with openai.OpenAI(base_url=params["base"], api_key=params["key"], max_retries=0, timeout=115,
                       http_client=openai.DefaultHttpxClient(verify=params["certificate"])) as sdk:
        response = sdk.responses.with_raw_response.create(**options, tool_choice=selector,
            parallel_tool_calls=False, max_tool_calls=1,
            input="Run this JavaScript program exactly and wait for the real client result: " + code)
        save("pending.json", response.http_response.content)
        original = response.http_response.json()
        assert original["status"] == "completed"
        parents = [i for i in original["output"] if i["type"] == "program"]
        children = [i for i in original["output"] if i["type"] == "apply_patch_call"]
        assert len(parents) == len(children) == 1 and parents[0]["fingerprint"]
        parent, child = parents[0], children[0]
        assert child["status"] == "completed" and child["caller"] == {
            "type": "program", "caller_id": parent["call_id"]}
        assert child["operation"] == {"type": "delete_file", "path": name}
        assert original["output"].index(parent) < original["output"].index(child)
        assert not any(i["type"] == "program_output" for i in original["output"])
        assert list(workspace.iterdir()) == []
        try:
            (workspace / name).unlink()
        except FileNotFoundError as error:
            assert error.errno == errno.ENOENT
            detail = errno.errorcode[error.errno]
        else:
            raise AssertionError("The missing-file patch unexpectedly succeeded")
        outcome = dict(type="apply_patch_call_output", status="failed", output=detail,
                       call_id=child["call_id"], caller=child["caller"])
        follow = dict(options, previous_response_id=original["id"], tool_choice="none",
                      instructions="Copy the complete program output string into the result field.",
                      text={"format": {"type": "json_schema", "name": "patch_status",
                            "strict": True, "schema": {"type": "object", "properties": {
                                "result": {"type": "string"}}, "required": ["result"],
                                "additionalProperties": False}}})
        try:
            sdk.responses.create(**follow, input=[dict(outcome, caller={"type": "program",
                                                                        "caller_id": "other"})])
        except openai.BadRequestError as error:
            assert error.status_code == 400
        else:
            raise AssertionError("An unrelated program claimed the patch result")
        answer = sdk.responses.with_raw_response.create(**follow, input=[outcome])
        save("completed.json", answer.http_response.content)
        final = answer.http_response.json()
        expected = "failed " + detail
        assert final["status"] == "completed" and final["previous_response_id"] == original["id"]
        replies = [i for i in final["output"] if i["type"] == "program_output"]
        assert len(replies) == 1 and replies[0]["call_id"] == parent["call_id"]
        assert replies[0]["status"] == "completed" and replies[0]["result"] == expected
        assert json.loads(answer.parse().output_text) == {"result": expected}
        assert not any(i["type"] in ("program", "apply_patch_call") for i in final["output"])
        stored = sdk.responses.with_raw_response.retrieve(final["id"])
        save("stored.json", stored.http_response.content)
        assert stored.http_response.json()["output"] == final["output"]
        page = sdk.responses.input_items.with_raw_response.list(final["id"], order="asc", limit=100)
        save("inputs.json", page.http_response.content)
        saved = [i for i in page.http_response.json()["data"] if i["type"] == outcome["type"]]
        assert len(saved) == 1 and all(saved[0].get(k) == v for k, v in outcome.items())
        with openai.OpenAI(base_url=params["base"], api_key=params["other"], max_retries=0, timeout=15,
                           http_client=openai.DefaultHttpxClient(verify=params["certificate"])) as peer:
            try:
                peer.responses.retrieve(original["id"])
            except openai.NotFoundError as error:
                assert error.status_code == 404
            else:
                raise AssertionError("Another client could read the issuing program")
        chat = sdk.chat.completions.with_raw_response.create(model=model, temperature=0,
            max_completion_tokens=64, messages=[{"role": "user", "content": "Reply only with 720041."}])
        save("chat.json", chat.http_response.content)
        assert chat.parse().choices[0].message.content.strip() == "720041"
    return dict(sdk=openai.__version__, response=original["id"], follow=final["id"],
                program=parent["call_id"], child=child["call_id"], failed_operations=1,
                answer=expected, peer=404, wrong_target=400, independent_chat="720041", captures=hashes)


if __name__ == "__main__":
    print(json.dumps(exercise(json.load(sys.stdin))))
