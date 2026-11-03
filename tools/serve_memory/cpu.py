"""Memory admission CPU batch; model/HTTP validation is a separate lease phase."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_strict_transport.prepare import command, require, save, sha

BASE = ROOT / ".q4t-work/serve-memory-20261103"
CPU = BASE / "cpu"
PREVIOUS = ROOT / ".q4t-work/serve-observability-20261103"


def sources():
    release = json.loads((ROOT / "build/q4t.release.json").read_text())
    names = set(json.loads(Path(release["tested_source_manifest"]).read_text()))
    names.update(("include/q4t/runtime/system_memory.h",
                  "src/runtime/system_memory.cpp",
                  "include/q4t/server/memory_admission.h",
                  "src/server/memory_admission.cpp", "src/server/chat_memory.cpp"))
    return {name: sha(ROOT / name) for name in sorted(names)}


def build():
    CPU.mkdir(parents=True, exist_ok=False)
    prior = json.loads((PREVIOUS / "cpu/commands.json").read_text())["commands"]
    commands = [(label, [arg.replace(str(PREVIOUS), str(BASE)) for arg in argv])
                for label, argv in prior]
    initial = sources()
    save(CPU / "build-sources.json", initial)
    save(CPU / "commands.json", dict(commands=commands,
        base_commit=subprocess.check_output(["git", "rev-parse", "HEAD"],
            cwd=ROOT, text=True).strip(), dirty_candidate=True))
    for label, argv in commands:
        command(CPU, label, argv, ROOT, 900)
        print(label, "passed", flush=True)
    logs = "\n".join(p.read_text() for p in CPU.glob("*.log"))
    require(re.search(r"\bwarning:", logs, re.I) is None and
            "cmake warning" not in logs.lower(), "zero warning build")
    require(sources() == initial, "runtime changed during build")
    save(CPU / "build-result.json", dict(passed=True, zero_warnings=True,
        binary_sha256=sha(BASE / "engine/q4t")))


def test():
    require(sources() == json.loads((CPU / "build-sources.json").read_text()),
            "sources changed since build")
    require(json.loads((CPU / "build-result.json").read_text())["passed"],
            "build first")
    commands = [("host-" + mode,
                 [str(BASE / ("host-" + mode) / "q4t_host_tests")])
                for mode in ("off", "on")]
    commands += [
        ("host-required", ["/usr/bin/python3", "-B",
         str(ROOT / "tools/release/check_host_contracts.py"), "--test-binary",
         str(BASE / "engine/q4t_host_tests"), "--output", str(CPU / "host-gate")]),
        ("backend", [str(BASE / "engine/q4t_host_tests"), "structured_decoder_"]),
        ("inventory", ["/usr/bin/python3", "-B",
         str(ROOT / "tools/serve_audit/check_inventory.py"), "--self-check"]),
        ("generated", ["/usr/bin/python3", "-B",
         str(ROOT / "tools/serve_protocol/build_contract.py"), "--check"])]
    results = {}
    for label, argv in commands:
        try:
            command(CPU, label, argv, ROOT, 180)
            results[label] = True
        except RuntimeError:
            results[label] = False
        print(label, results[label], flush=True)
    save(CPU / "test-result.json", dict(passed=all(results.values()),
                                        commands=results))
    require(all(results.values()), "preserve failures; recheck affected only")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("build", "test"))
    args = parser.parse_args()
    globals()[args.action]()


if __name__ == "__main__":
    main()
