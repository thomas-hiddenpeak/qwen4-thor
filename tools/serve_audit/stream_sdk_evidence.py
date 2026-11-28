"""Read original SDK request IDs from the issued Responses stream campaign."""
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit


def verified(path, expected):
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError(f"Stream SDK evidence changed: {path.name}")
    return json.loads(data)


def actor(capture, client, ident, mode):
    if (capture["sdk"] != client or capture["id"] != ident or
            capture["status"] != "completed" or len(capture["requests"]) != 1):
        raise ValueError("Stream capture has the wrong SDK, answer, or request count")
    wire = capture["requests"][0]
    if "url" in wire:
        address = urlsplit(wire["url"])
        if address.scheme or address.netloc or wire["version"] != client.removeprefix("node-"):
            raise ValueError("Managed stream request has the wrong origin or SDK version")
        wire = {**wire, "path": address.path, "query": address.query}
    expected = ("POST", "/v1/responses", "") if mode == "create" else (
        "GET", "/v1/responses/" + ident, "stream=true")
    if tuple(wire[key] for key in ("method", "path", "query")) != expected or wire["status"] != 200:
        raise ValueError("Stream SDK requested a different method, response ID, or selector")
    return client, {key: wire[key] for key in ("method", "path", "query", "trace", "status")}


def stream_campaigns(directory, clients):
    issued = json.loads((directory / "delivery.json").read_bytes())
    pub = issued["publication"]
    device = verified(directory / "device.json", issued["sdk_evidence"]["device.json"])
    managed = verified(directory / "managed.json", issued["short_receipt_sha256"])
    if (pub["status"] != "PASS" or pub["engine"] != device["engine"] or
            managed["engine"] != pub["engine"] or device["guard"]["status"] != "COMPLETED" or
            set(device["readers"]) != set(clients)):
        raise ValueError("Stream campaign does not match its released engine or client set")
    yield from exchanges(directory, clients, pub, device, managed, issued)


def exchanges(directory, clients, pub, device, managed, issued):
    version = pub["publication_revision"]
    writer = "node-6.34.0"
    ident = device["id"]
    raw = directory / "sdk-wire/device"
    create = verified(raw / "create.json", device["create_sha256"])
    packets = [actor(create, writer, ident, "create")]
    for client in clients:
        replay = verified(raw / f"replay-{client}.json", device["readers"][client]["result_sha256"])
        packets.append(actor(replay, client, ident, "read"))
    captured = verified(raw / "requests.json", device["receipts"]["requests.json"])
    if (len(captured) != device["requests"] or
            len({w["trace"] for w in captured}) != len(captured) or
            any(wire not in captured for _, wire in packets)):
        raise ValueError("SDK exchanges differ from the native TLS campaign")
    reads = [w for w in captured if w["trace"] not in {p["trace"] for _, p in packets}]
    if (sorted(w["status"] for w in reads) != [200, 404] or
            any((w["method"], w["path"], w["query"]) != ("GET", "/v1/responses/" + ident, "") for w in reads)):
        raise ValueError("Unattributed SDK campaign traffic differs from direct HTTP controls")
    yield "stream-tls", packets, dict(revision=version, transport="authenticated native TLS",
        requests=len(packets), observed="qualified candidate before issuance", engine=device["engine"],
        ingress=device["ingress"], receipt=f"{directory.name}/device.json",
        receipt_sha256=issued["sdk_evidence"]["device.json"], direct_http_controls=len(reads))

    for role, incident in (("fallback", "model"), ("primary", "gateway")):
        row = managed["roles"][role]
        if (row["manifest"] != pub[role] or row["starts"] != 2 or not row["guardian_same"] or
                row["new_generation"] <= row["old_generation"] or not row["ports_released"] or
                (role == "primary" and row["registered_fallback"] != pub["fallback"])):
            raise ValueError("Managed stream takeover does not match the issued registration")
        raw = directory / "sdk-wire" / role
        decision = verified(raw / "decision.json", row["decision_sha256"])["actual_business"]
        for phase, actors in (("prepare", [writer]), ("restore", row["sdk_readers"])):
            packets = []
            for client in actors:
                name = f"{phase}-{client}.json"
                digests = [digest for path, digest in decision["original_sources"].items()
                           if Path(path).name == name]
                if len(digests) != 1:
                    raise ValueError("Managed stream actor has no unique signed request capture")
                captured = verified(raw / name, digests[0])
                packets.append(actor(captured, client, row["original_response"],
                                     "create" if phase == "prepare" else "read"))
            after = phase == "restore"
            serving = pub["fallback"] if after and role == "primary" else row["manifest"]
            yield f"stream-{incident}-{phase}", packets, dict(
                revision=version, transport="authenticated native loopback HTTP", requests=len(packets),
                phase="after_recovery" if after else "before_incident", incident=row["incident"],
                generation=row["new_generation"] if after else row["old_generation"],
                observed="qualified candidate before issuance", manifest=serving,
                initial_manifest=row["manifest"], receipt=f"{directory.name}/managed.json",
                receipt_sha256=issued["short_receipt_sha256"], decision_sha256=row["decision_sha256"])
