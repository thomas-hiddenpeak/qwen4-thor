"""Stage an accepted row-max release, verify health, then replace known files.

No automatic service restoration. Reuses the checked-copy/replacement and
rollback contract of delivery.py; keeps the original default pair intact.
"""
import argparse
import contextlib
import copy
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import time
import traceback

from tools.vision_service import stage as s
from tools.vision_service import delivery as previous
from tools.vision_service.timing_stage import Run as TimingRun

h = previous.h
declarations = previous.declarations
ROOT = s.ROOT
BASE = ROOT / ".q4t-work/vision-rowmax-mainline-20261107"
PACKAGE = BASE / "release"
OUTPUT = BASE / "installation"
HEALTH = BASE / "health-01"
MARKER = ROOT / "build/vision-rowmax-recovery.json"
NAMES = ("q4t", "q4t.release.json")
OLD = dict(zip(NAMES, (
    "ea1b474d7d69808696e697346a60ee09bb9ecaca9bfb99925b78dcdbda7a4550",
    "3eb463850cf07338406b811197a68a5dcb9474ac9c2cf039470aa4f654924724")))
NEW = "60b1c4c121d066789ebc44746316161320448d98095f32a7ec24e3fa00343ff3"
LIMITS = dict(starts=1, generations=4, controls=16, native=0, seconds=300)
BORROW_GUARD = False


def copy_file(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    return h.copy_checked(h.identity(source), target)


def hashes(directory):
    return {name: h.sha(directory / name) for name in NAMES}


def prepare():
    decision = h.read(BASE / "decision.json")
    h.require(decision["status"] == "PASS_VISION_ROWMAX_ENGINEERING_ACCEPTANCE" and
              decision["performance_accepted"] is True and decision["binary_sha256"] == NEW,
              "explicit engineering decision required")
    for key, file in (("pilot_sha256", "audit.json"),
                      ("acceptance_audit_sha256", "acceptance-audit.json"),
                      ("acceptance_originals_sha256", "originals.json")):
        h.require(h.sha(BASE / file) == decision[key], "acceptance evidence drift")
    h.require(hashes(ROOT / "build") == OLD and not list((ROOT / "build").glob("*recovery*.json")),
              "default drift or pending recovery")
    old = h.verify_pair(ROOT / "build/q4t", ROOT / "build/q4t.release.json", OLD["q4t"])
    h.require(h.sha(BASE / "engine/q4t") == NEW, "candidate changed")
    PACKAGE.mkdir(mode=0o700)
    for name in NAMES:
        copy_file(ROOT / "build" / name, PACKAGE / "previous" / name)
    dependencies = {}
    for key in ("build_cache", "acceptance_decision", "tested_source_manifest"):
        source = Path(old[key])
        dependencies[key] = h.identity(source)
        copy_file(source, PACKAGE / "previous/dependencies" / key)
    for name in declarations.DECLARATION_FILES:
        source = Path(old["release_declarations"]["directory"]) / name
        dependencies["declarations/" + name] = h.identity(source)
        copy_file(source, PACKAGE / "previous/declarations" / name)
    for key in ("operations_rules", "rollback_operations_rules"):
        h.check_identity(old[key])
        dependencies[key] = old[key]
        copy_file(Path(old[key]["path"]), PACKAGE / "previous/dependencies" / key)
    h.save(PACKAGE / "previous/dependency-inventory.json", dict(files=dependencies,
        retained_subset_artifact=old["subset_artifact"],
        note="Old package/worktree paths retained; rollback sidecar byte-exact."))
    candidate = PACKAGE / "candidate"
    for source, target in (("q4t", "q4t"), ("CMakeCache.txt", "CMakeCache.txt"),
                           ("generated/q4t/server/chat_protocol_sources.json", "chat_protocol_sources.json")):
        copy_file(BASE / "engine" / source, candidate / target)
    plan = h.read(BASE / "accept-02/plan.json")
    for path in plan["files"]:
        path = Path(path)
        if path.is_relative_to(BASE / "engine/CMakeFiles") or path.is_relative_to(
                BASE / "engine/_deps/q4t_xgrammar-build/CMakeFiles"):
            copy_file(path, candidate / "build-records" / path.relative_to(BASE / "engine"))
    runtime = {}
    for path, digest in plan["files"].items():
        path = Path(path)
        if path.is_relative_to(ROOT):
            name = str(path.relative_to(ROOT))
            if name == "CMakeLists.txt" or name.startswith(("src/", "include/", "cmake/")):
                h.require(h.sha(path) == digest, "tested runtime changed: " + name)
                runtime[name] = digest
                copy_file(path, candidate / "source" / name)
    h.save(candidate / "runtime-source.json", runtime)
    for name in ("decision.json", "audit.json", "acceptance-audit.json", "originals.json"):
        copy_file(BASE / name, PACKAGE / "evidence" / name)
    HEALTH.mkdir()
    files = {str(p): h.sha(p) for p in PACKAGE.rglob("*") if p.is_file()}
    fixture = ROOT / ".q4t-work/vision-attention-rowmax-20261107/pilot-01/requests.json"
    pilot_seal = h.read(fixture.parent.parent / "originals.json")["files"]
    h.require(h.sha(fixture) == pilot_seal[str(fixture)], "retained image fixture drift")
    rows = [r for r in h.read(fixture) if r["group"] == "parent-0" and
            r["case"] == "earth-python"]
    row = copy.deepcopy(rows[0])
    h.save(HEALTH / "image.json", row)
    paths = [ROOT / "docs/STATUS.md", Path(__file__).resolve(), fixture,
             HEALTH / "image.json"]
    paths += [ROOT / name for name in subprocess.check_output(
        ["git", "ls-files"], cwd=ROOT, text=True).splitlines()
        if name.startswith(("tools/", "docs/serve-"))]
    files.update({str(p): h.sha(p) for p in paths})
    h.save(HEALTH / "plan.json", dict(profile="vision-rowmax-deployment-health-v1",
        limits=LIMITS, port=s.PORT, binary=str(candidate / "q4t"), model=str(s.MODEL),
        files=files, default_pair=OLD, candidate_sha256=NEW,
        source_commit=subprocess.check_output(["git", "rev-parse", "HEAD"],
            cwd=ROOT, text=True).strip(),
        note="One same-ELF staging HTTP health, then known-pair install and CLI check; no service retained."))
    return "PREPARED_NOT_INSTALLED"


def finalize_metadata():
    candidate = PACKAGE / "candidate"
    metadata = copy.deepcopy(h.read(PACKAGE / "previous/q4t.release.json"))
    decl = candidate / "declarations"
    for source, target in (("health-caps-wire.bin", "capabilities.json"),
                           ("health-config-wire.bin", "configuration.json")):
        copy_file(HEALTH / "execution" / source, decl / target)
    for name in declarations.DECLARATION_SCHEMAS:
        source = ROOT / ("docs/serve-" + name + "-v1.schema.json")
        if not source.is_file():
            source = Path(metadata["chat_protocol"][name + "_schema"])
        copy_file(source, decl / "schemas" / (name + ".json"))
    caps, config = (h.read(decl / name) for name in declarations.DECLARATION_FILES[:2])
    sources = h.read(candidate / "chat_protocol_sources.json")
    protocol = hashlib.sha256("".join(f"{n}:{sources[n]}\n" for n in sorted(sources)).encode()).hexdigest()
    h.require(caps["protocol_source_sha256"] == config["protocol_source_sha256"] == protocol,
              "observed protocol identity")
    for name, digest in sources.items():
        h.require(h.sha(ROOT / name) == digest, "protocol source drift")
    metadata.update(version="vision-rowmax-mainline-20261107", binary_sha256=NEW,
        source_commit=h.read(BASE / "accept-02/plan.json")["source_commit"],
        tool_source_commit=h.read(HEALTH / "plan.json")["source_commit"],
        source_note="Final mainline/row-max ELF; fresh plain/MTP matrix and visual/tool health; old numerical and pilot evidence explicitly inherited.",
        build_cache=str(candidate / "CMakeCache.txt"),
        build_cache_sha256=h.sha(candidate / "CMakeCache.txt"),
        tested_source_manifest=str(candidate / "runtime-source.json"),
        tested_source_manifest_sha256=h.sha(candidate / "runtime-source.json"),
        acceptance="ACCEPT_VISION_ROWMAX_WITH_TIMING_UNCERTAINTY",
        acceptance_decision=str(PACKAGE / "evidence/decision.json"),
        acceptance_decision_sha256=h.sha(PACKAGE / "evidence/decision.json"),
        inherited_release=h.identity(PACKAGE / "previous/q4t.release.json"),
        inherited_acceptance=dict(decision=metadata["acceptance_decision"],
                                  sha256=metadata["acceptance_decision_sha256"]),
        previous_binary=str(PACKAGE / "previous/q4t"),
        previous_release=str(PACKAGE / "previous/q4t.release.json"),
        observed_configuration=config, delivery_tool=h.identity(Path(__file__)),
        vision_rowmax=h.read(PACKAGE / "evidence/decision.json"))
    metadata["scope"]["performance"] = "Bounded visual HTTP gains; complete plain/default-MTP five-tier comparison; no statistical noninferiority claim"
    metadata["scope"]["installation_health"] = "Same-ELF staging: image JSON/SSE and one real read-only tool loop; installed pair/CLI checked, no persistent service"
    for key in caps.keys() & metadata["chat_protocol"].keys():
        metadata["chat_protocol"][key] = caps[key]
    metadata["chat_protocol"].update(protocol_sha256=protocol, media_acceptance=False,
        declaration_evidence="Actual health-01 opt-in media/plain observations; default text/MTP verified by final five-tier matrix; prior media task boundaries retained")
    for name in declarations.DECLARATION_SCHEMAS:
        path = decl / "schemas" / (name + ".json")
        metadata["chat_protocol"][name + "_schema"] = str(path)
        metadata["chat_protocol"][name + "_schema_sha256"] = h.sha(path)
    metadata["release_declarations"] = dict(profile=declarations.DECLARATION_PROFILE,
        directory=str(decl), binary_sha256=NEW, protocol_sha256=protocol,
        files={name: h.sha(decl / name) for name in declarations.DECLARATION_FILES})
    h.save(candidate / "q4t.release.json", metadata)
    h.verify_pair(candidate / "q4t", candidate / "q4t.release.json", NEW)
    declarations.accepted_release(metadata, h.read(PACKAGE / "evidence/decision.json"))
    h.save(PACKAGE / "pair.json", dict(old=OLD, new=hashes(candidate), atomicity=h.ATOMICITY))
    h.seal(PACKAGE)


def verify():
    h.verify_seal(PACKAGE)
    pair = h.read(PACKAGE / "pair.json")
    h.require(pair["old"] == OLD and pair["new"]["q4t"] == NEW, "package pair changed")
    for name, digests in (("previous", pair["old"]), ("candidate", pair["new"])):
        h.require(hashes(PACKAGE / name) == digests, "sealed pair differs")
        h.verify_pair(PACKAGE / name / "q4t", PACKAGE / name / "q4t.release.json", digests["q4t"])
    state = hashes(ROOT / "build")
    h.require(all(state[n] in (pair["old"][n], pair["new"][n]) for n in NAMES),
              "unknown installed identity")
    return dict(installed=state, pair=pair)


@contextlib.contextmanager
def locked():
    if BORROW_GUARD:
        s.guard_tools.require_guard()
        gpu = s.nvml_process_query.query(timeout=5)
        h.require(gpu["status"] == "OK" and not gpu["pids"], "install needs empty GPU")
        yield
    else:
        with previous.locked():
            gpu = s.nvml_process_query.query(timeout=5)
            h.require(gpu["status"] == "OK" and not gpu["pids"], "change needs empty GPU")
            yield


def change(rollback=False):
    with locked():
        review = verify()
        for path in (ROOT / "build").glob("*recovery*.json"):
            h.require(path == MARKER and h.read(path).get("stage") == str(PACKAGE),
                      "foreign recovery marker")
        if not rollback:
            h.require(review["installed"] == OLD and not MARKER.exists(), "install state")
            OUTPUT.mkdir(mode=0o700)
        output = OUTPUT / ("rollback" if rollback else "install")
        output.mkdir(parents=True, exist_ok=False)
        if not MARKER.exists():
            h.save(MARKER, dict(stage=str(PACKAGE), action="rollback" if rollback else "install"))
            h.fsync_directory(MARKER.parent)
        target = review["pair"]["old" if rollback else "new"]
        source = PACKAGE / ("previous" if rollback else "candidate")
        try:
            for name in NAMES:
                if review["installed"][name] != target[name]:
                    h.replace_checked(source / name, ROOT / "build" / name, review["installed"][name])
            h.verify_pair(ROOT / "build/q4t", ROOT / "build/q4t.release.json", target["q4t"])
            cli = subprocess.run([str(ROOT / "build/q4t"), "version"], cwd=ROOT,
                capture_output=True, timeout=15, env={k: v for k, v in os.environ.items()
                    if not k.startswith("Q4T_") and k not in ("LD_PRELOAD", "LD_LIBRARY_PATH")})
            (output / "cli.stdout").write_bytes(cli.stdout)
            (output / "cli.stderr").write_bytes(cli.stderr)
            h.require(cli.returncode == 0 and b"Qwen4-Thor" in cli.stdout and
                      hashes(ROOT / "build") == target, "installed CLI/pair health")
            h.save(output / "result.json", dict(status="PASS_ROLLBACK" if rollback else "PASS_INSTALLED_PAIR",
                pair=target, health="Installed CLI/identity; exact same ELF HTTP in health-01",
                persistent_service=False, atomicity=h.ATOMICITY))
            h.seal(output)
        except BaseException:
            if not rollback:
                state = verify()["installed"]
                for name in NAMES:
                    if state[name] != OLD[name]:
                        h.replace_checked(PACKAGE / "previous" / name, ROOT / "build" / name, state[name])
                h.verify_pair(ROOT / "build/q4t", ROOT / "build/q4t.release.json", OLD["q4t"])
            raise
        # The outer GPU guard must also finish before deployment is final.
        if not BORROW_GUARD:
            MARKER.unlink()
            h.fsync_directory(MARKER.parent)
        return h.read(output / "result.json")


class Run(s.Run):
    limits = LIMITS
    success_status = "PASS_ROWMAX_DEPLOYMENT"
    retire_model = TimingRun.retire_model

    def launch(self):
        self.budget("starts")
        folder = self.output / "model-health"
        argv = [self.plan["binary"], "serve", "--model-dir", self.plan["model"],
            "--host", "127.0.0.1", "--port", str(s.PORT), "--max-seq", "1",
            "--allow-media", "--no-mtp",
            "--max-prefill", "8192", "--max-len", "16384",
            "--max-inflight-requests", "8", "--max-queued-requests", "4"]
        env = {k: v for k, v in os.environ.items() if not k.startswith("Q4T_")
               and k not in ("LD_PRELOAD", "LD_LIBRARY_PATH")}
        self.model = self.children.launch(folder, argv, env)
        deadline = time.monotonic() + 180
        while "serving on port" not in (folder / "stderr.log").read_text():
            s.require(self.children.running(self.model) and time.monotonic() < deadline, "health startup")
            time.sleep(.1)
        for label, route in (("caps", "/v1/capabilities"), ("config", "/v1/config")):
            value = json.loads(self.http("health-" + label, route))
            s.require(not value["runtime"]["mtp_enabled"] and value["runtime"]["media_enabled"],
                      "opt-in media/plain profile")
        for route in ("/healthz", "/readyz"):
            self.http("health" + route.replace("/", "-"), route)

    def business(self):
        fixture = h.read(self.folder / "image.json")
        for stream in (False, True):
            body = copy.deepcopy(fixture["request"])
            body.update(request_id="rowmax-health-" + str(stream).lower(),
                        stream=stream)
            if stream:
                body["stream_options"] = dict(include_usage=True)
            else:
                body.pop("stream_options", None)
            value = self.complete("health-image-" + str(stream).lower(), body)
            actual = json.loads(value.message["content"])
            s.Draft202012Validator(body["response_format"]["json_schema"]
                                  ["schema"]).validate(actual)
            s.require(actual == fixture["expected"] and value.finish_reason == "stop",
                      "image JSON/SSE health answer")
        tools = copy.deepcopy(s.TOOLS[:1])
        tools[0]["function"]["strict"] = True
        body = s.request("Read the first 16 lines of docs/STATUS.md using read_project_record. "
            "Then summarize the current stage in one sentence and cite the actual document.",
            stream=True, tools=tools,
            tool_choice=dict(type="function", function=dict(name="read_project_record")),
            parallel_tool_calls=False)
        value = self.complete("health-tool-call", body)
        calls = s.client.prepare_tool_calls(value, s.VALIDATORS)
        s.require(len(calls) == 1 and calls[0].arguments ==
                  dict(path="docs/STATUS.md", start_line=1, line_count=16), "tool identity/range")
        result = s.ProjectRecords(ROOT).execute(calls[0])
        s.require(result["ok"] and result["source"]["sha256"] ==
                  self.plan["files"][str(ROOT / "docs/STATUS.md")] and
                  not result["source"]["truncated"], "real read-only tool/source")
        s.save(self.output / "real-tool-result.json", result)
        history = s.client.append_tool_results(body["messages"], value,
            {calls[0].call_id: json.dumps(result, ensure_ascii=False)})
        follow = s.client.make_request(history, tools, stream=False, max_tokens=256, timeout_ms=120000)
        follow["tool_choice"] = "none"
        value = self.complete("health-tool-final", follow)
        s.require(value.finish_reason == "stop" and result["source"]["citation"] in
                  value.message["content"], "real tool final/citation")
        self.idle("health-idle")
        self.retire_model("health")
        s.guard_tools.verify_files(dict(self.plan, model_file_metadata={}))
        finalize_metadata()
        global BORROW_GUARD
        BORROW_GUARD = True
        s.save(self.output / "installation.json", change())

    def run(self):
        s.guard_tools.require_guard()
        s.guard_tools.verify_files(dict(self.plan, model_file_metadata={}))
        self.monitor.start()
        failure, cleanup = None, []
        verdict = "FAILED"

        def expired(*_):
            raise TimeoutError("frozen health deadline; cleanup only")

        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, LIMITS["seconds"] - 45)
        try:
            self.launch()
            self.business()
            verdict = self.success_status
        except BaseException:
            failure = traceback.format_exc()
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            try:
                if self.model and self.children.children and self.children.running(self.model):
                    self.children.signal(self.model, signal.SIGTERM)
                s.wait_owned(self.children, cleanup)
                if self.children.children:
                    self.children.reap(self.output)
                for _ in range(2):
                    boundary = self.heartbeat["last_sample"]
                    while self.heartbeat["last_sample"] <= boundary:
                        s.guard_tools.require_monitor(self.monitor, self.failures, self.heartbeat, 10)
                        time.sleep(.05)
                gpu = s.nvml_process_query.query(timeout=5)
                s.save(self.output / "gpu-final.json", gpu)
                s.require(gpu["status"] == "OK" and not gpu["pids"], "ENV_ABORT: final GPU")
            except BaseException:
                cleanup.append(traceback.format_exc())
            finally:
                self.stop_monitor.set()
                self.monitor.join(5)
                self.children.cleanup()
            if self.monitor.is_alive() or self.failures:
                cleanup.append("ENV_ABORT: final monitor")
            try:
                s.guard_tools.verify_files(dict(self.plan, model_file_metadata={}))
            except BaseException:
                cleanup.append(traceback.format_exc())
            if failure or cleanup:
                verdict = "FAILED"
            s.save(self.output / "result.json", dict(status=verdict, counts=self.counts,
                seconds=time.monotonic() - self.started, failure=failure,
                cleanup_errors=cleanup, monitor_failures=self.failures,
                persistent_service=False, outer_guard_finalization_pending=MARKER.exists()))
        s.require(verdict == self.success_status, failure or str(cleanup))
        return verdict


def finalize():
    guard = h.read(HEALTH / "lease/exit.json")
    result = h.read(HEALTH / "execution/result.json")
    counts = result["counts"]
    h.require(guard["status"] == "COMPLETED" and guard["child_returncode"] == 0 and
              result["status"] == Run.success_status and not result["failure"] and
              not result["cleanup_errors"] and not result["monitor_failures"] and
              counts["starts"] == 1 and counts["generations"] == 4 and counts["native"] == 0 and
              3 <= counts["controls"] <= LIMITS["controls"],
              "health/outer guard incomplete; retain marker and use rollback")
    identity = h.read(HEALTH / "execution/model-health/identity.json")
    h.require(not Path(f"/proc/{identity['pid']}").exists(), "health model still present")
    with locked():
        review = verify()
        h.require(review["installed"] == review["pair"]["new"], "pending deployment identity")
        receipt = BASE / "deployment-finalized.json"
        if MARKER.exists():
            h.require(h.read(MARKER)["stage"] == str(PACKAGE), "foreign recovery marker")
        else:
            h.require(receipt.is_file(), "missing recovery marker and final receipt")
        result = dict(status="PASS_FINALIZED_ROWMAX_DEPLOYMENT", pair=review["installed"],
            health_source=str(HEALTH), model_starts=1, http_requests=4,
            persistent_service=False, old_packages_migrated=False,
            rollback=str(PACKAGE / "previous"))
        if receipt.exists():
            h.require(h.read(receipt) == result, "final receipt differs")
        else:
            h.save(receipt, result)
        if MARKER.exists():
            MARKER.unlink()
            h.fsync_directory(MARKER.parent)
    return result


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "health", "verify", "rollback", "finalize"))
    action = parser.parse_args().action
    if action == "prepare":
        result = prepare()
    elif action == "verify":
        result = verify()
    elif action == "rollback":
        result = change(rollback=True)
    elif action == "finalize":
        result = finalize()
    else:
        result = Run(HEALTH).run()
    print(json.dumps(result, ensure_ascii=False))
