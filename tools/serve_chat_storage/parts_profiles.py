"""Qualify the stored multimodal message projection across service capacities."""
import json
from pathlib import Path
import re
import subprocess

from tools.serve_recovery import profile_live_reuse as reuse
from tools.serve_responses import environment_lifecycle_profiles as lifecycle
from tools.serve_service import package

KEY = "stored_parts_handoff"
RELEASE = lifecycle.ROOT/"docs/evidence/serve-null-assistant-history-20261126/delivery.json"
PROOF = lifecycle.ROOT/"docs/evidence/serve-stored-content-parts-20261126/business.json"
ACTOR = Path(__file__).with_name("parts_handoff_sdk.py")
DIRECT = Path(__file__).with_name("native_parts_sdk.py")


def prepare(out, build, deployment, interpreter):
    proof = package.read(PROOF)
    version = subprocess.check_output([interpreter, "-IB", "-c",
        "import openai; print(openai.__version__)"], text=True, timeout=15).strip()
    package.require(proof["stage"] == "native_tls_business_verified" and proof["run"]["status"] == "PASS"
                    and version == proof["client"]["sdk"] and proof["client"]["color_correct"] is True
                    and proof["sdk_driver"] == package.sha(DIRECT) and proof["engine"] == package.sha(build/"q4t")
                    and proof["ingress"] == package.sha(build/"q4t-ingress"),
                    "stored part qualification requires the accepted visual HTTPS build and SDK")
    lifecycle.prepare_candidate(out, build, deployment, RELEASE, reuse.NULL_HISTORY_REVIEWED,
                                KEY, [PROOF, RELEASE, ACTOR, DIRECT, Path(__file__)])
    for name in ("primary", "light"):
        path = out/name/"plan.json"
        plan = package.read(path)
        command = plan["command"]
        package.require("--allow-media" in command and Path(command[command.index("--object-store-dir")+1])
                        == out/"state/application-objects", "shared visual history required")
        plan[KEY]["interpreter"] = str(Path(interpreter).absolute())
        package.save(path, plan)


def check(row, previous=None):
    approved = package.read(PROOF)["client"]
    package.require(row["sdk"] == approved["sdk"] and row["reply"].casefold().rstrip(".") == "green"
                    and row["finish"] == "stop" and row["independent_chat"] == approved["independent_chat"],
                    "missing genuine visual result or independent Chat")
    saved = row["saved"]
    ids = saved["message_ids"]
    package.require(type(ids) is list and len(ids) == 3 and len(set(ids)) == 3
                    and all(re.fullmatch(r"chatmsg_[0-9a-f]{64}", item) for item in ids)
                    and re.fullmatch(r"chatcmpl-[A-Za-z0-9_-]+", saved["chat_id"])
                    and re.fullmatch(r"[0-9a-f]{64}", saved["history_sha256"])
                    and saved["cursor"] == ids[1] and package.same_json(saved["peer"], 404),
                    "stored page lacks original IDs, cursor, digest or tenant isolation")
    if previous is None:
        package.require(row["restored"] is None, "unexpected prior page")
    else:
        check(previous)
        prior = previous["saved"]
        package.require(package.same_json(row["restored"], prior)
                        and prior["chat_id"] != saved["chat_id"] and set(ids).isdisjoint(prior["message_ids"])
                        and saved["history_sha256"] != prior["history_sha256"],
                        "changed original page or reused new message identities")


def exercise(params, interpreter, errors, captures, previous=None):
    request = dict(params, capture=str(captures), handoff=True, proof=str(PROOF))
    if previous is not None:
        request["previous"] = previous["saved"]
    output = subprocess.run([interpreter, "-IB", ACTOR], input=json.dumps(request), text=True,
                            stdout=subprocess.PIPE, stderr=errors, timeout=160, check=True)
    row = json.loads(output.stdout)
    check(row, previous)
    return row
