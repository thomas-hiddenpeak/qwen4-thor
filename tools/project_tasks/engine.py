"""Durable project queries: explicit resume, known-result reuse, no retries."""
from copy import deepcopy
from dataclasses import asdict
import hashlib
import io
import json
import os
from pathlib import Path
import sys

from tools.project_tasks import budget, migration, store

sys.path.insert(0, str(store.ROOT / "tools/examples"))
import chat_client as chat
from project_query import SYSTEM, validate_answer
from project_records import ProjectRecords, TOOLS, VALIDATORS

RUNTIME_FILES = (
    "tools/project_tasks/store.py", "tools/project_tasks/engine.py", "tools/project_tasks/budget.py",
    "tools/project_tasks/transport.py", "tools/project_tasks/runner.py",
    "tools/project_tasks/migration.py", "tools/project_tasks/compatible_runtimes.json",
    "tools/project_tasks/protocol.py", "tools/project_tasks/compatible_protocols.json",
    "tools/project_tasks/retention.py",
    "tools/project_tasks/backup.py", "tools/project_tasks/errors.py",
    "tools/examples/chat_client.py", "tools/examples/project_records.py",
    "tools/examples/project_query.py", "tools/examples/agent_tools_demo.py",
    "tools/examples/tool_recovery.py")
TOOLS = deepcopy(TOOLS)
for tool in TOOLS:
    tool["function"]["strict"] = True


def sources():
    return {name: hashlib.sha256((store.ROOT / name).read_bytes()).hexdigest()
            for name in RUNTIME_FILES}


def specification(binding, question, *, max_requests=4, max_tools=8,
                  stream=True, max_tokens=512):
    store.require(isinstance(question, str) and 0 < len(question) <= 4096,
                  "question must be 1..4096 characters", code='invalid_arguments')
    store.require(type(max_requests) is int and 2 <= max_requests <= 8 and
                  type(max_tools) is int and 1 <= max_tools <= 32 and
                  type(stream) is bool and type(max_tokens) is int and
                  1 <= max_tokens <= 512, "task budgets/mode", code='invalid_arguments')
    return dict(binding=deepcopy(binding), question=question,
                max_requests=max_requests, max_tools=max_tools, stream=stream,
                max_tokens=max_tokens, budget_profile=budget.PROFILE, runtime_sources=sources())


def completion(value):
    return chat._completion(value["id"], value["model"], value["message"],
                            value["finish_reason"], value["usage"])


def request_contract(value, spec, history):
    store.require(isinstance(value, dict) and value["messages"] == history and
        value["model"] == spec["binding"]["model"] and value["tools"] == TOOLS and
        value["stream"] == spec["stream"] and type(value["max_tokens"]) is int and
        value["max_tokens"] == spec["max_tokens"] and value["parse_response"] is True and
        value["parallel_tool_calls"] is False and isinstance(value["request_id"], str) and
        1 <= len(value["request_id"]) <= 128 and isinstance(value["cancel_token"], str) and
        len(value["cancel_token"]) == 64 and all(c in "0123456789abcdef" for c in value["cancel_token"]),
        "request contract/identity changed")


def check_wire(ledger, number):
    store.require(type(number) is int and 1 <= number <= budget.MAX_CHECKS, "preflight identity")
    return ledger.path / f"check-{number:03d}.bin"


def replay(ledger):
    """Derive executable state exclusively from committed, validated events."""
    spec = deepcopy(ledger.spec)
    store.require(spec.get("budget_profile") in (None, budget.PROFILE), "unknown task budget profile")
    checked = spec.get("budget_profile") == budget.PROFILE
    state = dict(phase="READY", requests=0, tool_attempts=0, tool_results=0,
        tool_errors=0, read_repetitions=0, discarded_requests=0, runs=0,
        history=[dict(role="system", content=SYSTEM),
                 dict(role="user", content=spec["question"])],
        citations=set(), tool_ids=set(), results={}, planned=[], request=None, response=None,
        active_tool=None, answer=None, outcome=None, revision=ledger.initial,
        preflight_checks=0, planned_request=None, preflight_error=None,
        execution_spec=spec, migrations=0)
    ids, tokens, planning_ids, planning_tokens = set(), set(), set(), set()
    for event in ledger.events():
        kind, value = event["kind"], event["payload"]
        phase = state["phase"]
        store.require(kind == "migration" or phase not in ("COMPLETED", "FAILED", "ABANDONED"),
                      "event after terminal task")
        if kind == "migration":
            spec = migration.event(spec, state, value, ledger.meta["task_id"], ledger.meta["store_id"])
            checked = spec.get("budget_profile") == budget.PROFILE
            state.update(execution_spec=spec, migrations=state["migrations"] + 1)
        elif kind == "run":
            store.require(value == {"number": state["runs"] + 1} and
                          state["runs"] < 16 and phase not in
                          ("REQUEST_UNKNOWN", "TOOL_UNKNOWN"), "resume limit/state")
            state["runs"] += 1
        elif kind == "request":
            store.require(phase == ("PREFLIGHT_APPROVED" if checked else "READY") and
                state["requests"] < spec["max_requests"], "request budget/state")
            request_contract(value, spec, state["history"])
            store.require(value["request_id"] not in ids and value["cancel_token"] not in tokens and
                (not checked or value == state["planned_request"]),
                "request contract/identity changed")
            ids.add(value["request_id"]); tokens.add(value["cancel_token"])
            state.update(phase="REQUEST_UNKNOWN", request=value, planned_request=None,
                         requests=state["requests"] + 1)
        elif kind == "preflight_started":
            store.require(checked and phase == "READY" and state["preflight_checks"] < budget.MAX_CHECKS and
                state["requests"] < spec["max_requests"], "preflight state/budget")
            request_contract(value, spec, state["history"])
            store.require(value["request_id"] not in planning_ids | ids and
                value["cancel_token"] not in planning_tokens | tokens, "preflight credentials reused")
            planning_ids.add(value["request_id"]); planning_tokens.add(value["cancel_token"])
            state.update(phase="PREFLIGHT_PENDING", preflight_checks=state["preflight_checks"] + 1,
                         planned_request=value, preflight_error=None)
        elif kind == "preflight_result":
            store.require(checked and phase == "PREFLIGHT_PENDING", "unexpected preflight result")
            path = check_wire(ledger, state["preflight_checks"])
            store.require(store.private(path).st_size <= budget.MAX_CHECK_BYTES and
                chat.load_json(path.read_bytes()) == value, "preflight wire/receipt mismatch")
            budget.validate(value, state["planned_request"], spec["binding"])
            state.update(phase="PREFLIGHT_APPROVED")
        elif kind == "preflight_rejected":
            store.require(checked and phase == "PREFLIGHT_PENDING", "unexpected preflight rejection")
            budget.validate_rejection(value)
            state.update(phase="PREFLIGHT_REJECTED", preflight_error=value)
        elif kind == "preflight_reset":
            store.require(checked and phase in ("PREFLIGHT_PENDING", "PREFLIGHT_APPROVED", "PREFLIGHT_REJECTED")
                and value == {"revision": state["revision"]}, "unexpected preflight reset")
            state.update(phase="READY", planned_request=None, preflight_error=None)
        elif kind == "response":
            store.require(phase == "REQUEST_UNKNOWN", "unexpected response")
            wire_path = ledger.wire(state["requests"])
            info = store.private(wire_path)
            store.require(info.st_size <= store.MAX_WIRE_BYTES, "wire limit")
            raw = wire_path.read_bytes()
            parsed = (chat.read_sse if spec["stream"] else chat.read_json)(io.BytesIO(raw))
            store.require(value["wire_sha256"] == hashlib.sha256(raw).hexdigest()
                and value["completion"] == asdict(parsed) and
                parsed.id == state["request"]["request_id"] and
                parsed.model == spec["binding"]["model"], "wire/response identity")
            state.update(phase="RESPONSE_SAVED", response=value["completion"])
        elif kind == "plan":
            store.require(phase == "RESPONSE_SAVED", "unexpected tool plan")
            planned = chat.prepare_tool_calls(completion(state["response"]), VALIDATORS)
            store.require(value == [asdict(p) for p in planned] and
                state["requests"] < spec["max_requests"] and
                state["tool_attempts"] + len(planned) <= spec["max_tools"] and
                not state["tool_ids"].intersection(p.call_id for p in planned),
                "tool batch budget/validation")
            state["tool_ids"].update(p.call_id for p in planned)
            state.update(phase="TOOLS_PENDING", planned=value, results={})
        elif kind == "tool_started":
            pending = [p for p in state["planned"] if p["call_id"] not in state["results"]]
            store.require(phase == "TOOLS_PENDING" and pending and
                value == pending[0] and state["tool_attempts"] < spec["max_tools"],
                "tool start/budget")
            state.update(phase="TOOL_UNKNOWN", active_tool=value,
                         tool_attempts=state["tool_attempts"] + 1)
        elif kind == "tool_result":
            store.require(phase == "TOOL_UNKNOWN" and set(value) ==
                {"tool_call_id", "content"} and
                value["tool_call_id"] == state["active_tool"]["call_id"],
                "tool result identity")
            result = chat.load_json(value["content"])
            store.require(isinstance(result, dict) and type(result.get("ok")) is bool,
                          "tool result shape")
            for source in result.get("matches", []) + (
                    [result["source"]] if "source" in result else []):
                store.require(isinstance(source["citation"], str) and
                    len(source["sha256"]) == 64, "tool source identity")
                state["citations"].add(source["citation"])
            state["results"][value["tool_call_id"]] = value["content"]
            state["tool_results"] += 1
            state["tool_errors"] += not result["ok"]
            state.update(phase="TOOLS_PENDING", active_tool=None)
        elif kind == "history":
            store.require(phase == "TOOLS_PENDING", "unexpected history")
            history = chat.append_tool_results(state["history"],
                completion(state["response"]), state["results"])
            store.require(value == {"sha256": store.digest(history)}, "history mismatch")
            state.update(phase="READY", history=history, planned=[], results={},
                         response=None, request=None)
        elif kind == "final":
            store.require(phase == "RESPONSE_SAVED", "unexpected final")
            answer = completion(state["response"])
            history = chat.append_final(state["history"], answer)
            validate_answer(answer.message["content"], state["citations"], state["tool_results"])
            outcome = ("UNVERIFIED" if state["tool_errors"] == state["tool_results"]
                       else "ANSWER_WITH_TOOL_ERRORS" if state["tool_errors"] else "ANSWERED")
            store.require(value == dict(answer=answer.message["content"], outcome=outcome),
                          "business outcome mismatch")
            state.update(phase="COMPLETED", history=history, **value)
        elif kind == "resolve":
            store.require(value["expected_revision"] == state["revision"] and
                isinstance(value["reason"], str) and 0 < len(value["reason"]) <= 512,
                "resolution requires exact current state and reason")
            if value["decision"] == "discard-request":
                store.require(phase == "REQUEST_UNKNOWN" and
                    state["requests"] < spec["max_requests"], "no request to discard/budget")
                state.update(phase="READY", request=None, response=None,
                    discarded_requests=state["discarded_requests"] + 1)
            elif value["decision"] == "repeat-read":
                store.require(phase == "TOOL_UNKNOWN" and
                    state["active_tool"]["name"] in VALIDATORS and
                    state["tool_attempts"] + len(state["planned"]) - len(state["results"])
                        <= spec["max_tools"], "no allowed read repetition/budget")
                state.update(phase="TOOLS_PENDING", active_tool=None,
                    read_repetitions=state["read_repetitions"] + 1)
            else:
                store.require(value["decision"] == "abandon", "unknown resolution")
                state.update(phase="ABANDONED", outcome="ABANDONED")
        elif kind == "failed":
            store.require(set(value) == {"code"} and isinstance(value["code"], str),
                          "failure shape")
            state.update(phase="FAILED", outcome=value["code"])
        else:
            raise ValueError("unknown event: " + kind)
        state["revision"] = event["digest"]
    # Every wire must belong to a durable submission; a partial wire remains
    # private evidence and is never promoted into a response during recovery.
    for path in ledger.path.glob("wire-*.bin"):
        store.require(1 <= int(path.stem[5:]) <= state["requests"] and
                      store.private(path).st_size <= store.MAX_WIRE_BYTES,
                      "orphan/oversized wire")
    for path in ledger.path.glob("check-*.bin"):
        store.require(checked and 1 <= int(path.stem[6:]) <= state["preflight_checks"] and
            store.private(path).st_size <= budget.MAX_CHECK_BYTES, "orphan/oversized preflight wire")
    return state


def public(ledger, state):
    spec = state["execution_spec"]
    phase = state["phase"]
    actions = [] if phase in ("COMPLETED", "FAILED", "ABANDONED") else ["abandon"]
    if phase == "REQUEST_UNKNOWN" and state["requests"] < spec["max_requests"]:
        actions.append("discard-request")
    elif phase == "TOOL_UNKNOWN" and (state["tool_attempts"] + len(state["planned"]) -
                                      len(state["results"]) <= spec["max_tools"]):
        actions.append("repeat-read")
    elif phase not in ("REQUEST_UNKNOWN", "TOOL_UNKNOWN", "COMPLETED", "FAILED", "ABANDONED"):
        actions.append("resume")
    value = dict(task_id=ledger.meta["task_id"], state=state["phase"],
        revision=state["revision"], outcome=state["outcome"], answer=state["answer"],
        requests=state["requests"], tool_attempts=state["tool_attempts"],
        saved_tool_results=state["tool_results"], read_repetitions=state["read_repetitions"],
        discarded_requests=state["discarded_requests"], runs=state["runs"],
        remaining_requests=spec["max_requests"] - state["requests"],
        remaining_tools=spec["max_tools"] - state["tool_attempts"],
        allowed_actions=actions, automatic_retry=False)
    if spec.get("budget_profile") == budget.PROFILE:
        value.update(max_tokens=spec["max_tokens"], preflight_checks=state["preflight_checks"],
                     preflight_error=state["preflight_error"])
        if state["runs"] >= 16 and "resume" in actions:
            actions.remove("resume")
    if state["migrations"]:
        value["migrations"] = state["migrations"]
    return value


def check_binding(ledger, binding, state=None):
    spec = (state or replay(ledger))["execution_spec"]
    store.require(spec["binding"] == binding, "task/client binding changed", code='binding_changed')
    store.require(spec["runtime_sources"] == sources(),
                  "task runtime changed; explicit migration required", code='runtime_changed')


def validate_next(ledger, kind, value):
    events = ledger.events()
    class Preview:
        spec, initial, path, meta = ledger.spec, ledger.initial, ledger.path, ledger.meta
        wire = ledger.wire
        def events(self):
            return events + [dict(kind=kind, payload=value, digest="preview")]
    replay(Preview())


def resolve(ledger, binding, decision, expected_revision, reason):
    check_binding(ledger, binding)
    state = replay(ledger)
    store.require(decision in ("discard-request", "repeat-read", "abandon") and
        isinstance(reason, str) and 0 < len(reason) <= 512 and
        isinstance(expected_revision, str), "resolution input", code="invalid_arguments")
    store.require(expected_revision == state["revision"] and
        decision in public(ledger, state)["allowed_actions"],
        "resolution requires exact current state and reason", code="resolution_conflict")
    value = dict(decision=decision, expected_revision=expected_revision, reason=reason)
    # Validate the prospective transition without publishing an invalid event.
    validate_next(ledger, "resolve", value)
    ledger.append("resolve", value)
    return public(ledger, replay(ledger))


def run(ledger, binding, client, *, records=None, checkpoint=None):
    state = replay(ledger)
    check_binding(ledger, binding, state)
    spec = state["execution_spec"]
    if state["phase"] in ("COMPLETED", "FAILED", "ABANDONED",
                          "REQUEST_UNKNOWN", "TOOL_UNKNOWN"):
        return public(ledger, state)
    store.require(state["runs"] < 16, "explicit resume budget exhausted", code='task_budget')
    records = records or ProjectRecords(store.ROOT)
    checkpoint = checkpoint or (lambda *args: None)
    ledger.append("run", dict(number=state["runs"] + 1))

    def commit(kind, value):
        validate_next(ledger, kind, value)
        seq = ledger.append(kind, value)
        checkpoint(kind, seq, ledger)

    state = replay(ledger)
    if state["phase"] in ("PREFLIGHT_PENDING", "PREFLIGHT_APPROVED", "PREFLIGHT_REJECTED"):
        # A read-only preflight reserves nothing. A new explicit run must not
        # reuse an approval from a previous process or repeat a model attempt.
        commit("preflight_reset", dict(revision=state["revision"]))
    caps = client.capabilities(model=binding["model"], protocol_sha256=binding["protocol_sha256"],
                               template_sha256=binding["template_sha256"])
    chat.validate_capabilities(caps, binding["model"], protocol_sha256=binding["protocol_sha256"],
                               template_sha256=binding["template_sha256"])
    store.require(caps["tools"].get("strict") is True and
                  "required" in caps["tools"]["tool_choice"] and
                  "none" in caps["tools"]["tool_choice"] and
                  False in caps["tools"]["parallel_tool_calls"], "strict tool controls unavailable")

    while True:
        state = replay(ledger)
        phase = state["phase"]
        if phase == "READY":
            if state["requests"] >= spec["max_requests"]:
                commit("failed", dict(code="request_budget_exhausted"))
                return public(ledger, replay(ledger))
            request = chat.make_request(state["history"], TOOLS,
                model=binding["model"], stream=spec["stream"], max_tokens=spec["max_tokens"])
            request["parallel_tool_calls"] = False
            request["tool_choice"] = ("required" if not state["tool_results"] else
                "none" if state["requests"] + 1 == spec["max_requests"] or
                state["tool_attempts"] >= spec["max_tools"] else "auto")
            if spec.get("budget_profile") == budget.PROFILE:
                if state["preflight_checks"] >= budget.MAX_CHECKS:
                    commit("failed", dict(code="preflight_budget_exhausted"))
                    return public(ledger, replay(ledger))
                commit("preflight_started", request)
                path = check_wire(ledger, state["preflight_checks"] + 1)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                try:
                    with os.fdopen(fd, "wb", buffering=0) as wire:
                        used = 0
                        def check_sink(data):
                            nonlocal used
                            store.require(used + len(data) <= budget.MAX_CHECK_BYTES, "preflight wire budget")
                            wire.write(data); used += len(data)
                        value = client.preflight(request, sink=check_sink)
                        os.fsync(wire.fileno())
                    store.sync(ledger.path)
                    budget.validate(value, request, binding)
                except (OSError, ValueError, chat.RemoteError) as error:
                    commit("preflight_rejected", budget.rejection(error))
                    return public(ledger, replay(ledger))
                commit("preflight_result", value)
            else:
                commit("request", request)
        elif phase == "PREFLIGHT_APPROVED":
            commit("request", state["planned_request"])
        elif phase == "REQUEST_UNKNOWN":
            # This state is entered in this invocation only after committing
            # the submission intent. An explicit resume returns it above.
            request = state["request"]
            wire_path = ledger.wire(state["requests"])
            fd = os.open(wire_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb", buffering=0) as wire:
                used = 0
                def sink(data):
                    nonlocal used
                    store.require(used + len(data) <= store.MAX_WIRE_BYTES, "wire budget exceeded")
                    wire.write(data); used += len(data)
                    checkpoint("wire", state["requests"], ledger)
                answer = client.complete(request, sink=sink)
                wire.flush(); os.fsync(wire.fileno())
            store.sync(ledger.path)
            store.require(answer.id == request["request_id"] and
                          answer.model == binding["model"], "response identity")
            commit("response", dict(completion=asdict(answer),
                wire_sha256=hashlib.sha256(wire_path.read_bytes()).hexdigest()))
        elif phase == "RESPONSE_SAVED":
            answer = completion(state["response"])
            try:
                if answer.finish_reason == "stop":
                    validate_answer(answer.message["content"], state["citations"], state["tool_results"])
                    outcome = ("UNVERIFIED" if state["tool_errors"] == state["tool_results"] else
                               "ANSWER_WITH_TOOL_ERRORS" if state["tool_errors"] else "ANSWERED")
                    final = dict(answer=answer.message["content"], outcome=outcome)
                    chat.append_final(state["history"], answer)
                    kind, value = "final", final
                else:
                    planned = chat.prepare_tool_calls(answer, VALIDATORS)
                    store.require(state["requests"] < spec["max_requests"] and
                        state["tool_attempts"] + len(planned) <= spec["max_tools"] and
                        not state["tool_ids"].intersection(p.call_id for p in planned),
                        "tool batch exceeds remaining budget")
                    kind, value = "plan", [asdict(p) for p in planned]
            except (ValueError, KeyError, TypeError):
                commit("failed", dict(code="response_or_business_contract"))
                return public(ledger, replay(ledger))
            commit(kind, value)
        elif phase == "TOOLS_PENDING":
            pending = [p for p in state["planned"] if p["call_id"] not in state["results"]]
            if not pending:
                history = chat.append_tool_results(state["history"], completion(state["response"]), state["results"])
                commit("history", dict(sha256=store.digest(history)))
                continue
            invocation = chat.ToolInvocation(**pending[0])
            # Revalidate the entire saved batch before executing any remaining tool.
            chat.prepare_tool_calls(completion(state["response"]), VALIDATORS)
            store.require(state["tool_attempts"] < spec["max_tools"], "tool budget")
            commit("tool_started", asdict(invocation))
            value = records.execute(invocation)
            checkpoint("tool_returned", state["tool_attempts"] + 1, ledger)
            value["application_budget"] = dict(
                remaining_model_requests=spec["max_requests"] - state["requests"],
                remaining_tool_calls=spec["max_tools"] - state["tool_attempts"] - 1,
                final_response_required=state["requests"] + 1 == spec["max_requests"] or
                    state["tool_attempts"] + 1 == spec["max_tools"])
            commit("tool_result", dict(tool_call_id=invocation.call_id,
                content=json.dumps(value, ensure_ascii=False, allow_nan=False)))
        else:
            return public(ledger, state)
