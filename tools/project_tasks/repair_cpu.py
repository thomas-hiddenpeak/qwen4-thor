"""Preserve the transport failure reproduction, then check only its correction."""
import io
import json
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from tools.project_tasks import stage, store


def run(fixed):
    os.umask(0o077)
    base = stage.cpu.BASE
    output = base / ("cpu-transport-fixed" if fixed else "cpu-transport-original")
    output.mkdir(mode=0o700)
    sources = {name: stage.pkg.sha(ROOT / name) for name in
               sorted(set((*stage.SOURCES, "tools/project_tasks/repair_cpu.py")))}
    for name in sources:
        target = output / "source" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / name).read_bytes())
    names = ["test_content_length_eof_after_connection_close_does_not_touch_retired_socket"]
    if fixed:
        names.append("test_deadline_reader_checks_each_partial_read_and_eof")
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(
        "tools.project_tasks.test_tasks.Tasks." + name) for name in names)
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    (output / "tests.log").write_text(stream.getvalue())
    value = dict(passed=result.wasSuccessful(), tests_run=result.testsRun,
        failures=len(result.failures), errors=len(result.errors), sources=sources,
        models_started=0, http_listeners_started=0, unix_socketpair_only=True)
    if fixed:
        old = stage.pkg.read(base / "cpu-wire/result.json")
        failed = stage.pkg.read(base / "cpu-transport-original/result.json")
        store.require(old["passed"] and old["effective_tests"] == 19 and
                      not failed["passed"] and failed["tests_run"] == 1, "original gates")
        allowed = {"tools/project_tasks/transport.py", "tools/project_tasks/test_tasks.py"}
        for name, checksum in old["sources"].items():
            store.require(stage.pkg.sha(base / "cpu-wire/source" / name) == checksum,
                          "original source changed")
            if name not in allowed:
                store.require(sources[name] == checksum, "reused dependency changed")
        value.update(effective_tests=20, reused_tests=18, driver_tests_reused=2,
            original_sha256=stage.pkg.sha(base / "cpu-transport-original/result.json"),
            previous_sha256=stage.pkg.sha(base / "cpu-wire/result.json"))
    store.write_new(output / "result.json", store.encode(value))
    print(stream.getvalue(), end="")
    print(json.dumps({k: v for k, v in value.items() if k != "sources"}))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    store.require(sys.argv[1:] in (["original"], ["fixed"]), "repair_cpu.py original|fixed")
    raise SystemExit(run(sys.argv[1:] == ["fixed"]))
