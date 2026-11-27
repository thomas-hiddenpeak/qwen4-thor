"""Refresh the STATUS handoff using a verified local publication and its receipt."""
from pathlib import Path
import hashlib
import json
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
    status.write_text(text)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", required=True, type=Path)
    parser.add_argument("--deployment", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args()
    refresh(args.checkout.resolve(), args.deployment.resolve(), args.receipt.resolve())
