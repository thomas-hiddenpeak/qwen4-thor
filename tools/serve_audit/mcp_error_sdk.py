"""Check a real project-document MCP decision across strict Responses clients."""
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import sys

import openai


def value(item):
    return item.model_dump(mode="json", by_alias=True, exclude_unset=True)


def run(plan):
    folder = Path(plan["capture"])
    folder.mkdir(mode=0o700)
    legacy = plan["string_error"]
    expected = {
        "type": "mcp_tool_execution_error",
        "content": {"code": "mcp_approval_denied", "message": "Tool approval denied"},
    }

    def check_error(item):
        assert item["type"] == "mcp_call" and item["status"] == "failed"
        assert item["name"] == "read_project_record" and item["output"] is None
        error = item["error"]
        assert isinstance(error, str) == legacy
        assert (json.loads(error) if legacy else error) == expected

    def stream_events(stream, start=0):
        with stream:
            events = [value(event) for event in stream]
        assert [e["sequence_number"] for e in events] == list(range(start, start + len(events)))
        assert events[-1]["type"] == "response.completed"
        return events

    def capture(label, payload):
        (folder / (label + ".json")).write_text(json.dumps(payload, ensure_ascii=False))

    with ExitStack() as stack:
        def connect(key):
            http = openai.DefaultHttpxClient(verify=plan["certificate"], trust_env=False)
            return stack.enter_context(openai.OpenAI(
                base_url=plan["base"], api_key=key, max_retries=0, timeout=90,
                http_client=http, _strict_response_validation=True))

        owner, peer = connect(plan["key"]), connect(plan["other"])
        if plan["operation"] == "create":
            def approval():
                result = owner.responses.create(**plan["request"])
                items = [it for it in result.output if it.type == "mcp_approval_request"]
                assert result.status == "completed" and len(items) == 1
                item = items[0]
                assert item.name == "read_project_record" and item.server_label == "project"
                assert json.loads(item.arguments) == {
                    "path": "docs/MODEL.md", "start_line": 1, "line_count": 3}
                return result.id, item.id

            def follow(allow):
                rid, target = approval()
                request = dict(plan["follow"], previous_response_id=rid, input=[{
                    "type": "mcp_approval_response", "request_id": target,
                    "approval_request_id": target, "approve": allow}])
                if not allow:
                    request.pop("text", None)
                return request

            accepted = owner.responses.with_raw_response.create(**follow(True))
            assert accepted.parse().status == "completed"
            capture("approved", json.loads(accepted.http_response.content))
            approved_id = accepted.parse().id
            live = stream_events(owner.responses.create(
                **follow(False), stream=True, stream_options={"include_obfuscation": False}))
            capture("live", live)
            ident = live[-1]["response"]["id"]
        else:
            ident = plan["id"]
            approved_id = plan["approved_id"]
            live = None

        approved = owner.responses.retrieve(approved_id)
        successful, = [item for item in approved.output if item.type == "mcp_call"]
        assert successful.status == "completed" and successful.error is None
        raw = owner.responses.with_raw_response.retrieve(ident)
        resource = json.loads(raw.http_response.content)
        assert raw.parse().id == ident
        assert {v.strip().lower() for v in raw.headers["vary"].split(",")} == {
            "x-stainless-lang", "x-stainless-package-version"}
        failed, = [item for item in resource["output"] if item["type"] == "mcp_call"]
        check_error(failed)
        replay = stream_events(owner.responses.retrieve(
            ident, stream=True, include_obfuscation=False))
        item_event, = [e for e in replay if e["type"] == "response.output_item.done"
                       and e["item"]["type"] == "mcp_call"]
        check_error(item_event["item"])
        assert replay[-1]["response"]["output"] == resource["output"]
        if live is not None:
            assert live == replay
        cursor = item_event["sequence_number"]
        assert cursor + 1 < len(replay)
        suffix = stream_events(owner.responses.retrieve(
            ident, stream=True, starting_after=cursor, include_obfuscation=False), cursor + 1)
        assert suffix == replay[cursor + 1:]
        for rid in (ident, approved_id):
            try:
                peer.responses.retrieve(rid)
            except openai.NotFoundError as error:
                assert error.status_code == 404
            else:
                raise AssertionError("MCP resource visible to another customer")
        capture("resource", resource)
        capture("replay", replay)
        return dict(sdk=openai.__version__, id=ident, approved_id=approved_id,
                    string_error=legacy, get_sha256=hashlib.sha256(raw.http_response.content).hexdigest(),
                    events=len(replay), cursor=cursor, resumed=len(suffix), foreign_404=2)


if __name__ == "__main__":
    print(json.dumps(run(json.load(sys.stdin))))
