#!/usr/bin/env python3
"""Fail when serve's reviewed surface drifts; does not certify its semantics."""
import argparse
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
INVENTORY = ROOT / "tools/serve_audit/inventory.json"


def current_surface(root):
    contract = (root / "src/server/chat_contract.cpp").read_text()
    match = re.search(r'Fields\(req,\s*\{([^}]+)\}', contract)
    if not match:
        raise ValueError("cannot extract Chat allowlist; review extractor")
    fields = re.findall(r'"([a-z_]+)"', match[1])
    completion = (root / "src/server/completion_plan.cpp").read_text()
    match = re.search(r'InputFields\(\s*req,\s*\{([^}]+)\}', completion)
    if not match:
        raise ValueError("cannot extract Completions allowlist; review extractor")
    completion_fields = re.findall(r'"([a-z_]+)"', match[1])
    dispatch = (root / "src/server/chat_http.cpp").read_text()
    routes = re.findall(r'path == "([^"]+)" && method == "([A-Z]+)"', dispatch)
    if 'if (IsModelDetailPath(path))' not in dispatch or \
            'method != "GET" && method != "DELETE"' not in dispatch or \
            'HandleModel(fd, method, path)' not in dispatch:
        raise ValueError("review dynamic model backend route extractor")
    routes.extend(("/v1/models/{model}", method) for method in ("GET", "DELETE"))
    native = (root / "src/service/ingress_main.cpp").read_text()
    native_routes = re.findall(
        r'\{\s*"(GET|POST)"\s*,\s*"([^"]+)"\s*,\s*(true|false)\s*\}', native)
    if len(native_routes) < 5:
        raise ValueError("cannot extract native ingress routes; review extractor")
    if 'server::IsModelDetailPath(path)' not in native or \
            'method == "GET" || method == "DELETE"' not in native:
        raise ValueError("review dynamic model native route extractor")
    native_routes.extend((method, "/v1/models/{model}", "true")
                         for method in ("GET", "DELETE"))
    application_routes = (root / "include/q4t/server/application_routes.h").read_text()
    if 'IsConversationTarget(path)' not in dispatch or \
            'HandleConversationRequest(*objects_' not in dispatch or \
            'server::ConversationMethodAllowed(method, path)' not in native or \
            'inline bool ConversationMethodAllowed' not in application_routes:
        raise ValueError("review dynamic Conversations transport extractor")
    conversations = [
        ("POST", "/v1/conversations"),
        *((method, "/v1/conversations/{conversation_id}")
          for method in ("GET", "POST", "DELETE")),
        *((method, "/v1/conversations/{conversation_id}/items")
          for method in ("GET", "POST")),
        *((method, "/v1/conversations/{conversation_id}/items/{item_id}")
          for method in ("GET", "DELETE"))]
    routes.extend((path, method) for method, path in conversations)
    native_routes.extend((method, path, "false") for method, path in conversations)
    if 'ServeResponsesState(fd, *objects_' not in dispatch or \
            'server::ResponsesMethodAllowed(method, path)' not in native or \
            'inline bool ResponsesMethodAllowed' not in application_routes:
        raise ValueError("review dynamic Responses transport extractor")
    responses = [("GET", "/v1/responses/{response_id}"),
                 ("DELETE", "/v1/responses/{response_id}"),
                 ("POST", "/v1/responses/{response_id}/cancel"),
                 ("GET", "/v1/responses/{response_id}/input_items")]
    routes.extend((path, method) for method, path in responses)
    if 'server::IsResponsesObservation(method, path)' not in native or \
            'server::IsResponseCancellation(method, path)' not in native or \
            'path.substr(end) == "/cancel"' not in application_routes:
        raise ValueError("review Responses cancellation/control route extractor")
    native_routes.append(("POST", "/v1/responses", "false"))
    native_routes.extend((method, path, "true") for method, path in responses)
    if 'if (method == "OPTIONS")' in native:
        if 'config.cors.Preflight(' not in native:
            raise ValueError("review native OPTIONS handler before cataloguing")
        native_routes.extend(("OPTIONS", path, "true")
                             for path in sorted({row[1] for row in native_routes}))
    if not fields or not routes:
        raise ValueError("empty protocol inventory")
    nested = {}
    for file in ("chat_contract.cpp", "output_constraints.cpp",
                 "input_contract.cpp", "chat_tool_history.cpp",
                 "chat_template.cpp", "legacy_tools.cpp", "completion_plan.cpp",
                 "conversation_api.cpp", "responses_plan.cpp", "responses_api.cpp"):
        source = (root / "src/server" / file).read_text()
        for index, item in enumerate(re.finditer(
                r'(?:Input)?Fields\(\s*([^,]+),\s*\{([^}]+)\}', source)):
            nested[f"{file}:{index}:{item[1]}"] = re.findall(
                r'"([a-z_]+)"', item[2])
    # These anchors intentionally cover more than the wire protocol identity:
    # reader, cancellation, scheduling, configuration, and tokenizer matter too.
    paths = sorted(set(root.glob("src/server/**/*.*")) |
                   set(root.glob("include/q4t/server/**/*.*")) |
                   set(root.glob("src/service/**/*.cpp")) |
                   set(root.glob("include/q4t/service/**/*.h")) |
                   {p for p in root.glob("tools/serve_recovery/*.py")
                    if not p.name.startswith("test_")} |
                   {p for p in root.glob("tools/serve_service/*.py")
                    if not p.name.startswith("test_")} | {
                       root / "src/io/json.cpp", root / "src/text/tokenizer.cpp",
                       root / "src/runtime/system_memory.cpp",
                       root / "src/runtime/startup_reservation.cpp",
                       root / "src/runtime/cuda_allocation_quota.cpp",
                       root / "src/runtime/cuda_quota_backend.h",
                       root / "include/q4t/runtime/cuda_allocation_quota.h",
                       root / "include/q4t/runtime/allocation_quota.h",
                       root / "include/q4t/runtime/cleanup_policy.h",
                       root / "include/q4t/runtime/device_memory.h",
                       root / "include/q4t/runtime/model_health.h",
                       root / "include/q4t/ple/page_reader.h",
                       root / "include/q4t/ple/ple_embedding.h",
                       root / "src/ple/page_reader.cpp",
                       root / "src/ple/ple_embedding.cpp",
                       root / "include/q4t/vision/vision.h",
                       root / "include/q4t/vision/processor.h",
                       root / "include/q4t/vision/timing.h",
                       root / "include/q4t/vision/forward_timing.h",
                       root / "include/q4t/vision/forward_timing_core.h",
                       root / "include/q4t/vision/media_plan.h",
                       root / "include/q4t/vision/configuration.h",
                       root / "src/vision/media_plan.cpp",
                       root / "src/vision/vision.cu",
                       root / "src/vision/processor.cpp",
                       root / "include/q4t/model/vision_positions.h",
                       root / "src/model/model_owner.cpp",
                       root / "src/model/model.cu", root / "src/mtp/mtp.cu",
                       root / "cmake/CudaCleanupBoundary.cmake",
                       root / "CMakeLists.txt",
                       root / "cmake/GuardianProcess.cmake",
                       root / "cmake/NativeIngress.cmake",
                       root / "cmake/RecoveryProbe.cmake",
                       root / "src/service/cuda_probe.cu",
                       root / "include/q4t/runtime/system_memory.h",
                       root / "src/main.cpp", root / "cmake/StructuredOutput.cmake",
                       root / "cmake/ChatProtocolIdentity.cmake",
                       root / "cmake/RustRegex.cmake",
                       root / "cmake/regex-syntax-0.8.8.sha256",
                       root / "src/server/regex_syntax/lib.rs"})
    return {"fields": sorted(fields), "completion_fields": sorted(completion_fields),
            "routes": sorted(f"{method} {path}" for path, method in routes),
            "native_routes": sorted(f"{method} {path}" for method, path, _ in native_routes),
            "whitelists": nested,
            "sources_sha256": {str(p.relative_to(root)):
                               hashlib.sha256(p.read_bytes()).hexdigest()
                               for p in paths}}


def check(inventory, actual, root=ROOT):
    errors = []
    for group in ("fields", "completion_fields", "routes", "native_routes"):
        recorded = inventory.get(group, {})
        missing = set(actual[group]) - recorded.keys()
        stale = recorded.keys() - set(actual[group])
        if missing or stale:
            errors.append(f"{group}: uncatalogued={sorted(missing)} "
                          f"removed={sorted(stale)}")
        for name, row in recorded.items():
            for key in ("semantics", "boundary", "evidence"):
                if not row.get(key):
                    errors.append(f"{group}.{name}: missing {key}")
            for path in row.get("evidence", []):
                if not (root / path).is_file():
                    errors.append(f"missing evidence reference: {path}")
    for group in ("whitelists", "sources_sha256"):
        recorded = inventory[group]
        changed = sorted(k for k in recorded.keys() | actual[group].keys()
                         if recorded.get(k) != actual[group].get(k))
        if changed:
            errors.append(f"{group} requires renewed review: {changed}")
    return errors


def mutation_checks(inventory, actual):
    # Independent failure modes of the anti-omission gate, not serving tests.
    cases = [dict(actual, fields=actual["fields"] + ["uncatalogued_option"]),
             dict(actual, completion_fields=actual["completion_fields"] + ["uncatalogued_option"]),
             dict(actual, routes=actual["routes"] + ["GET /uncatalogued"]),
             dict(actual, native_routes=actual["native_routes"] + ["GET /uncatalogued"]),
             dict(actual, sources_sha256=dict(actual["sources_sha256"],
                                             **{"src/server/chat_http.cpp": "0"}))]
    for case in cases:
        if not check(inventory, case):
            raise ValueError("inventory mutation escaped detection")
    return len(cases)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    inventory = json.loads(INVENTORY.read_text())
    actual = current_surface(ROOT)
    errors = check(inventory, actual)
    mutations = mutation_checks(inventory, actual) if args.self_check else 0
    print(json.dumps({"inventory_current": not errors,
                      "semantics_certified": False,
                      "fields": len(actual["fields"]),
                      "completion_fields": len(actual["completion_fields"]),
                      "routes": len(actual["routes"]),
                      "native_routes": len(actual["native_routes"]),
                      "reviewed_source_anchors": len(actual["sources_sha256"]),
                      "mutation_checks": mutations, "errors": errors},
                     ensure_ascii=False, indent=2))
    raise SystemExit(bool(errors))


if __name__ == "__main__":
    main()
