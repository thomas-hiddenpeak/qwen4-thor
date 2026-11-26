"""Qualify dependent program calls against a closed store of real SDK responses."""
import hashlib
import json
from pathlib import Path
import re
import subprocess

from tools.serve_recovery import profile_live_reuse as reuse
from tools.serve_responses import approval_identifiers_profiles as approvals
from tools.serve_responses import client_shell_profiles, environment_lifecycle_profiles as lifecycle
from tools.serve_service import package

KEY = "program_fork_handoff"
ACTOR = Path(__file__).with_name("program_fork_handoff_sdk.py")
PROOF = lifecycle.ROOT / "docs/evidence/serve-program-forks-20261126/business.json"
RELEASE = client_shell_profiles.PROOF.with_name("delivery.json")


def prepare(out, build, deployment, interpreter, source):
    proof = package.read(PROOF)
    sdk = subprocess.check_output([interpreter, "-IB", "-c", "import openai; print(openai.__version__)"],
                                  text=True, timeout=15).strip()
    package.require(proof["run"]["status"] == "PASS" and sdk == proof["client"]["sdk"]
                    and proof["client"]["sibling_status"] == 400 and proof["client"]["peer"] == 404
                    and proof["product"]["binary"] == package.sha(build / "q4t")
                    and proof["product"]["ingress"] == package.sha(build / "q4t-ingress"),
                    "program branch store or executable differs from its SDK acceptance")
    lifecycle.prepare_candidate(out, build, deployment, RELEASE, reuse.CLIENT_SHELL_REVIEWED, KEY,
                                [ACTOR, PROOF, RELEASE, Path(__file__), Path(approvals.__file__)])
    store = out / "state/application-objects"
    approvals.seed(source, store, proof)
    for name in ("primary", "light"):
        path = out / name / "plan.json"
        plan = package.read(path)
        command = plan["command"]
        package.require(Path(command[command.index("--object-store-dir") + 1]) == store,
                        "both capacities must use the original program store")
        plan[KEY]["interpreter"] = str(Path(interpreter).absolute())
        package.save(path, plan)


def check(row, previous=None):
    old = package.read(PROOF)["client"]
    release = package.read(RELEASE)
    revision = release["publication"]["publication_revision"]
    branches = old["branches"]
    hashes = row["wire_sha256"]
    expected = {"original": old["captures"]["initial"]}
    history = {old["original"], old["retry"]}
    for branch in branches:
        n = branch["revision"]
        expected[f"pending_{n}"] = old["captures"][f"stored-{n}"]
        expected[f"answer_{n}"] = old["captures"][f"answer-{n}"]
        history.update((branch["pending"], branch["continuation"]))
    labels = expected.keys() | {"branch", "create", "current_response", "current_inputs", "independent"}
    if previous is not None:
        labels |= {"restored_response", "restored_inputs"}
    package.require(len(branches) == 2 and len(history) == 6 and row["original"] == old["original"]
                    and row["follow"] == branches[0]["continuation"] and row["sdk"] == old["sdk"]
                    and revision not in {b["revision"] for b in branches}
                    and row["result"] == f"{revision}:{release['declared_methods']}"
                    and row["peer"] == 404 and row["independent_chat"] == "720041"
                    and labels == hashes.keys() and all(hashes[k] == v for k, v in expected.items())
                    and all(re.fullmatch(r"[0-9a-f]{64}", v) for v in hashes.values())
                    and hashes["create"] == hashes["current_response"],
                    "original program bytes, release result or client isolation changed")
    package.require(re.fullmatch(r"call_p_[0-9a-f]{56}", row["child"])
                    and row["child_item"] == "fc_" + hashlib.sha256(row["child"].encode()).hexdigest()[:56]
                    and row["child"] not in {old["first"], *(b["call_id"] for b in branches)}
                    and row["child_item"] not in {b["child_id"] for b in branches},
                    "fresh dependent call did not receive distinct identities")
    if previous is None:
        package.require(row["restored"] is None, "unexpected prior profile handoff")
    else:
        package.require(package.same_json(row["restored"], previous["saved"])
                        and (row["child"], row["child_item"]) == (previous["child"], previous["child_item"]),
                        "restart did not preserve the prior response and deterministic call identity")
    for label, value in (("restored", row["restored"]), ("current", row["saved"])):
        if value is None:
            continue
        ids = {value["id"], value["pending"]}
        package.require(len(ids) == 2 and not history.intersection(ids)
                        and all(re.fullmatch(r"resp_[A-Za-z0-9_-]+", item) for item in ids)
                        and all(value[k + "_sha256"] == hashes[label + "_" + k] for k in ("inputs", "response")),
                        "incorrect stored response, parent, or input page")
        history.update(ids)


def exercise(params, interpreter, errors, captures, previous=None):
    release = package.read(RELEASE)
    request = dict(params, capture=str(captures), original=package.read(PROOF)["client"],
                   revision=release["publication"]["publication_revision"], methods=release["declared_methods"])
    if previous is not None:
        request["previous"] = previous["saved"]
    run = subprocess.run([interpreter, "-IB", ACTOR], input=json.dumps(request), text=True,
                         stdout=subprocess.PIPE, stderr=errors, timeout=170, check=True)
    row = json.loads(run.stdout)
    check(row, previous)
    return row
