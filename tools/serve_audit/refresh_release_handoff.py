"""Refresh the release and operator handoffs using a verified local publication and its receipt."""
from pathlib import Path
import hashlib
import json
import os
import re


def verify_registration(build, pub):
    state = build / "service-native-state"

    def policy(name):
        source = state / name / "policy.json"
        return json.loads(source.read_bytes())["state"]["policy"]

    published = policy("service-publication")
    versions = policy("version-selection")
    payloads = versions["registry"]["payloads"]
    if (published["phase"] != "stable" or
            published["revision"] != pub["publication_revision"] or
            published["current"]["manifest_sha256"] != pub["primary"] or
            any(payloads[role]["manifest_sha256"] != pub[role]
                for role in ("primary", "fallback"))):
        raise ValueError("Receipt differs from the installed registration")


def checked_plan(info):
    path = Path(info["plan"])
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != info["plan_sha256"]:
        raise ValueError("Operator plan digest differs from the receipt")
    plan = json.loads(data)
    if Path(plan["output"]) != path.parent:
        raise ValueError("Operator plan names a different output directory")
    return path, plan


def refresh_operator_tasks(matrix, record, ref, installed):
    rows = {task["id"]: task for task in matrix["tasks"]}
    pub = record["publication"]
    revision = pub["publication_revision"]
    day, cold = record["day"], record["cold"]
    day_path, soak = checked_plan(day)
    cold_path, preflight = checked_plan(cold)
    if (soak["package"] != preflight["package"] or
            soak["package"]["manifest_sha256"] != pub["primary"] or
            Path(soak["package"]["package"]).resolve() != installed.resolve() or
            preflight["source_commit"] != soak["source_commit"] or
            soak["source_commit"] != day["source"]):
        raise ValueError("Operator plans do not refer to the issued package and source")
    operator = Path(day["operator"])
    if (operator.parent != day_path.parent or not os.access(operator, os.X_OK) or
            hashlib.sha256(operator.read_bytes()).hexdigest() != day["operator_sha256"]):
        raise ValueError("Operator entry differs from its checked identity")
    handoff_bytes = (day_path.parent / "handoff.json").read_bytes()
    handoff = json.loads(handoff_bytes)
    if (handoff["plan"] != str(day_path) or handoff["user_runs_day"] is not True or
            handoff["automatically_started"] is not False or
            soak["profile"] != "day" or soak["day_run_owner"] != "user" or
            len(soak["audio_cycles"]) != day["audio_pairs"] or day["hours_executed"] != 0 or
            cold["model_actions"] != 0 or cold["host_units_modified"] is not False or
            preflight["host_root_filter_installed"] is not False or
            preflight["other_services_modified"] is not False):
        raise ValueError("Receipt does not describe operator-owned, prepared plans")
    manifests = [Path(entry["path"]) for entry in soak["protected"]
                 if Path(entry["path"]).parts[-3:] == ("build", "service", "manifest.json")
                 and entry["sha256"] == pub["primary"]]
    if len(manifests) != 1 or manifests[0].resolve() != (installed / "manifest.json").resolve():
        raise ValueError("Checked operator workspace does not use the installed package")
    native = {key: day[key] for key in ("audio_pairs", "first_hour_business", "business_requests",
                                        "plan", "plan_sha256", "operator", "operator_sha256",
                                        "checked_at", "minimum_evidence_free_bytes", "hours_executed")}
    native.update(revision=revision, workspace=str(manifests[0].parents[2]),
                  package=soak["package"], source_commit=soak["source_commit"],
                  handoff_sha256=hashlib.sha256(handoff_bytes).hexdigest(),
                  source_files=len(soak["sources"]), checked=True)
    # A preparation receipt can refresh entry points, never erase recorded runs.
    def publish(task, field, current, observed):
        previous = rows[task].get(field, {})
        if previous.get(observed):
            if previous["revision"] == revision:
                if previous.get("plan_sha256") != current["plan_sha256"]:
                    raise ValueError("Completed run refers to another plan")
                return
            history = rows[task].setdefault("executed_operator_handoffs", [])
            if previous not in history:
                history.append(previous)
        rows[task][field] = current

    publish("Q03", "current_native_day_handoff", native, "hours_executed")
    publish("Q03", "current_day_handoff", dict(
        revision=revision, receipt=ref, plan_sha256=day["plan_sha256"],
        hours_run=day["hours_executed"], owner="user", prepared=True, checked=True), "hours_run")
    publish("R07", "current_cold_preflight", dict(
        revision=revision, receipt=ref, plan=str(cold_path), plan_sha256=cold["plan_sha256"],
        model_starts=0, executed=False), "executed")
    rows["R07"]["current_execution_prerequisite"] = (
        "Use the plan for the currently issued package. Check current device owners "
        "at execution time before inducing a genuine cold NVML failure.")


def refresh(root, deployment, receipt_path):
    status = root / "docs/STATUS.md"
    record = json.loads(receipt_path.read_bytes())
    pub = record["publication"]
    issued_stages = ("issued", "device_issued_handoffs_prepared",
                     "stopped_default_and_operator_handoff")
    if (record["stage"] not in issued_stages or pub["status"] != "PASS" or
            pub["desired"] != "STOPPED" or pub["model_starts"] != 0 or
            not pub["profile_budget_preserved"] or not pub["protected_files"] or
            pub["primary"] == pub["fallback"]):
        raise ValueError("Incomplete stopped issuance")
    for key in ("primary", "fallback", "engine"):
        if not re.fullmatch("[0-9a-f]{64}", pub[key]):
            raise ValueError("Invalid release identity")
    state = deployment / "build"
    for name, key in (("service/manifest.json", "primary"), ("q4t", "engine")):
        with (state / name).open("rb") as source:
            if hashlib.file_digest(source, "sha256").hexdigest() != pub[key]:
                raise ValueError("Installed identity changed")
    required = record["required_methods"]
    declared = record["declared_methods"]
    if (type(required) is not int or type(declared) is not int or
            not 0 <= declared <= required or required == 0):
        raise ValueError("Invalid method coverage")
    revision = pub["publication_revision"]
    ref = receipt_path.relative_to(root / "docs").as_posix()
    text = status.read_text()
    start = text.index("## 当前实际默认\n")
    end = text.index("## 全部任务进展与剩余缺口\n", start)
    summary = f"""## 当前实际默认

本机登记的发行版本为 **rev{revision}**。登记时服务意图为 `STOPPED`，模型启动 {pub['model_starts']} 次，{pub['protected_files']} 份客户与恢复状态通过复核。[rev{revision} 发行回执]({ref})

| 已登记产物 | SHA-256 |
|---|---|
| 主服务包清单 | `{pub['primary']}` |
| 回退服务包清单 | `{pub['fallback']}` |
| 默认引擎 | `{pub['engine']}` |

发行包为范围内 {required} 个方法中的 {declared} 个声明了预期成功状态。各方法的实际业务验收、剩余媒体后端、客户端与恢复组合见[逐项任务表](serve-master-goals-v2.json)。真实 24 小时运行由用户安排。

"""
    text = text[:start] + summary + text[end:]
    row = "| Q02/Q03 | 已登记的发行和回退包见上表，逐项验收与最终矩阵继续。长稳计划已交接，实跑由用户安排。 |"
    text, count = re.subn(r"^\| Q02/Q03 \|[^\n]+\|$", lambda _: row, text, flags=re.M)
    if count != 1:
        raise ValueError("Expected one delivery handoff row")
    verify_registration(state, pub)
    matrix_path = root / "docs/serve-master-goals-v2.json"
    if matrix_path.exists() and (record["stage"] == "stopped_default_and_operator_handoff" or
                                 "operator" in record.get("day", {})):
        matrix = json.loads(matrix_path.read_bytes())
        refresh_operator_tasks(matrix, record, ref, state / "service")
        matrix_path.write_text(json.dumps(matrix, ensure_ascii=False, indent=2) + "\n")
    status.write_text(text)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", required=True, type=Path)
    parser.add_argument("--deployment", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args()
    refresh(args.checkout.resolve(), args.deployment.resolve(), args.receipt.resolve())
