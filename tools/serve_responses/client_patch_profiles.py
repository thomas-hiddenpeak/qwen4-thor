"""Qualify original direct and program patch histories in both service capacities."""
import json
from pathlib import Path
import re
import subprocess
import tempfile

from tools.serve_recovery import profile_live_reuse as reuse
from tools.serve_responses import approval_identifiers_profiles as approvals
from tools.serve_responses import environment_lifecycle_profiles as lifecycle
from tools.serve_responses import local_shell_profiles
from tools.serve_service import package

KEY = "client_patch_handoff"
ACTOR = Path(__file__).with_name("client_patch_handoff_sdk.py")
PROOF = lifecycle.ROOT / "docs/evidence/serve-client-patch-20261126/business.json"
PROGRAM = PROOF.with_name("program.json")
RELEASE = local_shell_profiles.PROOF.with_name("delivery.json")


def seed_histories(store, sources):
    """Stage two closed fixtures atomically, preserving the original program keys."""
    package.require(not store.exists() and not store.is_symlink(), "expected a fresh store")
    store.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=store.parent) as tmp:
        direct, program = (Path(tmp) / key for key in ("direct", "program"))
        approvals.seed(sources[0][0], direct, sources[0][1])
        approvals.seed(sources[1][0], program, sources[1][1])
        common = {p.name for p in direct.iterdir()} & {p.name for p in program.iterdir()}
        package.require(common == {".q4t-format"} and
                        package.sha(direct / ".q4t-format") == package.sha(program / ".q4t-format"),
                        "fixtures have overlapping object identities or incompatible formats")
        for path in program.iterdir():
            if path.name not in common:
                path.rename(direct / path.name)
        direct.rename(store)


def prepare(out, build, deployment, interpreter, direct_source, program_source):
    direct, program = package.read(PROOF), package.read(PROGRAM)
    sdk = subprocess.check_output([interpreter, "-IB", "-c", "import openai; print(openai.__version__)"],
                                  text=True, timeout=15).strip()
    package.require(direct["run"]["status"] == program["run"]["status"] == "PASS"
                    and direct["client"]["sdk"] == program["client"]["sdk"] == sdk
                    and direct["client"]["writes"] == program["client"]["failed_operations"] == 1
                    and package.same_json(direct["product"], program["product"])
                    and direct["product"]["binary"] == package.sha(build / "q4t")
                    and direct["product"]["ingress"] == package.sha(build / "q4t-ingress"),
                    "patch histories and candidate identity differ")
    lifecycle.prepare_candidate(out, build, deployment, RELEASE, reuse.LOCAL_SHELL_REVIEWED,
                                KEY, [PROOF, PROGRAM, RELEASE, ACTOR, Path(__file__), Path(approvals.__file__)])
    store = out / "state/application-objects"
    seed_histories(store, [(direct_source, direct), (program_source, program)])
    for name in ("primary", "light"):
        path = out / name / "plan.json"
        plan = package.read(path)
        command = plan["command"]
        package.require(Path(command[command.index("--object-store-dir") + 1]) == store,
                        "profiles must share the qualified patch histories")
        plan[KEY]["interpreter"] = str(Path(interpreter).absolute())
        package.save(path, plan)


def check(row, previous=None):
    direct, program = (package.read(p)["client"] for p in (PROOF, PROGRAM))
    required = {prefix + "_" + field: source["captures"][name]
                for prefix, source, names in (("direct", direct, ("call.json", "completed.json", "inputs.json")),
                                               ("program", program, ("pending.json", "stored.json", "inputs.json")))
                for field, name in zip(("call", "completed", "inputs"), names, strict=True)}
    originals = [p[k] for p in (direct, program) for k in ("response", "follow")]
    hashes = row["wire_sha256"]
    labels = required.keys() | {"create", "independent", "current_response", "current_inputs"}
    if previous is not None:
        labels |= {"restored_response", "restored_inputs"}
    package.require(labels == hashes.keys() and all(hashes[k] == v for k, v in required.items())
                    and all(re.fullmatch(r"[0-9a-f]{64}", v) for v in hashes.values())
                    and [row[k] for k in ("original", "follow", "program", "program_follow")] == originals
                    and len(set(originals)) == 4 and row["sdk"] == direct["sdk"] == program["sdk"]
                    and row["result"] == program["answer"] == "failed ENOENT"
                    and row["peer"] == 404 and row["independent_chat"] == "720041",
                    "historical patch identity, client or resumed result changed")
    for label, value in (("current", row["saved"]), ("restored", row["restored"])):
        if label == "restored":
            if previous is None:
                package.require(value is None, "unexpected previous continuation")
                continue
            package.require(package.same_json(value, previous["saved"]) and value["id"] != row["saved"]["id"],
                            "the current phase did not preserve the previous continuation")
        package.require(re.fullmatch(r"resp_[A-Za-z0-9_-]+", value["id"]) and value["id"] not in originals
                        and all(value[k + "_sha256"] == hashes[label + "_" + k] for k in ("response", "inputs")),
                        "patch continuation identity changed")


def exercise(params, interpreter, errors, captures, previous=None):
    request = dict(params, capture=str(captures), original=package.read(PROOF)["client"],
                   program=package.read(PROGRAM)["client"])
    if previous is not None:
        request["previous"] = previous["saved"]
    probe = subprocess.run([interpreter, "-IB", ACTOR], input=json.dumps(request), text=True,
                           stdout=subprocess.PIPE, stderr=errors, timeout=160, check=True)
    row = json.loads(probe.stdout)
    check(row, previous)
    return row
