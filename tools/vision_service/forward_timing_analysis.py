"""Strict offline contract for vision forward CUDA-stream event diagnostics."""
import math

from tools.vision_service.timing_analysis import require

PREFIX = "[q4t][vision-forward-timing] "
STAGES = ("patch", "position_tables", "position_embed", "norm", "qkv", "rope",
          "attention", "projection", "mlp", "merger")


def parse_records(text):
    result = {}
    fields = {"version", "id", "valid", "clock", "items", "patches", "depth",
              "events", "total_ms", "other_ms", "setup_host_ms", "read_host_ms",
              "position_host_ms"}
    fields.update(s + suffix for s in STAGES for suffix in ("_ms", "_calls"))
    for line in text.splitlines():
        if not line.startswith("[q4t][vision-forward-timing]"):
            continue
        require(line.startswith(PREFIX), "malformed forward timing prefix")
        pairs = [part.split("=", 1) for part in line[len(PREFIX):].split()]
        require(all(len(p) == 2 for p in pairs), "malformed forward field")
        row = dict(pairs)
        require(len(row) == len(pairs), "duplicate forward field")
        require(row.get("version") == "1" and row.get("id") and
                row["id"] not in result, "version or duplicate request id")
        if row.get("valid") == "0":
            require(set(row) == {"version", "id", "valid", "error", "error_code",
                                 "events"} and row["error"] and
                    row["error_code"].isdigit() and row["events"].isdigit(),
                    "malformed failed forward timing")
            row["valid"] = 0
            result[row["id"]] = row
            continue
        require(set(row) == fields and row["valid"] == "1" and
                row["clock"] == "cuda_stream", "incomplete forward fields")
        for key in ("valid", "items", "patches", "depth", "events",
                    *(s + "_calls" for s in STAGES)):
            require(row[key].isdigit(), "invalid forward count: " + key)
            row[key] = int(row[key])
        for key in ("total_ms", "other_ms", "setup_host_ms", "read_host_ms",
                    "position_host_ms", *(s + "_ms" for s in STAGES)):
            row[key] = float(row[key])
            require(math.isfinite(row[key]) and row[key] >= 0,
                    "invalid forward duration: " + key)
        require(row["total_ms"] > 0 and row["items"] > 0 and row["patches"] > 0
                and row["depth"] > 0 and 0 < row["events"] <= 8192,
                "invalid forward geometry/capacity")
        for stage in STAGES:
            require(row[stage + "_calls"] or row[stage + "_ms"] == 0,
                    "forward duration without calls")
        # Only six-place decimal printing error (eleven summands).
        require(abs(row["other_ms"] + sum(row[s + "_ms"] for s in STAGES) -
                    row["total_ms"]) <= .000006, "forward partition differs")
        result[row["id"]] = row
    return result


def validate_request(row, body, wall, depth, patches):
    require(row["valid"] == 1 and row["id"] == body["request_id"],
            "invalid or wrong request forward timing")
    require(row["depth"] == depth and row["patches"] == patches == wall["patches"]
            and row["items"] == wall["items"] and wall["completed"] == 1,
            "forward geometry differs")
    n = row["items"]
    counts = dict(patch=1, position_tables=n, position_embed=n, merger=n,
                  norm=2*n*depth, qkv=n*depth, rope=n*depth, attention=n*depth,
                  projection=n*depth, mlp=n*depth)
    require(all(row[s + "_calls"] == v for s, v in counts.items()) and
            row["events"] == 2 + 2 * sum(counts.values()),
            "missing or extra forward interval")
    return row
