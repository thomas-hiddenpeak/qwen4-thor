"""Read owned local shell history and continue it after a service handoff."""
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import sys

import openai


def exercise(params):
    directory = Path(params["capture"])
    directory.mkdir(mode=0o700)
    hashes = {}
    source = params["original"]
    expected = {"architecture": source["answer"]}

    def capture(name, reply):
        response = reply.http_response
        response.raise_for_status()
        (directory / (name + ".json")).write_bytes(response.content)
        hashes[name] = hashlib.sha256(response.content).hexdigest()
        return response.json()

    def hidden(query):
        try:
            query()
        except openai.NotFoundError as error:
            assert error.status_code == 404
        else:
            raise AssertionError("Another client could read a stored local shell response")

    with ExitStack() as stack:
        def client(key):
            transport = openai.DefaultHttpxClient(verify=params.get("certificate") or True)
            return stack.enter_context(openai.OpenAI(
                base_url=params["base"], api_key=key, max_retries=0,
                timeout=95, http_client=transport))

        owner, peer = client(params["key"]), client(params["other"])
        api = owner.responses
        for label, key in (("call", "response"), ("completed", "follow")):
            record = capture(label, api.with_raw_response.retrieve(source[key]))
            assert record["id"] == source[key] and record["status"] == "completed"
            if label == "call":
                matches = [item for item in record["output"] if item["type"] == "local_shell_call"]
                assert len(matches) == 1
                item = matches[0]
                assert item["id"] == source["item"] and item["call_id"] == source["call_id"]
                assert item["action"]["command"] == ["head", "-n", "1", "docs/MODEL.md"]
            else:
                assert record["previous_response_id"] == source["response"]
        page = capture("inputs", api.input_items.with_raw_response.list(
            source["follow"], order="asc", limit=100))
        results = [i for i in page["data"] if i["type"] == "local_shell_call_output"]
        assert len(results) == 1 and results[0]["call_id"] == results[0]["id"] == source["call_id"]
        assert hashlib.sha256((results[0]["output"] + "\n").encode()).hexdigest() == source["document_sha256"]
        assert all(hashes[k] == source["captures"][k + ".json"] for k in ("call", "completed", "inputs"))
        hidden(lambda: peer.responses.retrieve(source["response"]))
        hidden(lambda: peer.responses.input_items.list(source["follow"]))

        def observe(ident, label):
            reply = api.with_raw_response.retrieve(ident)
            record = capture(label + "_response", reply)
            assert record["id"] == ident and record["status"] == "completed"
            assert record["previous_response_id"] == source["follow"]
            assert json.loads(reply.parse().output_text) == expected
            assert not any(item["type"] == "local_shell_call" for item in record["output"])
            page = capture(label + "_inputs", api.input_items.with_raw_response.list(
                ident, order="asc", limit=100))
            assert any(i["type"] == "message" and i.get("role") == "user" for i in page["data"])
            hidden(lambda: peer.responses.retrieve(ident))
            return dict(id=ident, response_sha256=hashes[label + "_response"],
                        inputs_sha256=hashes[label + "_inputs"])

        restored = None
        if prior := params.get("previous"):
            restored = observe(prior["id"], "restored")
            assert restored == prior
        prompt = "From the earlier project document result, return the architecture identifier inside parentheses."
        shape = dict(type="object", properties={"architecture": {"type": "string"}},
                     required=["architecture"], additionalProperties=False)
        reply = api.with_raw_response.create(
            model="qwen3.8-flash-next", store=True, previous_response_id=source["follow"],
            input=prompt, temperature=0, max_output_tokens=192,
            text={"format": dict(type="json_schema", name="project_record", strict=True, schema=shape)})
        created = capture("create", reply)
        assert created["id"] not in {source["response"], source["follow"]}
        saved = observe(created["id"], "current")
        chat = owner.chat.completions.with_raw_response.create(
            model="qwen3.8-flash-next", temperature=0, max_completion_tokens=64,
            messages=[{"role": "user", "content": "Reply with exactly 720041 and nothing else."}])
        capture("independent", chat)
        independent = chat.parse().choices[0].message.content.strip()
    return dict(sdk=openai.__version__, original=source["response"], follow=source["follow"],
                saved=saved, restored=restored, independent_chat=independent,
                answer=expected["architecture"], peer=404, wire_sha256=hashes)


if __name__ == "__main__":
    print(json.dumps(exercise(json.load(sys.stdin))))
