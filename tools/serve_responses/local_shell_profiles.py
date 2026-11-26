"""Qualify durable client-executed shell turns against their captured HTTP bytes."""
import json
from pathlib import Path
import re
import subprocess

from tools.serve_recovery import profile_live_reuse as reuse
from tools.serve_responses import approval_identifiers_profiles as approvals
from tools.serve_responses import environment_lifecycle_profiles as lifecycle
from tools.serve_service import package

KEY = "local_shell_handoff"
RELEASE = approvals.PROOF.with_name("delivery.json")
PROOF = lifecycle.ROOT / "docs/evidence/serve-local-shell-20261126/business.json"
ACTOR = Path(__file__).with_name("local_shell_handoff_sdk.py")


def prepare(out, build, deployment, interpreter, source):
    proof = package.read(PROOF)
    version = subprocess.check_output([interpreter, "-IB", "-c",
        "import openai; print(openai.__version__)"], text=True, timeout=15).strip()
    package.require(proof["run"]["status"] == "PASS" and proof["client"]["reads"] == 1
                    and proof["client"]["sdk"] == version
                    and proof["product"]["binary"] == package.sha(build / "q4t")
                    and proof["product"]["ingress"] == package.sha(build / "q4t-ingress"),
                    "the shell fixture and candidate must match the verified build")
    lifecycle.prepare_candidate(out, build, deployment, RELEASE, reuse.APPROVAL_IDS_REVIEWED,
                                KEY, [PROOF, RELEASE, ACTOR, Path(__file__), Path(approvals.__file__)])
    store = out / "state/application-objects"
    approvals.seed(source, store, proof)
    for name in ("primary", "light"):
        path = out / name / "plan.json"
        plan = package.read(path)
        command = plan["command"]
        package.require(Path(command[command.index("--object-store-dir") + 1]) == store,
                        "both profiles must share the qualified historical store")
        plan[KEY]["interpreter"] = str(Path(interpreter).absolute())
        package.save(path, plan)


def check(row, previous=None):
    source = package.read(PROOF)["client"]
    hashes = row["wire_sha256"]
    def check_saved(saved, label):
        package.require(re.fullmatch(r"resp_[A-Za-z0-9_-]+", saved["id"])
                        and saved["id"] not in (source["response"], source["follow"])
                        and hashes[label + "_response"] == saved["response_sha256"]
                        and hashes[label + "_inputs"] == saved["inputs_sha256"],
                        "stored continuation changed")
    labels = {"call", "completed", "inputs", "create", "independent", "current_response", "current_inputs"}
    if previous is not None:
        labels.update(("restored_response", "restored_inputs"))
    package.require(row["sdk"] == source["sdk"] and row["answer"] == source["answer"]
                    and row["independent_chat"] == "720041" and row["peer"] == 404
                    and (row["original"], row["follow"]) == (source["response"], source["follow"])
                    and hashes.keys() == labels and all(re.fullmatch(r"[0-9a-f]{64}", v) for v in hashes.values())
                    and all(hashes[k] == source["captures"][k + ".json"] for k in ("call", "completed", "inputs")),
                    "client, original owner-scoped records, or capture identities changed")
    check_saved(row["saved"], "current")
    if previous is None:
        package.require(row["restored"] is None, "unexpected prior continuation")
    else:
        package.require(package.same_json(row["restored"], previous["saved"])
                        and row["saved"]["id"] != previous["saved"]["id"],
                        "handoff lost or reused the previous continuation")
        check_saved(row["restored"], "restored")


def exercise(params, interpreter, errors, captures, previous=None):
    request = dict(params, capture=str(captures), original=package.read(PROOF)["client"])
    if previous is not None:
        request["previous"] = previous["saved"]
    probe = subprocess.run([interpreter, "-IB", ACTOR], input=json.dumps(request), text=True,
                           stdout=subprocess.PIPE, stderr=errors, timeout=160, check=True)
    row = json.loads(probe.stdout)
    check(row, previous)
    return row
