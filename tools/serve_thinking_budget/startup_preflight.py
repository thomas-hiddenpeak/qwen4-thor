"""Compile/run real option and SSD directory checks without starting serve."""
from pathlib import Path
import subprocess

import stage
v = stage.module("budget_startup_commands", Path(__file__).with_name("verify.py"))


def main():
    output = stage.BASE / "cpu/startup-preflight"
    output.mkdir(mode=0o700)
    binary = output / "startup_preflight"
    sources = ["tools/serve_thinking_budget/startup_preflight.cpp",
               "src/server/server_options.cpp", "src/server/session_cache.cpp",
               "src/trace/sha256.cpp"]
    command = ["g++-14", "-std=c++23", "-O2", "-Wall", "-Wextra", "-Werror",
               "-pthread", "-Iinclude", *sources, "-o", str(binary)]
    stage.h.save(output / "build-command.json", dict(argv=command))
    build = subprocess.run(command, cwd=stage.ROOT, capture_output=True, text=True, timeout=60)
    (output / "build.log").write_text(build.stdout + build.stderr)
    assert build.returncode == 0 and not build.stderr, build.stderr
    model = stage.h.read(stage.BASE / "package/plan.json")["model_dir"]
    directory = output / "private-cache"
    args = v.command("q4t", model, 18099, True, directory)
    missing_capacity = list(args)
    index = missing_capacity.index("--seq-capacities")
    del missing_capacity[index:index + 2]
    rows = []
    def case(label, argv, expected):
        result = subprocess.run([str(binary), *argv[2:]], cwd=stage.ROOT,
                                capture_output=True, text=True, timeout=20)
        record = dict(label=label, argv=argv, returncode=result.returncode,
                      stdout=result.stdout, stderr=result.stderr, expected=expected)
        stage.h.save(output / (label + ".json"), record)
        assert result.returncode == expected, record
        rows.append(record)
    case("original-missing-capacity", missing_capacity, 2)
    case("original-missing-directory", args, 3)
    v.prepare_cache(directory)
    case("corrected-cache-command", args, 0)
    case("plain-command", v.command("q4t", model, 18099, False), 0)
    case("installed-command", v.command("q4t", model, 18099, True), 0)
    assert not list(directory.iterdir()), "preflight left named cache data"
    stage.h.save(output / "result.json", dict(passed=True, cases=len(rows),
        binary=stage.h.identity(binary), sources={p: stage.h.sha(stage.ROOT / p) for p in sources},
        scope="Production option parser and SessionCache.Open direct IO; no model, GPU or HTTP evidence"))
    print("5 production startup preflight cases passed; no model/GPU/HTTP")


if __name__ == "__main__":
    main()
