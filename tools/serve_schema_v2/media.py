"""Four A05 image/video/schema/tool requests; no decoder or concurrency expansion."""
import argparse
import copy
from dataclasses import asdict
import http.client
import json
from pathlib import Path
import socket
import subprocess
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_schema_v2 import acceptance as base
from tools.serve_service.package import read, save, sha, require
from tools.examples.chat_client import prepare_tool_calls, append_tool_results
from tools.examples.project_records import ProjectRecords, VALIDATORS

FIXTURES = ROOT / ".q4t-work/vision-client-20261107/phase-01"
LIMITS = dict(starts=1, chats=4, controls=2000, seconds=600)


def fixtures():
    old = read(FIXTURES / "plan.json")
    names = ["requests.json", "fixtures/cabin.png", "fixtures/earth.jpg",
             "fixtures/provenance.json"]
    files = {str(FIXTURES / name): sha(FIXTURES / name) for name in names}
    require(all(old["files"].get(name) == digest for name, digest in files.items()),
            "accepted fixture identity changed")
    files[str(FIXTURES / "plan.json")] = sha(FIXTURES / "plan.json")
    return files


def requests():
    path = FIXTURES / "requests.json"
    # This archived input includes embedded image/video bytes; it is larger
    # than service metadata. Keep a separate explicit fixture-data bound.
    require(path.stat().st_size <= 4 * 1024**2, "fixture input size limit")
    archive = {row["label"]: row for row in json.loads(path.read_bytes())}
    def parts(label):
        return [copy.deepcopy(part)
                for message in archive[label]["request"]["messages"]
                for part in (message["content"]
                             if isinstance(message["content"], list) else [])
                if part["type"] != "text"]
    def request(label, content, question, schema, stream):
        body = dict(model="qwen3.8-flash-next", enable_thinking=False,
                    temperature=0, max_completion_tokens=384, stream=stream,
                    messages=[dict(role="user", content=content +
                        [dict(type="text", text=question)])],
                    response_format=dict(type="json_schema", json_schema=dict(
                        name="scene", strict=True, schema=schema)))
        if stream:
            body["stream_options"] = dict(include_usage=True)
        return base.tagged("media-" + label, body)
    cabin = copy.deepcopy(archive["cabin-python"]["request"]
                          ["response_format"]["json_schema"]["schema"])
    cabin["properties"]["visible_windows"].update(minimum=0, maximum=12)
    cabin = {"$ref": "#/$defs/scene", "$defs": dict(scene=cabin)}
    direction = dict(type="string", pattern=
        "^(left_to_right|right_to_left|up|down|stationary)$")
    motion = {"$ref": "#/$defs/result", "$defs": dict(direction=direction,
        result=base.closed(dict(direction={"$ref": "#/$defs/direction"})))}
    observed = copy.deepcopy(archive["earth-python"]["request"]
                             ["response_format"]["json_schema"]["schema"])
    output = base.final_schema()
    output["properties"]["observed"] = {"$ref": "#/$defs/observed"}
    output["required"].append("observed")
    output = {"$ref": "#/$defs/result", "$defs": dict(result=output,
        observed=observed)}
    tool = request("tool", parts("earth-python"),
        "Look at the photo. Read the first 16 lines of docs/MODEL.md with "
        "read_project_record. Then return JSON with the actual first-line "
        "title, exact source citation, requested_line_count 16, note null, "
        "and observed: the pictured object, clouds_visible, street_sign_visible.",
        output, True)
    tool.update(tools=base.requests()["tool"]["tools"], tool_choice="required",
                parallel_tool_calls=False)
    return dict(cabin=request("cabin", parts("cabin-python"),
        "Describe only the visible setting, windows, device lights and people. "
        "Return one JSON object.", cabin, False),
        motion=request("motion", parts("motion-forward"),
        "Which direction does the red circle move over the video? "
        "Return JSON direction.", motion, True), tool=tool)


def prepare(args):
    files = fixtures()
    base.prepare(args, rows=requests(), limits=LIMITS,
        scope="Reuse accepted photographs and synthetic video frames. "
              "Root refs/bounds/patterns with media and a real documentation "
              "tool/JSON closure. No new hardware decoder, vision concurrency "
              "or full quality/performance matrix.")
    plan = read(args.output / "plan.json")
    plan.update(fixtures=files, fixture_provenance=read(
        FIXTURES / "fixtures/provenance.json"),
        expected=dict(cabin=dict(scene="aircraft_cabin", visible_windows=2,
            device_lights_on=True, people_visible=False),
            motion=dict(direction="left_to_right"),
            observed=dict(object="earth", clouds_visible=True,
                          street_sign_visible=False)))
    save(args.output / "plan.json", plan)


class Media(base.Stage):
    limits = LIMITS
    request_templates = staticmethod(requests)
    success_status = "PASS_SCHEMA_V2_MEDIA_BUSINESS"

    def __init__(self, plan):
        require(fixtures() == plan["fixtures"], "fixture drift")
        super().__init__(plan)

    def start(self):
        for port in (self.plan["port"], self.plan["operations_port"]):
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", port))
        args = [self.plan["binary"], "serve", "--model-dir", self.plan["model"],
            "--host", "127.0.0.1", "--port", str(self.plan["port"]),
            "--operations-port", str(self.plan["operations_port"]),
            "--allow-media", "--no-mtp", "--max-seq", "1", "--max-len", "16384",
            "--max-prefill", "8192", "--max-tokens", "384",
            "--max-inflight-requests", "8", "--max-queued-requests", "2"]
        save(self.out / "model-command.json", args)
        self.budget("starts")
        with (self.out / "model.log").open("xb", buffering=0) as log:
            self.model = subprocess.Popen(args, cwd=ROOT, stdout=log,
                stderr=subprocess.STDOUT, pass_fds=(self.lease,))
        self.model_identity = base.lifecycle.guard.processes()[self.model.pid]
        self.history[self.model.pid] = self.model_identity
        save(self.out / "model-identity.json", dict(pid=self.model.pid,
                                                   **self.model_identity))
        end = time.monotonic() + 180
        while time.monotonic() < end:
            self.same_model()
            try:
                status, _ = self.http("/readyz", timeout=1)
                if status == 200:
                    break
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(.1)
        else:
            raise TimeoutError("media readiness")
        status, raw = self.http("/v1/capabilities", label="initial-capabilities")
        require(status == 200, "media capabilities missing")
        caps = json.loads(raw)
        require(caps["runtime"] == dict(media_enabled=True, mtp_enabled=False)
                and caps["structured_output"]["schema_profile"] ==
                "q4t-json-schema-v2", "media/schema runtime differs")
        save(self.out / "capabilities.json", caps)
        self.client.protocol_sha256 = caps["protocol_source_sha256"]

    def sdk_request(self, label, body):
        value = super().sdk_request(label, body)
        preflight = read(self.out / (label + "-preflight.wire"))
        require(value.usage["prompt_tokens"] == preflight["prompt_tokens"],
                "expanded visual token accounting differs")
        return value

    def business(self):
        rows = self.plan["requests"]
        for label in ("cabin", "motion"):
            value = self.sdk_request(label, rows[label])
            require(value.finish_reason == "stop" and not value.message.get("tool_calls")
                    and json.loads(value.message["content"]) ==
                    self.plan["expected"][label], "visual truth differs: " + label)
        call = self.sdk_request("tool", rows["tool"])
        base.require_fragments((self.out / "tool.wire").read_bytes())
        calls = prepare_tool_calls(call, VALIDATORS)
        require(len(calls) == 1 and calls[0].name == "read_project_record" and
                calls[0].arguments == dict(path=base.DOCUMENT, start_line=1,
                                           line_count=16), "visual tool arguments")
        result = ProjectRecords(ROOT).execute(calls[0])
        require(result["ok"] and result["source"]["sha256"] ==
                self.plan["sources"][base.DOCUMENT], "document changed")
        save(self.out / "tool-execution.json", dict(plan=asdict(calls[0]), result=result))
        follow = base.tagged("media-final", rows["tool"])
        follow["tool_choice"] = "auto"
        follow["messages"] = append_tool_results(follow["messages"], call,
            {calls[0].call_id: json.dumps(result, ensure_ascii=False)})
        follow["messages"].append(dict(role="developer", content=
            "Return the actual document title, exact citation, requested_line_count "
            "16, note null, and observed visual fields as one JSON object. "
            "The actual tool result is already available."))
        value = self.sdk_request("final", follow)
        answer = base.check_final(value, result)
        require(answer["observed"] == self.plan["expected"]["observed"],
                "visual/tool/schema closure differs")
        metrics = self.drained("final")
        require(metrics["q4t_requests_total"] == 4 and
                metrics["q4t_requests_success_total"] == 4 and
                metrics["q4t_requests_error_total"] == 0, "media accounting")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--binary", type=Path, required=True)
    prep.add_argument("--output", type=Path, required=True)
    prep.add_argument("--port", type=int, default=18164)
    run = sub.add_parser("run")
    run.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args()
    if args.mode == "prepare":
        prepare(args)
    else:
        with base.lifecycle.child_subreaper():
            Media(read(args.plan)).run()


if __name__ == "__main__":
    main()
