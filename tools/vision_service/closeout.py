"""Bounded same-input temporal diagnosis and real-record image business.

Old failures and bytes are never rewritten. A completed contrast is not a
quality acceptance. This driver does not change or install the runtime.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import secrets
import subprocess
import uuid

from PIL import Image, ImageDraw, ImageFont
from tools.vision_service import stage as s

BASE = s.ROOT / ".q4t-work/vision-closeout-20261107"
FOLDER = BASE / "phase-a"
RECORD = "docs/evidence/vision-service-v1-20261107/result.json"
TOTAL = dict(starts=4, generations=48, controls=120, native=2, seconds=2400)
LIMITS = dict(starts=1, generations=17, controls=40, native=0, seconds=900)
FONT = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
DEFAULT = "ea1b474d7d69808696e697346a60ee09bb9ecaca9bfb99925b78dcdbda7a4550"


def read(path):
    return json.loads(Path(path).read_text())


def fresh(body):
    body = copy.deepcopy(body)
    body.update(request_id="closeout-" + uuid.uuid4().hex,
                cancel_token=secrets.token_hex(32))
    return body


def input_contract(body):
    return {k: v for k, v in body.items()
            if k not in {"request_id", "cancel_token", "response_format"}}


def contrasts(originals):
    rows = []
    for label, body, expected in originals:
        for mode in ("strict", "json", "free"):
            changed = fresh(body)
            if mode == "json":
                changed["response_format"] = dict(type="json_object")
            elif mode == "free":
                changed.pop("response_format", None)
            s.require(input_contract(changed) == input_contract(body),
                      "paired input changed")
            rows.append(dict(label=label+"-"+mode, group=label, mode=mode,
                             request=changed, expected=expected))
    return rows


def budget_plans(plans):
    used = {k: sum(p["limits"][k] for p in plans) for k in TOTAL}
    s.require(all(type(p["limits"][k]) is int and p["limits"][k] >= 0
                  for p in plans for k in TOTAL), "invalid budget")
    s.require(all(used[k] <= TOTAL[k] for k in TOTAL), "total phase budget")
    return used


def record_truth(record):
    totals = record["totals"]
    phases = {r["phase"]: r["counts"]["generations"] for r in record["phases"]}
    selected = {k: phases[k] for k in ("phase-04", "phase-05b", "phase-07")}
    return dict(totals=totals, chart=selected,
                tallest=max(selected, key=selected.get))


def render_records(folder):
    source = s.ROOT / RECORD
    truth = record_truth(read(source))
    font = ImageFont.truetype(str(FONT), 24)
    small = ImageFont.truetype(str(FONT), 17)
    image = Image.new("RGB", (720, 416), "white")
    draw = ImageDraw.Draw(image)
    draw.text((24, 16), "Q4T visual v1 - actual acceptance record", fill="black", font=font)
    draw.text((24, 54), "2026-11-07 | Attempt counts, including failures", fill="black", font=small)
    for i, (name, value) in enumerate(truth["totals"].items()):
        y = 100 + 64*i
        draw.rectangle((24, y, 696, y+52), outline="black", width=2)
        draw.text((40, y+10), name, fill="black", font=font)
        draw.text((580, y+10), str(value), fill="black", font=font)
    draw.text((24, 376), "Source: vision-service-v1-20261107/result.json", fill="black", font=small)
    image.save(folder / "record-table.png")
    image = Image.new("RGB", (720, 416), "white")
    draw = ImageDraw.Draw(image)
    draw.text((24, 16), "Actual Chat attempts by selected phase", fill="black", font=font)
    for i, (name, value) in enumerate(truth["chart"].items()):
        x, top = 90 + 210*i, 335 - 10*value
        draw.rectangle((x, top, x+105, 335), fill=(32, 102, 160))
        draw.text((x+32, top-32), str(value), fill="black", font=font)
        draw.text((x-8, 350), name, fill="black", font=small)
    draw.text((24, 390), "Source: vision-service-v1-20261107/result.json", fill="black", font=small)
    image.save(folder / "record-chart.png")
    s.save(folder / "truth.json", dict(**truth, source=RECORD,
        source_sha256=s.sha(source), font_sha256=s.sha(FONT),
        provenance="programmatic render of real committed records, not a scan"))
    return truth


def image_cases(folder, truth):
    table = s.image_part(folder / "record-table.png")
    chart = s.image_part(folder / "record-chart.png")
    schema = s.structured({k: dict(type="integer") for k in truth["totals"]})
    return [dict(label="record-table", request=s.request(
        "Read all four attempt counts. Return JSON keys starts, generations, controls, native.",
        [table], response_format=schema), expected=truth["totals"]),
        dict(label="record-chart", request=s.request(
        "Which phase has the tallest bar? Return JSON key phase with its exact label.",
        [chart], stream=True, response_format=s.structured(dict(phase=dict(type="string")))),
        expected=dict(phase=truth["tallest"])),
        dict(label="record-multiple", request=s.request(
        "Read the generations count from the table and the phase-05b count from the chart. Return JSON keys total and phase05b.",
        [table, chart], response_format=s.structured(dict(total=dict(type="integer"),
            phase05b=dict(type="integer")))), expected=dict(total=truth["totals"]["generations"],
                phase05b=truth["chart"]["phase-05b"]))]


def prepare():
    s.require(s.sha(s.ROOT/"build/q4t") == DEFAULT, "default identity changed")
    FOLDER.mkdir()
    originals = [(r["label"], r["request"], r["expected"]) for r in
                 read(s.BASE/"phase-04/requests.json") if r["label"].startswith("motion-")]
    encoded = s.BASE/"phase-04/execution/encoded-video-request.json"
    originals.append(("encoded-video", read(encoded)["body"], dict(direction="left_to_right")))
    rows = contrasts(originals)
    fixtures = FOLDER/"fixtures"
    fixtures.mkdir()
    truth = render_records(fixtures)
    rows += image_cases(fixtures, truth)
    s.save(FOLDER/"requests.json", rows)
    tools = copy.deepcopy(s.TOOLS[:1])
    tools[0]["function"]["strict"] = True
    body = s.request(f"Read the first 12 lines of {RECORD} with read_project_record. "
        "Then compare all four attempt counts in this image with the actual record. "
        "Report each count, say whether they agree, and cite the exact returned source.",
        [s.image_part(fixtures/"record-table.png")], stream=True, tools=tools,
        tool_choice=dict(type="function", function=dict(name="read_project_record")),
        parallel_tool_calls=False)
    s.save(FOLDER/"tool-request.json", body)
    probe = s.BASE/"engine/q4t_preflight_probe"
    envelopes = [dict(allow_media=True, max_len=16384, max_prefill=8192,
        body=dict(request=row["request"], include_prompt=True, include_token_ids=True)) for row in rows]
    result = subprocess.run([str(probe), str(s.MODEL/"tokenizer.json")],
        input="".join(json.dumps(x)+"\n" for x in envelopes), text=True,
        capture_output=True, timeout=90)
    (FOLDER/"preflight.stdout").write_text(result.stdout)
    (FOLDER/"preflight.stderr").write_text(result.stderr)
    result.check_returncode()
    parsed = [json.loads(x) for x in result.stdout.splitlines()]
    s.require(len(parsed) == len(rows), "preflight output count")
    for row, observed in zip(rows, parsed):
        s.require("error" not in observed, row["label"] + ": " + str(observed.get("error")))
    for i in range(0, 12, 3):
        for field in ("token_ids", "prompt", "prompt_tokens"):
            s.require(all(p[field] == parsed[i][field] for p in parsed[i:i+3]),
                      "preflight contrast changed " + field)
    s.save(FOLDER/"preflight.json", dict(zip((r["label"] for r in rows), parsed)))
    paths = [s.ROOT/n for n in subprocess.check_output(["git", "ls-files"],
        cwd=s.ROOT, text=True).splitlines() if n.startswith(("src/", "include/",
        "cmake/", "tools/", "docs/serve-")) or n == "CMakeLists.txt"]
    paths += list(FOLDER.glob("*.json")) + list(fixtures.iterdir())
    paths += [s.ROOT/"build/q4t", s.ROOT/"build/q4t.release.json", probe,
        s.ROOT/RECORD, FONT, encoded, s.BASE/"phase-04/requests.json",
        s.ROOT/"docs/VISION_CLOSEOUT_PLAN_2026-11-07.md"]
    paths += [s.MODEL/n for n in ("config.json", "preprocessor_config.json",
        "video_preprocessor_config.json", "tokenizer.json")]
    plan = dict(profile="q4t-visual-closeout-a-v1", limits=LIMITS, total=TOTAL,
        port=s.PORT, binary=str(s.ROOT/"build/q4t"), model=str(s.MODEL),
        cpu_video=True, tower_repair=False,
        reused=dict(video=str(s.BASE/"phase-01/execution/hardware/motion-h264.mp4"),
                    numerical="unchanged ea1b474d official HF full tower"),
        source_commit=subprocess.check_output(["git", "rev-parse", "HEAD"],
            cwd=s.ROOT, text=True).strip(), files={str(p):s.sha(p) for p in paths})
    budget_plans([read(p) for p in BASE.glob("*/plan.json")] + [plan])
    s.save(FOLDER/"plan.json", plan)
    return dict(contrasts=12, image_requests=3, tool_requests=2,
                same_input_and_tokens=True, limits=LIMITS)


class Run(s.Run):
    limits = LIMITS
    success_status = "CONTRASTS_RECORDED_IMAGE_BUSINESS_CHECKED"

    def business(self):
        budget_plans([read(p) for p in BASE.glob("*/plan.json")])
        cpu = read(self.folder/"preflight.json")
        for row in read(self.folder/"requests.json"):
            label, body = row["label"], row["request"]
            pre = json.loads(self.http(label+"-preflight", "/v1/chat/preflight",
                dict(request=body, include_prompt=True, include_token_ids=True)))
            for field in ("token_ids", "prompt", "prompt_tokens"):
                s.require(pre[field] == cpu[label][field], "HTTP/CPU prompt drift")
            value = self.complete(label, body)
            s.require(value.finish_reason == "stop", "truncated response: " + label)
            s.require(value.usage["prompt_tokens"] == pre["prompt_tokens"],
                      "actual prompt accounting drift")
            content = value.message.get("content", "")
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError:
                parsed = None
            if row.get("mode") in ("strict", "json") or not row.get("group"):
                s.require(isinstance(parsed, dict), "invalid JSON output")
            if row.get("mode") == "strict":
                s.Draft202012Validator(body["response_format"]["json_schema"]["schema"]).validate(parsed)
            quality = parsed == row["expected"]
            self.observe(label, True if row.get("group") else quality,
                diagnostic_only=bool(row.get("group")), expected=row["expected"],
                actual=content, exact_json_quality_passed=quality,
                usage=value.usage)
        self.tools()
        self.idle("final-idle")

    def tools(self):
        body = read(self.folder/"tool-request.json")
        value = self.complete("record-tool-call", body)
        calls = s.client.prepare_tool_calls(value, s.VALIDATORS)
        s.require(len(calls) == 1 and calls[0].arguments == dict(
            path=RECORD, start_line=1, line_count=12), "unexpected record read")
        result = s.ProjectRecords(s.ROOT).execute(calls[0])
        s.require(result["ok"] and result["source"]["sha256"] ==
            read(self.folder/"fixtures/truth.json")["source_sha256"], "source mismatch")
        s.save(self.output/"record-tool-result.json", result)
        history = s.client.append_tool_results(body["messages"], value,
            {calls[0].call_id: json.dumps(result, ensure_ascii=False)})
        follow = s.client.make_request(history, body["tools"], stream=False,
            max_tokens=256, timeout_ms=120000)
        follow["tool_choice"] = "none"
        final = self.complete("record-tool-final", follow)
        content = final.message.get("content", "")
        # Counts are checked independently from raw text during final audit.
        self.observe("record-tool-loop", final.finish_reason == "stop" and
            result["source"]["citation"] in content, content=content,
            expected=read(self.folder/"fixtures/truth.json")["totals"])


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "run"))
    print(json.dumps(prepare() if parser.parse_args().action == "prepare" else Run(FOLDER).run()))
