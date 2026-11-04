from pathlib import Path
import json
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_strict_transport import stage

h, base, output = stage.h, stage.BASE, stage.OUTPUT
h.verify_seal(stage.PACKAGE)
h.verify_seal(output)
samples, reports, cases = [], [], []
for folder in sorted((output / "business").iterdir()):
    if not folder.is_dir():
        continue
    log = (folder / "server.log").read_text()
    for m in re.finditer(r"grammar worker reaped=(\d+) lifetime_max_rss_bytes=(\d+) "
                        r"reported_post_exec_rss_bytes=(\d+) wait_status=(\d+)", log):
        reports.append(dict(case=folder.name, reaped=int(m[1]), lifetime_max_rss_bytes=int(m[2]),
            reported_post_exec_rss_bytes=int(m[3]), wait_status=int(m[4])))
    inputs = [(str(folder / "generation-memory-samples.json"), row)
              for row in h.read(folder / "generation-memory-samples.json")]
    inputs += [(str(path), h.read(path)["memory"]) for path in folder.glob("*-resources.json")]
    for path, row in inputs:
        procs, parents, workers = row["processes"], [], []
        for pid, status in procs.items():
            ppid = re.search(r"^PPid:\s+(\d+)", status, re.M)[1]
            rss = re.search(r"^VmRSS:\s+(\d+)", status, re.M)
            value = dict(pid=int(pid), rss_bytes=int(rss[1])*1024 if rss else None,
                state=re.search(r"^State:\s+([^\n]+)", status, re.M)[1])
            (workers if ppid in procs else parents).append(value)
        samples.append(dict(source=path, monotonic=row["monotonic"], parents=parents, workers=workers,
            memavailable_bytes=int(re.search(r"^MemAvailable:\s+(\d+)", row["meminfo"], re.M)[1])*1024,
            errors=row["sampling_errors"]))
    case = h.read(folder / "case-result.json")
    generated = h.read(folder / "generated.json")
    h.require(generated["ids"] == h.read(base / "cpu/canonical-ids.json") + [248046],
              "actual generated IDs do not match canonical sequence plus EOS")
    case.pop("final_metrics")
    case["generated"] = {k:v for k,v in generated.items() if k != "ids"}
    case["canonical_ids_plus_eos"] = True
    case["first_write"] = h.read(folder / "first-write.json")["log"]
    cases.append(case)
memory = dict(sample_count=len(samples), samples=samples, worker_reports=reports,
    parent_rss_max_bytes=max((p["rss_bytes"] for s in samples for p in s["parents"]
                             if p["rss_bytes"] is not None), default=None),
    worker_rss_max_bytes=max((p["rss_bytes"] for s in samples for p in s["workers"]
                             if p["rss_bytes"] is not None), default=None),
    worker_rss_missing_samples=sum(p["rss_bytes"] is None for s in samples for p in s["workers"]),
    global_memavailable_min_bytes=min(s["memavailable_bytes"] for s in samples),
    worker_reported_post_exec_max_bytes=max(r["reported_post_exec_rss_bytes"] for r in reports),
    worker_lifetime_max_bytes=max(r["lifetime_max_rss_bytes"] for r in reports),
    note="Sparse observations, not full peaks or additive physical RAM. Missing VmRSS is null, not zero. wait4 lifetime includes pre-exec inheritance.")
h.save(base / "memory-summary.json", memory)
plan = h.read(stage.PACKAGE / "plan.json")
business = h.read(output / "business/result.json")
business.pop("cases")
summary = dict(source_commit=plan["source_commit"], binary=plan["binary"], default=plan["default"],
    sidecar=plan["sidecar"], protocol_sha256=re.search(r'kProtocol\[\] = "([^"]+)"',
        (base / "fault-engine/generated/q4t/server/chat_protocol_identity.h").read_text())[1],
    production_protocol_sha256=h.read(stage.previous.PACKAGE / "plan.json")["protocol_sha256"],
    business=business, cases=cases, guard=h.read(output / "lease/exit.json"),
    finalize_seconds=h.read(output / "finalized.json")["seconds"],
    schema_check=h.identity(output / "wire-schema-check.json"),
    postflight=h.identity(base / "postflight.json"), memory=h.identity(base / "memory-summary.json"))
h.save(base / "acceptance-summary.json", summary)
print(json.dumps(dict(business=business, finalize_seconds=summary["finalize_seconds"],
    postflight=h.read(base / "postflight.json"),
    memory={k:v for k,v in memory.items() if k not in ("samples", "worker_reports")}), indent=2))
