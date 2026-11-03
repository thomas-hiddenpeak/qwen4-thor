"""Build validation-only token evidence; never install or load a model."""
import argparse
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_strict_transport.prepare import command, require, save, sha

BASE = ROOT / ".q4t-work/serve-strict-transport-20261103"
PREVIOUS = ROOT / ".q4t-work/serve-observability-20261103"


def sources():
    names = json.loads((PREVIOUS / "package/candidate/runtime-source.json").read_text())
    return {name: sha(ROOT / name) for name in names}


def diagnostic():
    import subprocess
    output = BASE / "diagnostic"
    output.mkdir(exist_ok=False)
    source = ROOT / "tools/serve_strict_transport/diagnostic_probe.cpp"
    env = {k: v for k, v in os.environ.items() if not k.startswith("Q4T_")}
    rows = []
    for mode in ("off", "on"):
        binary = output / mode
        argv = ["/usr/bin/g++-14", "-std=c++23", "-Wall", "-Wextra", "-Werror",
                "-O2", "-I"+str(ROOT / "include"), str(source), "-o", str(binary)]
        if mode == "on":
            argv.append("-DQ4T_SERVE_FAULT_INJECTION=1")
        command(output, "compile-"+mode, argv, ROOT, 60)
        for selection in (None, "other", "selected"):
            trial_env = dict(env)
            if selection is not None:
                trial_env["Q4T_SERVE_GENERATED_IDS"] = selection
            result = subprocess.run([str(binary)], env=trial_env, capture_output=True,
                                    text=True, timeout=10)
            label = mode + "-" + str(selection)
            (output / (label+"-stderr.log")).write_text(result.stderr)
            require(result.returncode == 0 and not result.stdout, "diagnostic exit")
            expected = mode == "on" and selection == "selected"
            if expected:
                lines = result.stderr.splitlines()
                require(len(lines) == 2, "request-specific dump count")
                match = re.fullmatch(r"\[serve-test-generated\] request=selected "
                    r"total=5000 captured=4096 truncated=1 tokens=(\[.*\])", lines[0])
                require(match and json.loads(match[1]) == [7]*4096,
                        "bounded diagnostic lost token order or cap")
                require(lines[1].endswith("total=0 captured=0 truncated=0 tokens=[]"),
                        "empty generation dump")
            else:
                require(not result.stderr, "unselected or production token disclosure")
            rows.append(dict(mode=mode, selection=selection, passed=True))
    save(output / "result.json", dict(passed=True, cases=rows,
        source_sha256=sha(source), header_sha256=sha(ROOT / "include/q4t/server/serve_fault.h")))
    print("diagnostic: 6 cases passed", flush=True)


def fault():
    output = BASE / "fault-build"
    output.mkdir(exist_ok=False)
    prior = json.loads((PREVIOUS / "cpu/commands.json").read_text())["commands"]
    argv = next(argv for label, argv in prior if label == "configure-engine")
    argv = [arg.replace(str(PREVIOUS / "engine"), str(BASE / "fault-engine"))
            .replace("Q4T_SERVE_FAULT_INJECTION=OFF", "Q4T_SERVE_FAULT_INJECTION=ON")
            for arg in argv]
    initial = sources()
    save(output / "sources.json", initial)
    command(output, "configure", argv, ROOT, 180)
    command(output, "build", ["cmake", "--build", str(BASE / "fault-engine"),
            "--target", "q4t", "--parallel", "8"], ROOT, 900)
    logs = "".join(path.read_text() for path in output.glob("*.log"))
    require(re.search(r"\bwarning:", logs, re.I) is None and
            "cmake warning" not in logs.lower(), "build warning")
    require(sources() == initial, "sources changed during build")
    save(output / "result.json", dict(passed=True, zero_warnings=True,
        binary_sha256=sha(BASE / "fault-engine/q4t"), default_replaced=False))
    print("validation build ready; default unchanged", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("diagnostic", "fault"))
    args = parser.parse_args()
    globals()[args.action]()


if __name__ == "__main__":
    main()
