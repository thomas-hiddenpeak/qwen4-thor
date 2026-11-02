"""Build the real CLI preflight and check all three planned service commands."""
from pathlib import Path
import subprocess

import stage
v = stage.module("preflight_launch_arguments", Path(__file__).with_name("verify.py"))


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
    stage.h.require(build.returncode == 0 and not build.stderr, "preflight compile")
    prior = stage.h.read(stage.previous.BASE / "package-fixture/plan.json")
    records = []
    for name, mtp, explicit in (("uniform", True, False), ("slots", True, True),
                                ("installed-plain", False, False)):
        args = v.command("q4t", prior["model_dir"], 18099, mtp, explicit)
        run = subprocess.run([str(binary), *args[2:]], cwd=stage.ROOT,
                             capture_output=True, text=True, timeout=20)
        record = dict(name=name, argv=args, stdout=run.stdout, stderr=run.stderr,
                      returncode=run.returncode)
        stage.h.save(output / (name + ".json"), record)
        stage.h.require(run.returncode == 0, "CLI preflight failed")
        records.append(record)
    stage.h.save(output / "result.json", dict(passed=True, cases=len(records),
        binary=stage.h.identity(binary), sources={p: stage.h.sha(stage.ROOT / p) for p in sources},
        scope="Production ParseServerOptions, no model, GPU or HTTP"))
    print("3 production launch argument sets passed")


if __name__ == "__main__":
    main()
