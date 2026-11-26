"""Verify original shell histories and fresh program results across service capacities."""
import json
from pathlib import Path
import re
import subprocess

from tools.serve_recovery import profile_live_reuse as reuse
from tools.serve_responses import approval_identifiers_profiles as approvals
from tools.serve_responses import environment_lifecycle_profiles as lifecycle
from tools.serve_responses import client_patch_profiles
from tools.serve_service import package

KEY = "client_shell_handoff"
ACTOR = Path(__file__).with_name("client_shell_handoff_sdk.py")
PROOF = lifecycle.ROOT / "docs/evidence/serve-shell-history-20261126/business.json"
RELEASE = client_patch_profiles.PROOF.with_name("delivery.json")


def prepare(out, build, deployment, interpreter, source):
    proof = package.read(PROOF)
    sdk = subprocess.check_output([interpreter, "-IB", "-c", "import openai; print(openai.__version__)"],
                                  text=True, timeout=15).strip()
    repair_path = PROOF.with_name("fork-repair.json")
    repair = package.read(repair_path)
    package.require(proof["run"]["status"] == "PASS" and proof["client"]["sdk"] == sdk
                    and proof["client"]["reads"] == 1 and proof["client"]["program"]["exit_code"] == 1
                    and repair["prior_business_sha256"] == package.sha(PROOF)
                    and package.same_json(repair["original_product"], proof["product"])
                    and repair["product"]["binary"] == package.sha(build / "q4t")
                    and repair["product"]["ingress"] == package.sha(build / "q4t-ingress"),
                    "original shell history or repaired candidate identity differs")
    lifecycle.prepare_candidate(out, build, deployment, RELEASE, reuse.PATCH_REVIEWED,
                                KEY, [PROOF, repair_path, RELEASE, ACTOR, Path(__file__), Path(approvals.__file__)])
    store = out / "state/application-objects"
    approvals.seed(source, store, proof)
    for name in ("primary", "light"):
        path = out / name / "plan.json"
        plan = package.read(path)
        command = plan["command"]
        package.require(Path(command[command.index("--object-store-dir") + 1]) == store,
                        "profiles must share the qualified shell histories")
        plan[KEY]["interpreter"] = str(Path(interpreter).absolute())
        package.save(path, plan)


def check(row, previous=None):
    direct = package.read(PROOF)["client"]
    program = direct["program"]
    required = {prefix + "_" + field: direct["captures"][name]
                for prefix, names in (("direct", ("call.json", "completed.json", "inputs.json")),
                                      ("program", ("program-pending.json", "program-stored.json", "program-inputs.json")))
                for field, name in zip(("call", "completed", "inputs"), names, strict=True)}
    originals = [p[k] for p in (direct, program) for k in ("response", "follow")]
    hashes = row["wire_sha256"]
    labels = required.keys() | {"create", "independent", "current_response", "current_inputs"}
    if previous is not None:
        labels |= {"restored_response", "restored_inputs"}
    package.require(labels == hashes.keys() and all(hashes[k] == v for k, v in required.items())
                    and all(re.fullmatch(r"[0-9a-f]{64}", v) for v in hashes.values())
                    and [row[k] for k in ("original", "follow", "program", "program_follow")] == originals
                    and len(set(originals)) == 4 and row["sdk"] == direct["sdk"]
                    and row["result"] == "exit=0 count=2" and program["answer"] == "exit=1 count=0"
                    and row["peer"] == 404 and row["independent_chat"] == "720041",
                    "historical shell identity, client or resumed result changed")
    for label, value in (("current", row["saved"]), ("restored", row["restored"])):
        if label == "restored":
            if previous is None:
                package.require(value is None, "unexpected previous continuation")
                continue
            package.require(package.same_json(value, previous["saved"]) and value["id"] != row["saved"]["id"],
                            "the current phase did not preserve the previous continuation")
        package.require(re.fullmatch(r"resp_[A-Za-z0-9_-]+", value["id"]) and value["id"] not in originals
                        and all(value[k + "_sha256"] == hashes[label + "_" + k] for k in ("response", "inputs")),
                        "shell continuation identity changed")


def exercise(params, interpreter, errors, captures, previous=None):
    direct = package.read(PROOF)["client"]
    request = dict(params, capture=str(captures), original=direct, program=direct["program"])
    if previous is not None:
        request["previous"] = previous["saved"]
    probe = subprocess.run([interpreter, "-IB", ACTOR], input=json.dumps(request), text=True,
                           stdout=subprocess.PIPE, stderr=errors, timeout=160, check=True)
    row = json.loads(probe.stdout)
    check(row, previous)
    return row
