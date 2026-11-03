"""Validate client-scoped read-only admission without silently clipping output."""
import re

from tools.project_tasks import store

PROFILE = "q4t-project-budget-v1"
MAX_CHECKS = 32
MAX_CHECK_BYTES = 65536


class BudgetError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def needed(condition, code="preflight_contract"):
    if not condition:
        raise BudgetError(code)


def integer(value, lower=1, upper=1048576):
    return type(value) is int and lower <= value <= upper


def validate(value, request, binding):
    needed(isinstance(value, dict))
    needed(value.get("object") == "q4t.chat.preflight" and
           value.get("profile") == "q4t-preflight-v1" and
           value.get("model") == binding["model"] and
           value.get("protocol_source_sha256") == binding["protocol_sha256"])
    needed(value.get("execution_guaranteed") is False and
           "prompt" not in value and "token_ids" not in value)
    output, admission, checks = (value.get(key) for key in ("output", "admission", "checks"))
    needed(all(isinstance(item, dict) for item in (output, admission, checks)))
    needed(output.get("source") == "max_tokens" and
           integer(output.get("requested_tokens")) and
           output["requested_tokens"] == request["max_tokens"] and
           output.get("counts_reasoning_and_forced_tokens") is True)
    needed(integer(output.get("capped_tokens")) and integer(output.get("available_tokens")))
    needed(output["capped_tokens"] == output["available_tokens"] == request["max_tokens"],
           "preflight_budget_reduced")
    needed(integer(value.get("prompt_tokens")) and integer(value.get("max_context_tokens")) and
           value["prompt_tokens"] + request["max_tokens"] <= value["max_context_tokens"])
    limits = value.get("client_limits")
    needed(isinstance(limits, dict), "preflight_client_limits_missing")
    needed(integer(limits.get("max_prompt_tokens")) and integer(limits.get("max_output_tokens")))
    needed(value["prompt_tokens"] <= limits["max_prompt_tokens"] and
           request["max_tokens"] <= limits["max_output_tokens"], "preflight_client_budget_mismatch")
    slots = admission.get("eligible_slots")
    needed(admission.get("policy") in ("explicit_full_budget", "uniform_context_clip") and
           admission.get("slot_reserved") is False and admission.get("availability_checked") is False and
           isinstance(slots, list) and 0 < len(slots) <= 1024 and
           all(integer(slot, 0) for slot in slots) and len(set(slots)) == len(slots))
    needed(integer(admission.get("required_capacity"), 0))
    if admission["policy"] == "explicit_full_budget":
        needed(admission["required_capacity"] == value["prompt_tokens"] + request["max_tokens"])
    for name in ("input", "template", "tokenizer", "budget", "structured_requested"):
        needed(checks.get(name) is True)
    for name in ("grammar_compiled", "cache_accessed", "request_registered", "gpu_executed"):
        needed(checks.get(name) is False)
    return value


def rejection(error):
    """Return actionable, bounded fields without exposing a remote error body."""
    def safe(value, pattern, maximum):
        return value if isinstance(value, str) and len(value) <= maximum and re.fullmatch(pattern, value) else None
    status = getattr(error, "status", None)
    status = status if type(status) is int and 100 <= status <= 599 else None
    return dict(category="remote_error" if status is not None else
        "invalid_response" if isinstance(error, ValueError) else "transport_error",
        http_status=status, code=safe(getattr(error, "code", None), r"[a-zA-Z0-9_]+", 64),
        param=safe(getattr(error, "param", None), r"[a-zA-Z0-9_.\[\]]+", 128))


def validate_rejection(value):
    store.require(isinstance(value, dict) and set(value) ==
        {"category", "http_status", "code", "param"} and value["category"] in
        ("remote_error", "invalid_response", "transport_error"), "invalid preflight rejection")
    store.require(value["http_status"] is None or integer(value["http_status"], 100, 599),
                  "invalid rejection status")
    for name, pattern, maximum in (("code", r"[a-zA-Z0-9_]+", 64),
                                    ("param", r"[a-zA-Z0-9_.\[\]]+", 128)):
        item = value[name]
        store.require(item is None or isinstance(item, str) and len(item) <= maximum and
                      re.fullmatch(pattern, item), "invalid rejection field")
