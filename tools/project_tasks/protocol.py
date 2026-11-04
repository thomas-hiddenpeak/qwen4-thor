"""Reviewed directed protocol upgrades; historical receipts remain replayable."""
from copy import deepcopy
import json
import re

from tools.project_tasks import store

REGISTRY = "tools/project_tasks/compatible_protocols.json"
CONTRACT = "q4t-project-readonly-protocol-v1"


def checksum(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def same_identity(before, after):
    store.require(set(before) == set(after) and
        checksum(before.get("protocol_sha256")) and checksum(after.get("protocol_sha256")) and
        before["protocol_sha256"] != after["protocol_sha256"] and
        {key: value for key, value in before.items() if key != "protocol_sha256"} ==
        {key: value for key, value in after.items() if key != "protocol_sha256"},
        "protocol migration changes only the protocol; client, origin, IP, CA, key, model and template stay fixed",
        code="binding_changed")


def validate(review, before, after):
    """Check a committed receipt without consulting a mutable release registry.

    Authorization occurs in reviewed() when preparing/applying a new plan.
    Keeping its complete receipt lets old ledgers replay after registry changes.
    """
    same_identity(before, after)
    store.require(isinstance(review, dict) and set(review) == {
        "release", "contract", "from_protocol_sha256", "to_protocol_sha256",
        "model", "template_sha256", "evidence", "archive_sha256", "basis"} and
        review["contract"] == CONTRACT and
        review["from_protocol_sha256"] == before["protocol_sha256"] and
        review["to_protocol_sha256"] == after["protocol_sha256"] and
        review["model"] == after["model"] and
        review["template_sha256"] == after["template_sha256"] and
        checksum(review["template_sha256"]) and checksum(review["archive_sha256"]) and
        isinstance(review["release"], str) and 0 < len(review["release"]) <= 128 and
        isinstance(review["basis"], str) and 0 < len(review["basis"]) <= 512 and
        isinstance(review["evidence"], str) and
        re.fullmatch(r"docs/evidence/[a-z0-9-]{1,128}/records\.tar\.gz", review["evidence"]) is not None,
        "protocol review receipt does not match the transition", code="protocol_unsupported")


def reviewed(before, after):
    same_identity(before, after)
    registry = json.loads((store.ROOT / REGISTRY).read_bytes())
    store.require(registry["format"] == "q4t.project.task.protocols.v1" and
        isinstance(registry["releases"], list) and len(registry["releases"]) <= 64,
        "protocol compatibility registry format")
    selected = [row for row in registry["releases"] if
        row["from_protocol_sha256"] == before["protocol_sha256"] and
        row["to_protocol_sha256"] == after["protocol_sha256"] and
        row["model"] == after["model"] and row["template_sha256"] == after["template_sha256"]]
    store.require(len(selected) == 1, "no unique reviewed protocol transition",
                  code="protocol_unsupported")
    validate(selected[0], before, after)
    return deepcopy(selected[0])
