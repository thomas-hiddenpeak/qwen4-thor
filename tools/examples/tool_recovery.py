"""Read-only recovery planning for the versioned tool demo journal.

Never sends requests, cancels, dispatches tools, or repairs the input journal.
Use only on a stopped run. An unknown outcome always needs reconciliation.
"""
import argparse
from contextlib import contextmanager
from dataclasses import asdict
import fcntl
import io
from itertools import islice
import json
import os
from pathlib import Path
import tempfile

from chat_client import (CustomToolInvocation, _invocation_calls, ToolInvocation, _completion, append_final,
                         append_tool_results, load_json, read_json, read_sse,
                         require, validate_capabilities)


ROOT = Path(__file__).resolve().parents[2]
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_JOURNAL_BYTES = 128 * 1024 * 1024


def save_receipt(path, value):
    """Publish complete JSON without replacement, durable before side effects."""
    path = Path(path)
    payload = (json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2)
               + "\n").encode("utf-8")
    require(len(payload) <= MAX_FILE_BYTES, "receipt exceeds byte budget")
    fd, name = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)  # Atomic no-replace publication, same directory.
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
            temporary.unlink()
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def journal_lock(directory, *, create=False):
    flags = os.O_NOFOLLOW | (os.O_RDWR | os.O_CREAT | os.O_EXCL
                            if create else os.O_RDONLY)
    fd = os.open(Path(directory) / ".journal.lock", flags, 0o600)
    try:
        fcntl.flock(fd, (fcntl.LOCK_EX if create else fcntl.LOCK_SH) |
                    fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def _inspect(directory):
    paths = list(islice(directory.iterdir(), 257))
    require(len(paths) <= 256, "too many journal files")
    files, total = {}, 0
    for path in paths:
        require(not path.is_symlink() and path.is_file(),
                "journal entries must be regular local files")
        size = path.stat().st_size
        total += size
        require(size <= MAX_FILE_BYTES and total <= MAX_JOURNAL_BYTES,
                "journal byte budget exceeded")
        files[path.name] = path
    seen = {".journal.lock"}

    def read(name, required=False, raw=False):
        if name not in files:
            require(not required, "missing receipt: " + name)
            return None
        seen.add(name)
        data = files[name].read_bytes()
        if raw:
            return data
        value = load_json(data)
        # None means absent, never a present but invalid JSON receipt.
        require(value is not None, "null receipt: " + name)
        return value

    start = read("run-started.json", True)
    require(isinstance(start, dict) and
            type(start.get("journal_version")) is int and
            start["journal_version"] == 1,
            "unversioned journals cannot prove recovery boundaries")
    history = start.get("initial_messages")
    require(isinstance(history, list) and history, "missing initial history")
    limit, tool_limit = start.get("max_requests"), start.get("max_tool_calls")
    require(type(limit) is int and 1 <= limit <= 8 and
            type(tool_limit) is int and 1 <= tool_limit <= 32,
            "invalid journal budgets")
    caps = read("capabilities.json")
    if caps is not None:
        validate_capabilities(caps, start.get("model"))
    failure = read("failed.json")
    require(failure is None or (isinstance(failure, dict) and
            failure.get("automatic_retry") is False), "invalid failure receipt")
    completed_tools = attempted_tools = requests = 0
    request_ids, cancel_tokens = set(), set()

    def finish(state, action, *, pending=None, results=None):
        require(set(files) <= seen, "unexpected or out-of-order journal files: " +
                ", ".join(sorted(set(files) - seen)))
        return dict(state=state, next_action=action, automatic_retry=False,
            requests=requests, completed_tool_calls=completed_tools,
            attempted_tool_calls=attempted_tools,
            remaining_requests=limit - requests,
            remaining_tool_calls=tool_limit - attempted_tools,
            messages=history, pending_tool_calls=pending or [],
            known_results=results or {},
            cancellation_note="202 accepts cancellation only; 404 does not "
                              "prove no execution. Never reuse request IDs.")

    for turn in range(limit):
        label = f"round-{turn:02d}"
        request = read(label + "-request.json")
        if request is None:
            return finish("READY_TO_REQUEST", "Recheck capabilities, then use "
                "a fresh request ID/cancel token with the saved full history.")
        require(caps is not None and isinstance(request, dict) and
                request.get("messages") == history and
                request.get("model") == start.get("model") and
                request.get("parse_response") is True and
                type(request.get("stream")) is bool,
                "request does not match committed history/capabilities")
        identifier, token = request.get("request_id"), request.get("cancel_token")
        require(isinstance(identifier, str) and identifier and
                identifier not in request_ids and isinstance(token, str) and
                len(token) == 64 and all(c in "0123456789abcdef" for c in token)
                and token not in cancel_tokens, "invalid/reused request credentials")
        request_ids.add(identifier)
        cancel_tokens.add(token)
        submitted = read(label + "-submitted.json")
        wire = read(label + "-wire.bin", raw=True)
        response = read(label + "-response.json")
        if submitted is None:
            require(wire is None and response is None,
                    "response exists without submission receipt")
            return finish("REQUEST_NOT_SUBMITTED", "Recheck capabilities and "
                "create a new request with fresh credentials; do not reuse it.")
        require(submitted == {"request_id": request.get("request_id")},
                "submission identity mismatch")
        requests += 1
        if response is None:
            return finish("REQUEST_OUTCOME_UNKNOWN", "Keep the wire and request "
                "receipt; reconcile or explicitly cancel using original "
                "credentials. Do not commit partial text or execute tools.")
        require(isinstance(response, dict) and wire is not None,
                "response receipt requires raw wire")
        completion = _completion(response.get("id"), response.get("model"),
            response.get("message"), response.get("finish_reason"),
            response.get("usage"))
        require(completion.id == request.get("request_id") and
                completion.model == request.get("model"),
                "saved response identity mismatch")
        decoded = (read_sse if request["stream"] else read_json)(io.BytesIO(wire))
        require(asdict(decoded) == asdict(completion), "wire/receipt mismatch")
        saved_history = read(label + "-history.json")
        if completion.finish_reason == "length":
            require(saved_history is None, "truncated turn was committed")
            return finish("LENGTH_LIMIT", "Keep the last committed history; "
                "explicitly choose a new bounded request. Never execute "
                "tool calls from a length-limited turn.")
        if completion.finish_reason == "stop":
            history = append_final(history, completion)
            require(saved_history is None or saved_history == history,
                    "final history mismatch")
            completed = read("completed.json")
            if completed is not None:
                require(saved_history is not None and completed == dict(
                    requests=requests, tool_calls=completed_tools,
                    messages=history), "completion receipt mismatch")
            return finish("COMPLETED" if completed else "FINAL_RESPONSE_SAVED",
                          "No replay needed. Task success requires its own "
                          "business check, separate from protocol completion.")
        invocations = [asdict(CustomToolInvocation(call["id"], call["custom"]["name"],
            call["custom"]["input"]) if call["type"] == "custom" else
            ToolInvocation(call["id"], call["function"]["name"],
                load_json(call["function"]["arguments"])))
            for call in _invocation_calls(completion.message)]
        plan = read(label + "-plan.json")
        require(plan is None or plan == invocations, "tool plan mismatch")
        results, pending, unknown = {}, [], []
        for index, invocation in enumerate(invocations):
            tool_label = f"{label}-tool-{index:03d}"
            started = read(tool_label + "-started.json")
            result = read(tool_label + "-result.json")
            require(started is None or (plan is not None and
                    started == invocation), "tool start/plan mismatch")
            if started is not None:
                attempted_tools += 1
            if result is not None:
                require(started is not None and isinstance(result, dict) and
                        set(result) == {"tool_call_id", "content"} and
                        result["tool_call_id"] == invocation["call_id"] and
                        isinstance(result["content"], str),
                        "tool result/start mismatch")
                results[invocation["call_id"]] = result["content"]
                completed_tools += 1
            else:
                pending.append(invocation)
                if started is not None:
                    unknown.append(invocation["call_id"])
        require(completed_tools + len(pending) <= tool_limit,
                "recorded tool batch exceeds execution budget")
        if pending:
            require(saved_history is None, "incomplete tool batch was committed")
            if not unknown and requests >= limit:
                return finish("REQUEST_BUDGET_EXHAUSTED", "No request budget "
                    "remains to submit tool results; do not start more tools.",
                    pending=pending, results=results)
            return finish("TOOL_OUTCOME_UNKNOWN" if unknown else "TOOLS_PENDING",
                "Reconcile started calls without results; never automatically "
                "replay them." if unknown else "Revalidate the complete batch "
                "against the current allowlist and remaining budgets before "
                "explicitly executing only calls that never started.",
                pending=pending, results=results)
        history = append_tool_results(history, completion, results)
        require(saved_history is None or saved_history == history,
                "tool history mismatch")
        # A missing history receipt is recoverable from complete durable results.
    return finish("REQUEST_BUDGET_EXHAUSTED", "Do not automatically extend the "
                  "request budget; all known tool results are retained.")


def inspect_recovery(directory):
    directory = Path(directory).resolve(strict=True)
    try:
        with journal_lock(directory):
            return _inspect(directory)
    except BlockingIOError:
        return dict(state="ACTIVE_RUN", automatic_retry=False,
                    next_action="Wait for the writer to stop; do not replay.")
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        return dict(state="RECORDS_INVALID", automatic_retry=False,
                    next_action="Preserve evidence and reconcile manually.",
                    error=str(error))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = inspect_recovery(args.directory)
    if args.output:
        output = args.output.resolve()
        require(output.is_relative_to(ROOT / ".q4t-work") and
                not output.is_relative_to(args.directory.resolve()),
                "write recovery plans outside the source journal in .q4t-work")
        save_receipt(output, result)
    # Keep messages, tool results and cancellation credentials off the terminal.
    print(json.dumps({k: v for k, v in result.items() if k not in
                      ("messages", "known_results", "pending_tool_calls")},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
