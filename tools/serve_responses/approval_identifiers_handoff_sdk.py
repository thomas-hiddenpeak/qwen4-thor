"""Read original MCP decisions through the Python SDK after a service handoff."""
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import sys

import openai


def semantic_digest(value):
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def exercise(params):
    directory = Path(params["capture"])
    directory.mkdir(mode=0o700)
    hashes = {}

    def capture(label, reply):
        response = reply.http_response
        response.raise_for_status()
        (directory / (label + ".json")).write_bytes(response.content)
        hashes[label] = hashlib.sha256(response.content).hexdigest()
        return response.json()

    with ExitStack() as stack:
        def client(key):
            transport = openai.DefaultHttpxClient(verify=params.get("certificate") or True)
            return stack.enter_context(openai.OpenAI(
                base_url=params["base"], api_key=key, max_retries=0,
                timeout=90, http_client=transport))

        owner, peer = client(params["key"]), client(params["other"])
        service = owner.conversations
        decisions = []
        for name in ("approved", "denied"):
            source = params["original"][name]
            response_id = source["response"]
            response = capture(name + "_response", owner.responses.with_raw_response.retrieve(
                response_id))
            assert response["id"] == response_id and response["status"] == "completed"
            calls = [i for i in response["output"] if i["type"] == "mcp_call"
                     and i.get("status") == "completed"]
            if name == "approved":
                assert len(calls) == 1 and calls[0]["name"] == "read_project_record"
                assert params["citation"] in calls[0]["output"]
            else:
                assert not calls

            page = capture(name + "_inputs", owner.responses.input_items.with_raw_response.list(
                response_id, order="asc", limit=100))
            assert semantic_digest(page) == source["projected_sha256"]
            selected = [i for i in page["data"] if i["id"] == source["decision"]]
            assert len(selected) == 1
            decision = selected[0]
            assert decision["type"] == "mcp_approval_response"
            assert decision["approval_request_id"] == decision["request_id"] == source["target"]
            assert decision["approve"] is (name == "approved")
            decisions.append(decision)
            try:
                peer.responses.input_items.list(response_id)
            except openai.NotFoundError as error:
                assert error.status_code == 404
            else:
                raise AssertionError("Another owner read an original approval")

        def observe(ident, label):
            page = capture(label + "_page", service.items.with_raw_response.list(
                ident, order="asc"))
            assert page["data"] == decisions
            for decision in decisions:
                key = "approved" if decision["approve"] else "denied"
                item = capture(label + "_" + key, service.items.with_raw_response.retrieve(
                    decision["id"], conversation_id=ident))
                assert item == decision
            try:
                peer.conversations.items.list(ident)
            except openai.NotFoundError as error:
                assert error.status_code == 404
            else:
                raise AssertionError("Another owner read the approval conversation")
            return dict(conversation=ident, page_sha256=hashes[label + "_page"],
                        approved_sha256=hashes[label + "_approved"],
                        denied_sha256=hashes[label + "_denied"], peer=404)

        restored = None
        if previous := params.get("previous"):
            restored = observe(previous["conversation"], "restored")
            assert restored == previous
        # The original IDs are reused. Existing SDKs can omit the wire alias.
        legacy = dict(decisions[1])
        del legacy["request_id"]
        created = capture("create", service.with_raw_response.create(items=[decisions[0], legacy]))
        saved = observe(created["id"], "current")
        try:
            service.create(items=[dict(decisions[1], approve=True)])
        except openai.ConflictError as error:
            assert error.status_code == 409
        else:
            raise AssertionError("The original denial was rewritten")
        try:
            service.create(items=[dict(decisions[1], request_id=decisions[0]["request_id"])])
        except openai.BadRequestError as error:
            assert error.status_code == 400 and error.param == "items[0].request_id"
        else:
            raise AssertionError("A decision targeted two different approvals")
        simple = owner.chat.completions.with_raw_response.create(
            model="qwen3.8-flash-next", temperature=0, max_completion_tokens=64,
            messages=[{"role": "user", "content": "Reply with exactly 720041 and nothing else."}])
        capture("independent", simple)
        answer = simple.parse().choices[0].message.content.strip()
    return dict(sdk=openai.__version__, original=params["original"], saved=saved,
                restored=restored, decision_conflict=409, mismatch=400,
                independent_chat=answer, wire_sha256=hashes)


if __name__ == "__main__":
    print(json.dumps(exercise(json.load(sys.stdin))))
