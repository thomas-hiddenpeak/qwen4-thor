"""Qualify stored encrypted Conversations across primary and light capacities."""
import json
from pathlib import Path
import re
import subprocess

from tools.serve_recovery import profile_live_reuse as reuse
from tools.serve_responses import environment_lifecycle_profiles as lifecycle
from tools.serve_service import package

KEY = "conversation_compaction_handoff"
RELEASE = lifecycle.ROOT / "docs/evidence/serve-stored-content-parts-20261126/delivery.json"
PROOF = lifecycle.ROOT / "docs/evidence/serve-conversation-compaction-20261126/business.json"
ACTOR = Path(__file__).with_name("conversation_compaction_handoff_sdk.py")
DIRECT = Path(__file__).with_name("conversation_compaction_sdk.py")


def prepare(out, build, deployment, interpreter):
    record = package.read(PROOF)
    client = record["client"]
    version = subprocess.check_output([interpreter, "-IB", "-c",
        "import openai; print(openai.__version__)"], text=True, timeout=15).strip()
    package.require(record["stage"] == "native_tls_business_verified"
                    and record["run"]["status"] == "PASS" and version == client["sdk"]
                    and client["exact_answer"] is True and client["peer"] == 404
                    and record["sdk_driver"] == package.sha(DIRECT)
                    and record["engine"] == package.sha(build / "q4t")
                    and record["ingress"] == package.sha(build / "q4t-ingress"),
                    "conversation capacity requires the verified SDK and binaries")
    lifecycle.prepare_candidate(out, build, deployment, RELEASE, reuse.PARTS_REVIEWED,
                                KEY, [PROOF, RELEASE, ACTOR, DIRECT, Path(__file__)])
    for name in ("primary", "light"):
        path = out / name / "plan.json"
        plan = package.read(path)
        command = plan["command"]
        directory = Path(command[command.index("--object-store-dir") + 1])
        package.require(directory == out / "state/application-objects",
                        "both capacities must use the same object store")
        plan[KEY]["interpreter"] = str(Path(interpreter).absolute())
        package.save(path, plan)


def check(row, previous=None):
    direct = package.read(PROOF)["client"]
    saved = row["saved"]
    ids = saved["items"]
    package.require(row["sdk"] == direct["sdk"] and row["answer"] == direct["answer"]
                    and row["independent_chat"] == direct["independent_chat"]
                    and package.same_json(saved["peer"], 404)
                    and re.fullmatch(r"conv_[A-Za-z0-9_-]+", saved["conversation"])
                    and type(ids) is list and len(ids) == len(set(ids)) == 3
                    and all(type(i) is str and i for i in ids)
                    and saved["seal"] == ids[-1] and re.fullmatch(r"cmp_[A-Za-z0-9_-]+", ids[-1])
                    and all(re.fullmatch(r"[0-9a-f]{64}", saved[k]) for k in ("page_sha256","seal_sha256"))
                    and row["wire_sha256"]["current_page"] == saved["page_sha256"]
                    and row["wire_sha256"]["current_seal"] == saved["seal_sha256"],
                    "missing sealed conversation, isolated owner or correct stateless continuation")
    if previous is None:
        package.require(row["restored"] is None and row["recovered_answer"] is None,
                        "unexpected prior conversation")
    else:
        check(previous)
        prior = previous["saved"]
        package.require(package.same_json(row["restored"], prior)
                        and row["recovered_answer"] == direct["answer"]
                        and prior["conversation"] != saved["conversation"]
                        and set(ids).isdisjoint(prior["items"])
                        and row["wire_sha256"]["restored_page"] == prior["page_sha256"]
                        and row["wire_sha256"]["restored_seal"] == prior["seal_sha256"],
                        "restored page, ciphertext, identity or model continuation changed")


def exercise(params, interpreter, errors, captures, previous=None):
    request = dict(params, capture=str(captures))
    if previous is not None:
        request["previous"] = previous["saved"]
    probe = subprocess.run([interpreter, "-IB", ACTOR], input=json.dumps(request), text=True,
                           stdout=subprocess.PIPE, stderr=errors, timeout=170, check=True)
    row = json.loads(probe.stdout)
    check(row, previous)
    return row
