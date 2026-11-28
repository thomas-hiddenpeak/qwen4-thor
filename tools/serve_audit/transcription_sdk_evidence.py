"""Attribute the signed-in mixed-speech requests to their observed client and release."""
import hashlib
import json

from .render_sdk_matrix import sha


SDK = "python-3.55.1"
ROUTE = "/v1/audio/transcriptions"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def signed(path, expected):
    require(sha(path) == expected, f"Transcription evidence changed: {path.name}")
    return json.loads(path.read_bytes())


def transcription(actor, witness, source):
    wire = actor["wire"].encode()
    segments = json.loads(wire)["segments"]
    labels = [segment["speaker"] for segment in segments]
    expected = ("POST", ROUTE, 200, witness["trace"])
    require(actor["sdk"] == SDK.removeprefix("python-") and
            tuple(actor[k] for k in ("method", "path", "status", "trace")) == expected and
            actor["source_sha256"] == source and actor["request"]["response_format"] == "diarized_json" and
            hashlib.sha256(wire).hexdigest() == actor["wire_sha256"] == witness["wire_sha256"] and
            labels == actor["labels"] == witness["typed_speakers"] and len(set(labels)) == 2,
            "SDK, endpoint, request ID, mixed source, response bytes or speakers differ")
    return SDK, {key: actor[key] for key in ("method", "path", "status", "trace")}


def transcription_campaigns(directory):
    issued = json.loads((directory / "delivery.json").read_bytes())
    pub = issued["publication"]
    proofs = issued["sdk_evidence"]
    captures = issued["transcription_captures"]
    device = signed(directory / "device.json", proofs["device.json"])
    capacity = signed(directory / "capacity.json", proofs["capacity.json"])
    managed = signed(directory / "managed.json", proofs["managed.json"])
    require(pub["status"] == device["status"] == managed["status"] == "PASS" and
            pub["engine"] == device["engine"] == capacity["engine"] == managed["engine"] and
            device["ingress"] == capacity["ingress"] and
            device["guard"]["status"] == "COMPLETED" and device["guard"]["child_returncode"] == 0 and
            device["sdk"] == SDK.removeprefix("python-") and device["ports_released"] and
            capacity["status"] == "ACCEPT_OVERLAP_DISCOVERY_PRIMARY_LIGHT" and capacity["model_starts"] == 2 and
            managed["model_starts"] == 4, "Mixed-speech suites do not match the issued engine and cleanup")
    source = device["source_sha256"]
    def campaign(label, receipt, name, packets, transport, **details):
        return label, packets, dict(revision=pub["publication_revision"], sdk=SDK, requests=len(packets),
            observed="qualified candidate before issuance", transport=transport, engine=pub["engine"],
            receipt=f"{directory.name}/{receipt}", receipt_sha256=proofs[receipt],
            wire=f"{directory.name}/sdk-wire/{name}.json", wire_sha256=captures[name], **details)
    def actor(name):
        return signed(directory / "sdk-wire" / (name + ".json"), captures[name])

    tls = "authenticated native TLS"
    summary = actor("device-sdk")
    require(summary["version"] == device["sdk"] and summary["chat"] == device["core_answer"] == "42" and
            summary["exchanges"] == device["authenticated_requests"] and
            summary["typed_speakers"] == device["typed_speakers"], "Mixed-speech TLS capture differs")
    discovery, request, chat = summary["exchanges"]
    require([(w["method"], w["path"], w["status"]) for w in (discovery, request, chat)] ==
            [("GET", "/v1/capabilities", 200), ("POST", ROUTE, 200), ("POST", "/v1/chat/completions", 200)] and
            len({w["trace"] for w in summary["exchanges"]}) == 3, "Unexpected device exchanges")
    packet = transcription(actor("device-transcription"), dict(device, trace=request["trace"]), source)
    yield campaign("speech-tls", "device.json", "device-sdk", [packet, (SDK, chat)], tls,
                   direct_http_controls=1, transcription_capture_sha256=captures["device-transcription"])

    for role in ("primary", "light"):
        row = capacity["capacity"][role]
        require(row["mtp"] is (role == "primary") and row["transcription_attempts"] == 1 and
                row["source_sha256"] == source, "Transcription capacity identity differs")
        name = f"capacity-{role}"
        yield campaign(f"speech-{name}", "capacity.json", name, [transcription(actor(name), row, source)],
                       tls, capacity=role, mtp=row["mtp"], profile_sha256=row["record_sha256"])

    for role, incident in (("fallback", "model"), ("primary", "gateway")):
        row = managed["roles"][role]
        require(row["manifest"] == pub[role] and row["model_starts"] == 2 and row["guardian_same"] and
                row["new_generation"] > row["old_generation"] and row["ports_free"] and
                row["default_unchanged"] and not row["remaining"] and
                (role != "primary" or row["registered_fallback"] == pub["fallback"]),
                "Managed speech recovery has no qualified serving generation")
        for phase in ("prepare", "restore"):
            name = f"{role}-{phase}"
            seen = actor(name)
            observed = row["requests"][phase]
            require(captures[name] == observed["actor_sha256"] and
                    all(seen[k] == observed[k] for k in ("method","path","status","sdk","source_sha256","trace")),
                    "Managed speech actor differs from the recovery receipt")
            witness = dict(observed, typed_speakers=observed["speaker_labels"])
            after = phase == "restore"
            manifest = pub["fallback"] if after and role == "primary" else row["manifest"]
            yield campaign(f"speech-{incident}-{phase}", "managed.json", name,
                           [transcription(seen, witness, source)], "authenticated native loopback HTTP",
                           incident=row["incident"], initial_manifest=row["manifest"], manifest=manifest,
                           generation=row["new_generation"] if after else row["old_generation"],
                           phase="after_recovery" if after else "before_incident")


def issued_transcription_campaigns(directory, clients):
    issued = json.loads((directory / "delivery.json").read_bytes())
    publication = issued["publication"]
    proof = signed(directory / "clients.json", issued["sdk_evidence"]["clients.json"])
    run = proof["runtime"]
    require(publication["status"] == proof["status"] == run["status"] == "PASS" and
            proof["publication"] == publication["publication_revision"] and
            proof["package"] == publication["primary"] and proof["engine"] == publication["engine"] and
            proof["guard"]["status"] == "COMPLETED" and proof["guard"]["backend"] == "nvml" and
            proof["guard"]["child_returncode"] == 0 and proof["guard"]["samples"] > 0 and
            run["starts"] == 1 and run["requests"] == 2 * len(clients) and run["model_exit"] == 0 and
            run["ports_free"] and run["unchanged_default"] and run["private_config_reads"] == 1 and
            set(proof["sdk"]) == set(clients), "Cross-version speech evidence differs from the issued release")
    for label in clients:
        row = proof["sdk"][label]
        path = directory / "sdk-wire" / (label + ".json")
        captured = signed(path, row["capture_sha256"])
        require(captured["sdk"] == label and captured["requests"] == row["requests"] and
                len(row["requests"]) == 2 and all((w["method"], w["path"], w["status"]) ==
                ("POST", ROUTE, 200) for w in row["requests"]), "Wrong SDK, source operation or request count")
        result, events = captured["unary"], captured["events"]
        require([v["type"] for v in events] == row["event_types"] ==
                ["transcript.text.delta","transcript.text.segment","transcript.text.delta",
                 "transcript.text.segment","transcript.text.done"] and
                [v for v in events if v["type"] == "transcript.text.segment"] == result["segments"] and
                len({v["speaker"] for v in result["segments"]}) == row["segments"] == 2 and
                "".join(v["delta"] for v in events if v["type"] == "transcript.text.delta") == result["text"] and
                all(events[-1][k] == result[k] for k in ("duration", "task", "segments", "text", "usage")),
                "Transcription segments, text or terminal usage differ from the unary response")
        for i, mode in enumerate(("json", "sse")):
            yield f"speech-issued-{label}-{mode}", [(label, row["requests"][i])], dict(
                revision=proof["publication"], transport="authenticated native TLS", sdk=label,
                requests=1, observed="issued engine and ingress", mode=mode, manifest=proof["package"],
                engine=proof["engine"], ingress=proof["ingress"], receipt=f"{directory.name}/clients.json",
                receipt_sha256=issued["sdk_evidence"]["clients.json"],
                wire=f"{directory.name}/sdk-wire/{path.name}", wire_sha256=row["capture_sha256"])
