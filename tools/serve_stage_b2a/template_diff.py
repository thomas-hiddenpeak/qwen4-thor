"""Extend the native/Jinja oracle with reasoning aliases and null history."""
import copy
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("b2_native_oracle",
    ROOT / "tools/serve_stage_b1/template_diff.py")
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)
original_cases, original_reference = b.cases, b.reference_request


def cases():
    rows = original_cases()
    additions = []
    for row in rows:
        if "reject_param" in row or not any(m.get("reasoning_content") is not None
                for m in row["request"]["messages"]):
            continue
        for variant in ("alias", "equal", "legacy-null", "canonical-null"):
            adapted = copy.deepcopy(row)
            adapted.pop("legacy", None)
            adapted.pop("expected", None)
            adapted["id"] = variant + "-" + row["id"]
            for key in ("reference_messages",):
                adapted.pop(key, None)
            for message in adapted["request"]["messages"]:
                if "reasoning_content" not in message:
                    continue
                value = message["reasoning_content"]
                if variant == "canonical-null":
                    message["reasoning"] = None
                else:
                    message["reasoning"] = value
                    if variant == "alias":
                        del message["reasoning_content"]
                    elif variant == "legacy-null":
                        message["reasoning_content"] = None
            # Keep the oracle's independently ordered tool-result history.
            if "reference_messages" in row:
                adapted["reference_messages"] = copy.deepcopy(row["reference_messages"])
            additions.append(adapted)
    for field in ("reasoning", "reasoning_content"):
        additions.append(dict(id=field + "-empty-history", request=dict(messages=[
            dict(role="user", content="q"), dict(role="assistant", content="a",
            **{field: None})], enable_thinking=False), contents=["q", "a"]))
    return rows + additions


def reference_request(row):
    # The checkpoint's single native slot is reasoning_content. The request
    # alias is normalized only in this independent Python/Jinja oracle.
    row = copy.deepcopy(row)
    for messages in (row["request"]["messages"], row.get("reference_messages", [])):
        for message in messages:
            if message.get("reasoning") is not None:
                message["reasoning_content"] = message["reasoning"]
            message.pop("reasoning", None)
            if message.get("reasoning_content") is None:
                message.pop("reasoning_content", None)
    return original_reference(row)


if __name__ == "__main__":
    b.cases, b.reference_request = cases, reference_request
    b.main()
