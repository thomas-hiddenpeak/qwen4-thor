"""SRV-01b native CPU build/contract batch, with immutable attempt outputs.

Does not start HTTP listeners, load a model, or invoke a CUDA operation.
The separate lifecycle executable's actual HTTP group requires a GPU lease.
"""
import argparse
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_admission import stage as previous

h = previous.h
BASE = ROOT / ".q4t-work/serve-observability-20261103"
CPU = BASE / "cpu"


def sources():
    release = h.read(ROOT / "build/q4t.release.json")
    paths = set(h.read(release["tested_source_manifest"])) | {
        "include/q4t/server/http_observation.h"}
    return {p: h.sha(ROOT / p) for p in sorted(paths)}


def command(label, argv, folder=CPU, timeout=900):
    h.save(folder / (label+"-command.json"), dict(argv=argv, started=time.time()))
    with (folder / (label+".log")).open("xb") as log:
        result = subprocess.run(argv, cwd=ROOT, stdout=log, stderr=log, timeout=timeout)
    h.save(folder / (label+"-exit.json"), dict(exit=result.returncode, ended=time.time()))
    print(label, result.returncode, flush=True)
    return result.returncode == 0


def build():
    CPU.mkdir(parents=True, exist_ok=False)
    prior = h.read(previous.BASE / "cpu/commands.json")["commands"]
    commands = [(label, [arg.replace(str(previous.BASE), str(BASE)) for arg in argv])
                for label, argv in prior]
    h.save(CPU / "commands.json", dict(commands=commands, started=time.time(),
        source_base=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        dirty_candidate=True))
    h.save(CPU / "build-sources.json", sources())
    passed = True
    for label, argv in commands:
        if not command(label, argv):
            passed = False
            break
    logs = "\n".join(path.read_text() for path in CPU.glob("*.log"))
    zero_warnings = re.search(r"\bwarning:", logs, re.I) is None
    h.save(CPU / "build-result.json", dict(passed=passed, zero_warnings=zero_warnings,
        binary_sha256=h.sha(BASE / "engine/q4t") if (BASE / "engine/q4t").exists() else None))
    h.require(passed and zero_warnings, "build failed or produced warnings; preserve and repair")
    h.require(sources() == h.read(CPU / "build-sources.json"), "sources changed during build")


def test():
    h.require(h.read(CPU / "build-result.json")["passed"], "build first")
    h.require(sources() == h.read(CPU / "build-sources.json"), "runtime changed since build")
    commands = [("host-"+mode, [str(BASE / ("host-"+mode) / "q4t_host_tests")])
                for mode in ("off", "on")]
    commands += [("host-required", ["/usr/bin/python3", "-B",
        str(ROOT / "tools/release/check_host_contracts.py"), "--test-binary",
        str(BASE / "engine/q4t_host_tests"), "--output", str(CPU / "host-gate")]),
        ("native-observation", [str(BASE / "engine/q4t_http_lifecycle_tests"), "observation_runtime_"]),
        ("backend-gate", [str(BASE / "engine/q4t_host_tests"), "structured_decoder_"]),
        ("inventory", ["/usr/bin/python3", "-B", str(ROOT / "tools/serve_audit/check_inventory.py"), "--self-check"]),
        ("contract-generated", ["/usr/bin/python3", "-B", str(ROOT / "tools/serve_protocol/build_contract.py"), "--check"])]
    results = {label: command(label, argv, timeout=180) for label, argv in commands}
    h.save(CPU / "test-result.json", dict(passed=all(results.values()), commands=results))
    h.require(all(results.values()), "CPU failures retained; repair only affected checks")


def fault():
    folder = BASE / "fault-build"
    folder.mkdir(exist_ok=False)
    commands = h.read(CPU / "commands.json")["commands"]
    configure = next(argv for label, argv in commands if label == "configure-engine")
    configure = [arg.replace(str(BASE / "engine"), str(BASE / "fault-engine"))
                 .replace("Q4T_SERVE_FAULT_INJECTION=OFF", "Q4T_SERVE_FAULT_INJECTION=ON")
                 for arg in configure]
    h.save(folder / "sources.json", sources())
    passed = command("configure", configure, folder) and command("build", [
        "cmake", "--build", str(BASE / "fault-engine"), "--target", "q4t",
        "--parallel", "8"], folder)
    logs = "\n".join(p.read_text() for p in folder.glob("*.log"))
    zero_warnings = re.search(r"\bwarning:", logs, re.I) is None
    h.save(folder / "result.json", dict(passed=passed, zero_warnings=zero_warnings,
        binary_sha256=h.sha(BASE / "fault-engine/q4t") if passed else None))
    h.require(passed and zero_warnings and sources() == h.read(folder / "sources.json"),
              "fault build failed, warned or changed sources")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("build", "test", "fault"))
    args = parser.parse_args()
    globals()[args.action]()


if __name__ == "__main__":
    main()
