"""Strict, offline parser for request-local vision wall-clock diagnostics."""
import math

STAGES = ("prepare", "decode_png", "decode_jpeg", "resize", "normalize",
          "patchify", "pack", "pixels_allocate", "pixels_upload", "workspace",
          "output_allocate", "forward", "cleanup")
PREFIX = "[q4t][vision-timing] "


def require(ok, message):
    if not ok:
        raise ValueError(message)


def parse_records(text):
    result = {}
    expected = {"version", "id", "clock", "completed", "items", "frames",
                "patches", "total_ms", "other_ms"}
    expected.update(name + suffix for name in STAGES
                    for suffix in ("_ms", "_calls"))
    for line in text.splitlines():
        if not line.startswith("[q4t][vision-timing]"):
            continue
        require(line.startswith(PREFIX), "malformed timing prefix")
        pairs = [field.split("=", 1) for field in line[len(PREFIX):].split()]
        require(all(len(pair) == 2 for pair in pairs), "malformed timing field")
        row = dict(pairs)
        require(len(row) == len(pairs) and set(row) == expected,
                "duplicate/missing/unknown timing fields")
        require(row["version"] == "1" and row["clock"] == "host_wall",
                "unsupported timing contract")
        require(row["id"] and row["id"] not in result, "duplicate request id")
        require(row["completed"] in ("0", "1"), "invalid completion flag")
        for name in ("completed", "items", "frames", "patches",
                     *(stage + "_calls" for stage in STAGES)):
            require(row[name].isdigit(), "invalid nonnegative count: " + name)
            row[name] = int(row[name])
        for name in ("total_ms", "other_ms", *(s + "_ms" for s in STAGES)):
            row[name] = float(row[name])
            require(math.isfinite(row[name]) and row[name] >= 0,
                    "invalid elapsed time: " + name)
        require(row["total_ms"] > 0, "empty total duration")
        for stage in STAGES:
            require(row[stage + "_calls"] or row[stage + "_ms"] == 0,
                    "duration without a call: " + stage)
        # Six decimal places per field, 14 summands. This only covers printed
        # rounding; it is not a performance tolerance or a measurement limit.
        accounted = row["other_ms"] + sum(row[s + "_ms"] for s in STAGES)
        require(abs(accounted - row["total_ms"]) <= .00001,
                "overlapping/missing timing accounting")
        result[row["id"]] = row
    return result


def input_counts(body):
    items = frames = png = jpeg = 0
    for message in body["messages"]:
        parts = message.get("content")
        if not isinstance(parts, list):
            continue
        for part in parts:
            kind = part["type"]
            if kind not in ("image_url", "video_frames"):
                continue
            items += 1
            urls = ([part["image_url"]["url"]] if kind == "image_url"
                    else part["video_frames"])
            frames += len(urls)
            for url in urls:
                if url.startswith("data:image/png;base64,"):
                    png += 1
                elif url.startswith("data:image/jpeg;base64,"):
                    jpeg += 1
                else:
                    raise ValueError("unknown fixed fixture codec")
    return dict(items=items, frames=frames, png=png, jpeg=jpeg)


def validate_request(row, body):
    counts = input_counts(body)
    require(row["id"] == body["request_id"] and row["completed"] == 1,
            "wrong request identity or incomplete pipeline")
    require(row["items"] == counts["items"] and
            row["frames"] == counts["frames"] and row["patches"] > 0,
            "wrong input geometry counts")
    for codec in ("png", "jpeg"):
        require(row["decode_" + codec + "_calls"] == counts[codec],
                "wrong decoder count: " + codec)
    for stage in ("prepare", "resize", "normalize", "patchify"):
        require(row[stage + "_calls"] == counts["items"],
                "wrong processor count: " + stage)
    for stage in ("pack", "pixels_allocate", "pixels_upload", "workspace",
                  "output_allocate", "forward"):
        require(row[stage + "_calls"] == 1, "missing pipeline stage: " + stage)
    require(row["cleanup_calls"] == 3, "cleanup measurement incomplete")
    return counts
