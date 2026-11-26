"""Read a retried ChatKit thread through the official Python management client."""

from contextlib import ExitStack
import hashlib
import json
import sys

import openai


def read_history(client, thread, visible, answers, order):
    ids = visible if order == "asc" else list(reversed(visible))
    limit = max(1, len(ids) // 2)
    page = client.beta.chatkit.threads.list_items(thread, order=order, limit=limit)
    results = []
    for offset in range(0, len(ids), limit):
        if offset:
            page = page.get_next_page()
        expected = ids[offset:offset + limit]
        assert [item.id for item in page.data] == expected
        assert page.last_id == expected[-1]
        assert page.has_next_page() == (offset + limit < len(ids))
        for item in page.data:
            assert item.thread_id == thread
            if item.id in answers:
                assert item.type == "chatkit.assistant_message"
                assert "".join(part.text for part in item.content).strip() == answers[item.id]
            else:
                assert item.type == "chatkit.user_message"
        results.append([item.to_dict() for item in page.data])
    return results


def private(operation):
    try:
        operation()
    except openai.NotFoundError as error:
        assert error.status_code == 404
        return 404
    raise AssertionError("Expected a missing or inaccessible resource")


def exercise(owner, peer, plan):
    thread, ids = plan["thread"], plan["visible"]
    answers = plan["answers"]
    assert len(ids) >= 2 and len(set(ids)) == len(ids)
    assert answers.keys() <= set(ids) and plan["obsolete"] not in ids
    ascending = read_history(owner, thread, ids, answers, "asc")
    descending = read_history(owner, thread, ids, answers, "desc")
    items = [item for page in ascending for item in page]
    assert [item for page in descending for item in page] == items[::-1]
    denied = private(lambda: peer.beta.chatkit.threads.list_items(thread))
    deleted = private(lambda: owner.files.content(plan["deleted_file"]))
    return dict(sdk=openai.__version__, thread=thread, visible=ids,
                pages_per_order=len(ascending), peer=denied, deleted=deleted,
                items_sha256=hashlib.sha256(json.dumps(
                    items, sort_keys=True, ensure_ascii=False).encode()).hexdigest())


if __name__ == "__main__":
    params = json.load(sys.stdin)
    with ExitStack() as stack:
        def connect(key):
            return stack.enter_context(openai.OpenAI(
                base_url=params["base"], api_key=key, max_retries=0, timeout=8,
                _strict_response_validation=True,
                http_client=openai.DefaultHttpxClient(verify=params["certificate"])))

        result = exercise(connect(params["key"]), connect(params["other"]), params)
    print(json.dumps(result))
