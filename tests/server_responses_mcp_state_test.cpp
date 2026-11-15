#include "q4t/server/responses_mcp_state.h"

#include <cstdlib>
#include <ctime>
#include <filesystem>
#include <fstream>
#include <latch>
#include <thread>

#include "../src/server/object_protocol.h"
#include "q4t/server/chat_protocol_identity.h"
#include "q4t/server/object_retention.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
Json Value(const std::string& text) {
  Json value;
  if (!Parse(text, &value)) throw std::runtime_error("MCP state fixture");
  return value;
}
StateAccess Access(std::string scope = "client:mcp-state") {
  return {std::move(scope), Clock::now() + 8s, {}};
}
ChatPlanConfig Config() {
  ChatPlanConfig config;
  config.model = "mcp-state-model";
  return config;
}
ResponsesMcpDefinition Definition(std::string approval = "always",
                                  Json patch = Object()) {
  auto declaration = Value(R"({"type":"mcp","server_label":"project",
    "server_url":"https://project.example/mcp",
    "authorization":"fixture-secret-token",
    "headers":{"X-Private":"fixture-secret-header"}})");
  Set(&declaration, "require_approval", String(std::move(approval)));
  for (const auto& [key, field] : patch.object) Set(&declaration, key, field);
  ResponsesMcpDefinition definition;
  ChatPlanError error;
  if (!PlanResponsesMcpDefinition(declaration, &definition, &error))
    throw std::runtime_error("MCP definition fixture");
  return definition;
}
Json Tools() {
  return Value(R"([{"name":"read","inputSchema":{"type":"object",
    "properties":{"path":{"type":"string"}},"required":["path"],
    "additionalProperties":false},"outputSchema":{"type":"object",
    "properties":{"text":{"type":"string"}},"required":["text"],
    "additionalProperties":false},"annotations":{"readOnlyHint":true}}])");
}
bool Ok(const ObjectReply& reply) {
  if (!reply.ok() || reply.commit_unknown)
    std::fprintf(stderr, "%s: %s\n", reply.code.c_str(), reply.message.c_str());
  return reply.ok() && !reply.commit_unknown;
}
struct Fixture {
  ObjectStoreOptions options;
  Fixture() {
    const auto* output = std::getenv("Q4T_TEST_OUTPUT_DIR");
    auto root = std::filesystem::absolute(
        output ? output : ".q4t-work/responses-mcp-state-cpu");
    std::filesystem::create_directories(root);
    auto directory = (root / "mcp-state-XXXXXX").string();
    if (!mkdtemp(directory.data()))
      throw std::runtime_error("fixture directory");
    options.directory = directory + "/objects";
    options.restart_backoff_ms = 0;
  }
};
Json Catalog(ResponsesMcpState& state,
             const ResponsesMcpDefinition& definition = Definition(),
             int lifetime = 600) {
  auto reply = state.Catalog(definition, Tools(), Access(), lifetime);
  if (!Ok(reply)) throw std::runtime_error("fixture catalog");
  return reply.value;
}
ObjectReply Propose(ResponsesMcpState& state, const Json& catalog,
                    const ResponsesMcpDefinition& definition = Definition(),
                    Json args = Value(R"({"path":"docs/MODEL.md"})"),
                    std::string caller = "direct") {
  return state.Propose(catalog, definition, "read", std::move(args),
                       std::move(caller), Access());
}
McpTransportReceipt Success(std::string dispatch_id = "") {
  McpTransportReceipt receipt;
  receipt.dispatch_id = std::move(dispatch_id);
  receipt.call_permitted = true;
  receipt.result = Value(R"({"content":[{"type":"text","text":"model info"}],
                            "structuredContent":{"text":"model info"}})");
  return receipt;
}
Json Record(ObjectStore& store, std::string id) {
  ObjectRequest request;
  request.kind = ObjectKind::kToolRun;
  request.scope = Access().scope;
  request.id = std::move(id);
  auto reply = store.Execute(request, Access().deadline);
  if (!Ok(reply)) throw std::runtime_error("fixture record");
  return reply.value;
}
}  // namespace

Q4T_TEST(
    responses_mcp_credentials_must_be_resupplied_and_http_error_is_standard) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  ResponsesMcpState state(objects, Config());
  const auto definition = Definition("never");
  const auto catalog = Catalog(state, definition);
  auto proposed = Propose(state, catalog, definition);
  Q4T_CHECK(Ok(proposed));
  const auto id = proposed.value.GetString("id");
  ResponsesMcpDefinition anonymous;
  ChatPlanError error;
  Q4T_CHECK(
      PlanResponsesMcpDefinition(definition.declaration, &anonymous, &error));
  Q4T_CHECK(state.Permit(id, anonymous, Access()).code ==
            "mcp_credentials_required");
  Q4T_CHECK(state.Inspect(id, Access()).value.GetString("state") == "ready");
  const auto permit = state.Permit(id, definition, Access());
  Q4T_CHECK(Ok(permit));
  auto receipt = Success(permit.value.GetString("dispatch_id"));
  receipt.outcome_unknown = true;
  auto transport = Error(502, "mcp_http_error", "Remote request failed");
  transport.value = Object({{"upstream_status", Number(503)}});
  const auto finished = state.Finish(id, receipt, transport, Access());
  Q4T_CHECK(Ok(finished) && finished.value.GetString("status") == "failed");
  Q4T_CHECK(finished.value.Find("error")->GetString("type") == "http_error" &&
            finished.value.Find("error")->GetInt("code") == 503 &&
            state.Inspect(id, Access()).value.GetString("state") == "unknown");
  return true;
}

Q4T_TEST(responses_mcp_state_catalog_rejects_forgery_scope_and_model) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesMcpState state(objects, Config());
  const auto catalog = Catalog(state);
  auto forged = catalog;
  Set(&forged, "server_label", String("forged"));
  Q4T_CHECK(!Propose(state, forged).ok());
  forged = catalog;
  Set(&Field(forged, "tools").array[0], "input_schema", Object());
  Q4T_CHECK(!Propose(state, forged).ok());
  Q4T_CHECK(!state
                 .Propose(catalog, Definition(), "read", Object(), "direct",
                          Access("client:other"))
                 .ok());
  auto config = Config();
  config.model = "different-model";
  ResponsesMcpState different(objects, config);
  Q4T_CHECK(!Propose(different, catalog).ok());
  Q4T_CHECK(Ok(Propose(state, catalog)));
  return true;
}

Q4T_TEST(responses_mcp_state_approval_single_use_and_denial_survive_restart) {
  Fixture f;
  std::string id, denied;
  {
    ObjectStore objects(f.options);
    ResponsesMcpState state(objects, Config());
    const auto catalog = Catalog(state);
    const auto proposal = Propose(state, catalog);
    Q4T_CHECK(Ok(proposal) &&
              proposal.value.GetString("type") == "mcp_approval_request");
    id = proposal.value.GetString("id");
    Q4T_CHECK(!state.Permit(id, Definition(), Access()).ok());
    const auto allowed =
        state.Decide(id, true, "read only", Definition(), Access());
    Q4T_CHECK(Ok(allowed) &&
              allowed.value.GetString("approval_request_id") == id);
    Q4T_CHECK(
        state.Decide(id, true, "again", Definition(), Access()).http_status ==
        409);
    auto next = Propose(state, catalog);
    Q4T_CHECK(Ok(next));
    denied = next.value.GetString("id");
    next = state.Decide(denied, false, "not allowed", Definition(), Access());
    Q4T_CHECK(Ok(next) && next.value.GetString("status") == "failed");
  }
  ObjectStore objects(f.options);
  ResponsesMcpState state(objects, Config());
  Q4T_CHECK(Ok(state.Permit(id, Definition(), Access())));
  Q4T_CHECK(state.Permit(id, Definition(), Access()).http_status == 409);
  Q4T_CHECK(!state.Permit(denied, Definition("never"), Access()).ok());
  const auto persisted = state.Inspect(denied, Access());
  Q4T_CHECK(Ok(persisted) && persisted.value.GetString("state") == "denied");
  return true;
}

Q4T_TEST(responses_mcp_state_concurrent_permission_sends_at_most_once) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesMcpState state(objects, Config());
  const auto definition = Definition("never");
  const auto proposal = Propose(state, Catalog(state, definition), definition);
  Q4T_CHECK(Ok(proposal));
  const auto id = proposal.value.GetString("id");
  std::latch start(1);
  ObjectReply a, b;
  std::jthread first([&] {
    start.wait();
    a = state.Permit(id, definition, Access());
  });
  std::jthread second([&] {
    start.wait();
    b = state.Permit(id, definition, Access());
  });
  start.count_down();
  first.join();
  second.join();
  Q4T_CHECK(static_cast<int>(a.ok()) + static_cast<int>(b.ok()) == 1);
  Q4T_CHECK(!a.commit_unknown && !b.commit_unknown);
  Q4T_CHECK(state.Inspect(id, Access()).value.GetString("state") ==
            "dispatching");
  const auto winner = a.ok() ? a : b;
  Q4T_CHECK(!winner.value.GetString("dispatch_id").empty());
  McpTransportReceipt losing;
  auto failure = Error(409, "mcp_dispatch_not_ready", "Other request won");
  Q4T_CHECK(state.Finish(id, losing, failure, Access()).code ==
            "mcp_dispatch_owner_conflict");
  losing.dispatch_id = "mcpd_forged";
  losing.call_permitted = true;
  losing.outcome_unknown = true;
  Q4T_CHECK(state.Finish(id, losing, failure, Access()).code ==
            "mcp_dispatch_owner_conflict");
  Q4T_CHECK(state.Inspect(id, Access()).value.GetString("state") ==
            "dispatching");
  Q4T_CHECK(Ok(state.Finish(id, Success(winner.value.GetString("dispatch_id")),
                            {}, Access())));
  return true;
}

Q4T_TEST(
    responses_mcp_state_original_schema_and_current_permissions_intersect) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesMcpState state(objects, Config());
  const auto definition = Definition("never");
  const auto catalog = Catalog(state, definition);
  Q4T_CHECK(!Propose(state, catalog, definition, Value(R"({"path":4})")).ok());
  Q4T_CHECK(!Propose(state, catalog, definition, Object()).ok());
  Q4T_CHECK(
      !Propose(state, catalog, definition, Value(R"({"path":"x","extra":1})"))
           .ok());
  const auto proposal = Propose(state, catalog, definition);
  Q4T_CHECK(Ok(proposal));
  const auto id = proposal.value.GetString("id");
  for (const auto* patch :
       {R"({"allowed_tools":[]})", R"({"allowed_callers":["programmatic"]})",
        R"({"server_url":"https://another.example/mcp"})",
        R"({"server_label":"other"})"}) {
    const auto current = Definition("never", Value(patch));
    Q4T_CHECK(!Propose(state, catalog, current).ok());
    Q4T_CHECK(!state.Permit(id, current, Access()).ok());
  }
  Q4T_CHECK(state.Permit(id, Definition(), Access()).code ==
            "mcp_approval_required");
  Q4T_CHECK(state.Inspect(id, Access()).value.GetString("state") == "ready");
  Q4T_CHECK(Ok(state.Permit(id, definition, Access())));
  const auto restricted =
      Definition("never", Value(R"({"allowed_callers":["programmatic"]})"));
  const auto private_catalog = Catalog(state, restricted);
  Q4T_CHECK(!Propose(state, private_catalog, definition).ok());
  Q4T_CHECK(Ok(Propose(state, private_catalog, restricted,
                       Value(R"({"path":"docs/MODEL.md"})"), "programmatic")));
  return true;
}

Q4T_TEST(responses_mcp_state_does_not_persist_credentials_even_encrypted) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesMcpState state(objects, Config());
  const auto definition = Definition("never");
  const auto catalog = Catalog(state, definition);
  const auto proposal = Propose(state, catalog, definition);
  Q4T_CHECK(Ok(proposal));
  StateCipher cipher(objects);
  for (const auto& [id, kind] :
       {std::pair(catalog.GetString("id"), "catalog"),
        std::pair(proposal.value.GetString("id"), "call")}) {
    const auto record = Record(objects, id);
    const auto* data = record.Find("data");
    Q4T_CHECK(data && data->GetString("sealed").starts_with("q4t1:"));
    std::string plain;
    const StateCipherBinding binding{
        Config().model,
        Digest(std::string(identity::kTemplateReference) + ':' +
               identity::kRenderer),
        "responses.mcp." + std::string(kind) + ".v1", id};
    Q4T_CHECK(
        Ok(cipher.Open(data->GetString("sealed"), binding, Access(), &plain)));
    Q4T_CHECK(plain.find("fixture-secret") == plain.npos &&
              plain.find("authorization") == plain.npos &&
              plain.find("X-Private") == plain.npos);
    const auto retention = InspectObjectRetention("tool_run", *data);
    Q4T_CHECK(!retention.uncertain && !retention.active &&
              !retention.collect_when_unreferenced &&
              retention.references.empty());
  }
  for (const auto& entry :
       std::filesystem::recursive_directory_iterator(f.options.directory)) {
    if (!entry.is_regular_file()) continue;
    std::ifstream file(entry.path(), std::ios::binary);
    const std::string bytes((std::istreambuf_iterator<char>(file)), {});
    Q4T_CHECK(bytes.find("fixture-secret") == bytes.npos);
  }
  return true;
}

Q4T_TEST(responses_mcp_state_unknown_dispatch_never_replays_after_restart) {
  Fixture f;
  std::string id;
  const auto definition = Definition("never");
  {
    ObjectStore objects(f.options);
    ResponsesMcpState state(objects, Config());
    const auto proposal =
        Propose(state, Catalog(state, definition), definition);
    Q4T_CHECK(Ok(proposal));
    id = proposal.value.GetString("id");
    Q4T_CHECK(Ok(state.Permit(id, definition, Access())));
  }
  ObjectStore objects(f.options);
  ResponsesMcpState state(objects, Config());
  Q4T_CHECK(state.Inspect(id, Access()).value.GetString("state") ==
            "dispatching");
  Q4T_CHECK(!state.Permit(id, definition, Access()).ok());
  McpTransportReceipt unknown;
  // This fixture destroyed the previous executor/store owner above. Recovery
  // can reconcile its private dispatch; an ordinary losing caller cannot.
  unknown.dispatch_id =
      state.Inspect(id, Access()).value.GetString("dispatch_id");
  unknown.outcome_unknown = true;
  const auto finished = state.Finish(
      id, unknown, Error(503, "mcp_worker_failed", "Worker unavailable"),
      Access());
  Q4T_CHECK(Ok(finished) && finished.value.GetString("status") == "failed");
  Q4T_CHECK(finished.value.Find("error")->Find("content")->GetString("code") ==
            "mcp_result_unknown");
  Q4T_CHECK(state.Inspect(id, Access()).value.GetString("state") == "unknown");
  Q4T_CHECK(!state.Permit(id, definition, Access()).ok());
  return true;
}

Q4T_TEST(responses_mcp_state_result_schema_errors_and_no_result_forgery) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesMcpState state(objects, Config());
  const auto definition = Definition("never");
  const auto catalog = Catalog(state, definition);
  for (int variant = 0; variant < 5; ++variant) {
    const auto proposal = Propose(state, catalog, definition);
    Q4T_CHECK(Ok(proposal));
    const auto id = proposal.value.GetString("id");
    auto receipt = Success();
    Q4T_CHECK(!state.Finish(id, receipt, {}, Access()).ok());
    const auto permitted = state.Permit(id, definition, Access());
    Q4T_CHECK(Ok(permitted));
    receipt.dispatch_id = permitted.value.GetString("dispatch_id");
    if (variant == 0) {
      Set(&receipt.result, "structuredContent", Value(R"({"text":12})"));
    } else if (variant == 1) {
      std::erase_if(receipt.result.object, [](const auto& field) {
        return field.first == "structuredContent";
      });
    } else if (variant == 2) {
      Set(&receipt.result, "isError", Boolean(true));
    } else if (variant == 3) {
      receipt.error = Value(R"({"code":-32603,"message":"Remote error"})");
    } else {
      Set(&receipt.result, "structuredContent",
          Object({{"text", String(std::string(2U << 20, 'x'))}}));
      Set(&receipt.result, "content",
          Array({Object({{"type", String("text")},
                         {"text", String(std::string(2U << 20, 'x'))}})}));
    }
    const auto finished = state.Finish(id, receipt, {}, Access());
    Q4T_CHECK(Ok(finished) && finished.value.GetString("status") == "failed" &&
              finished.value.Find("output")->IsNull());
    Q4T_CHECK(!state.Finish(id, Success(), {}, Access()).ok());
    Q4T_CHECK(!state.Permit(id, definition, Access()).ok());
  }
  return true;
}

Q4T_TEST(responses_mcp_state_completed_result_roundtrip_after_restart) {
  Fixture f;
  Json final;
  std::string id;
  const auto definition = Definition("never");
  {
    ObjectStore objects(f.options);
    ResponsesMcpState state(objects, Config());
    const auto proposal =
        Propose(state, Catalog(state, definition), definition);
    Q4T_CHECK(Ok(proposal));
    id = proposal.value.GetString("id");
    const auto permitted = state.Permit(id, definition, Access());
    Q4T_CHECK(Ok(permitted));
    const auto finished = state.Finish(
        id, Success(permitted.value.GetString("dispatch_id")), {}, Access());
    Q4T_CHECK(Ok(finished) &&
              finished.value.GetString("status") == "completed");
    final = finished.value;
  }
  ObjectStore objects(f.options);
  ResponsesMcpState state(objects, Config());
  const auto restored = state.Inspect(id, Access());
  Q4T_CHECK(Ok(restored) && restored.value.GetString("state") == "completed");
  Q4T_CHECK(OutputJson(*restored.value.Find("terminal")) == OutputJson(final));
  Q4T_CHECK(!state.Permit(id, definition, Access()).ok());
  return true;
}

Q4T_TEST(responses_mcp_state_cancel_expiry_and_retention_are_bounded) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesMcpState state(objects, Config());
  const auto definition = Definition("never");
  const auto catalog = Catalog(state, definition, 1);
  const auto proposal = Propose(state, catalog, definition);
  Q4T_CHECK(Ok(proposal));
  const auto id = proposal.value.GetString("id");
  auto cancelled = Access();
  cancelled.cancelled = [] { return true; };
  Q4T_CHECK(state.Permit(id, definition, cancelled).http_status == 499);
  auto expired = Access();
  expired.deadline = Clock::now() - 1ms;
  Q4T_CHECK(state.Permit(id, definition, expired).http_status == 408);
  const auto until = Record(objects, id).GetInt("expires_at");
  const auto deadline = Clock::now() + 2s;
  while (std::time(nullptr) < until && Clock::now() < deadline)
    std::this_thread::sleep_for(5ms);
  Q4T_CHECK(std::time(nullptr) >= until);
  // Ordinary reads already run automatic pruning. Inspect before those reads
  // to verify the explicit collector's original-expiry decision as well.
  auto planned = objects.Retention(0, 128, {}, Access().deadline);
  Q4T_CHECK(Ok(planned));
  const auto* candidates = planned.value.GetArray("candidates");
  Q4T_CHECK(candidates && candidates->array.size() == 2);
  auto collected = objects.Retention(
      0, 128, planned.value.GetString("plan_sha256"), Access().deadline);
  Q4T_CHECK(Ok(collected) && collected.value.GetInt("deleted_objects") == 2);
  Q4T_CHECK(!Propose(state, catalog, definition).ok());
  Q4T_CHECK(!state.Permit(id, definition, Access()).ok());
  return true;
}

Q4T_TEST(responses_mcp_history_approval_index_and_optional_call_fields) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesMcpState state(objects, Config());
  const auto catalog = Catalog(state);
  const auto proposal = Propose(state, catalog);
  Q4T_CHECK(Ok(proposal));
  const auto approval_id = proposal.value.GetString("id");
  Q4T_CHECK(Ok(state.Authenticate(proposal.value, Access())));
  auto decision = Object({{"type", String("mcp_approval_response")},
                          {"approval_request_id", String(approval_id)},
                          {"approve", Boolean(true)}});
  Q4T_CHECK(Ok(state.Authenticate(decision, Access())));
  Q4T_CHECK(state.Inspect(approval_id, Access()).value.GetString("state") ==
            "approval");
  auto allowed =
      state.Decide(approval_id, true, "read", Definition(), Access());
  Q4T_CHECK(Ok(allowed));
  const auto call_id = allowed.value.GetString("id");
  Q4T_CHECK(call_id.starts_with("mcp_") && call_id != approval_id);
  Q4T_CHECK(state.Inspect(call_id, Access()).value.GetString("state") ==
            "ready");
  const auto permitted = state.Permit(approval_id, Definition(), Access());
  Q4T_CHECK(Ok(permitted));
  const auto finished = state.Finish(
      call_id, Success(permitted.value.GetString("dispatch_id")), {}, Access());
  Q4T_CHECK(Ok(finished));
  auto omitted = finished.value;
  std::erase_if(omitted.object, [](const auto& field) {
    return field.first == "approval_request_id" || field.first == "status" ||
           field.first == "error";
  });
  const auto restored = state.Authenticate(omitted, Access());
  Q4T_CHECK(Ok(restored) &&
            OutputJson(restored.value) == OutputJson(finished.value));
  Q4T_CHECK(Ok(state.Authenticate(proposal.value, Access())));
  Set(&decision, "approve", Boolean(false));
  Q4T_CHECK(state.Authenticate(decision, Access()).http_status == 409);
  auto forged = omitted;
  Set(&forged, "output", String("fabricated result"));
  Q4T_CHECK(!state.Authenticate(forged, Access()).ok());
  Q4T_CHECK(!state.Authenticate(omitted, Access("client:foreign")).ok());
  const auto alias = Record(objects, approval_id);
  const auto call = Record(objects, call_id);
  Q4T_CHECK(alias.GetInt("expires_at") == call.GetInt("expires_at"));
  const auto retention =
      InspectObjectRetention("tool_run", *alias.Find("data"));
  Q4T_CHECK(!retention.uncertain && !retention.collect_when_unreferenced &&
            retention.references == std::vector<std::string>{call_id});
  return true;
}

Q4T_TEST(
    responses_mcp_history_catalog_restores_metadata_without_weakening_schema) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesMcpState state(objects, Config());
  const auto catalog = Catalog(state);
  auto minimal = catalog;
  auto& tool = Field(minimal, "tools").array[0];
  std::erase_if(tool.object, [](const auto& field) {
    return field.first == "description" || field.first == "annotations";
  });
  const auto restored = state.Authenticate(minimal, Access());
  Q4T_CHECK(Ok(restored) && OutputJson(restored.value) == OutputJson(catalog));
  auto opened = state.OpenCatalog(minimal, Definition(), Access());
  Q4T_CHECK(Ok(opened) &&
            opened.value.GetArray("tools")->array[0].Has("outputSchema"));
  Q4T_CHECK(
      !state
           .OpenCatalog(
               minimal,
               Definition(
                   "always",
                   Value(R"({"server_url":"https://other.example/mcp"})")),
               Access())
           .ok());
  Set(&Field(minimal, "tools").array[0], "input_schema", Object());
  Q4T_CHECK(!state.Authenticate(minimal, Access()).ok());
  return true;
}

Q4T_TEST(responses_mcp_history_stream_ids_and_failed_catalog_are_owned) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesMcpState state(objects, Config());
  const auto definition = Definition("never");
  const auto catalog =
      state.Catalog(definition, Tools(), Access(), 600, "mcpl_streamed");
  Q4T_CHECK(Ok(catalog) && catalog.value.GetString("id") == "mcpl_streamed");
  Q4T_CHECK(state.Catalog(definition, Tools(), Access(), 600, "mcpl_streamed")
                .http_status == 409);
  const auto args = Value(R"({"path":"docs/MODEL.md"})");
  const auto call = state.Propose(catalog.value, definition, "read", args,
                                  "direct", Access(), "mcp_streamed");
  Q4T_CHECK(Ok(call) && call.value.GetString("id") == "mcp_streamed");
  Q4T_CHECK(state
                .Propose(catalog.value, definition, "read", args, "direct",
                         Access(), "mcp_streamed")
                .http_status == 409);
  Q4T_CHECK(!state
                 .Propose(catalog.value, Definition(), "read", args, "direct",
                          Access(), "mcp_requires_approval")
                 .ok());
  Q4T_CHECK(
      !state.Catalog(definition, Tools(), Access(), 600, "mcpl_bad", "failed")
           .ok());
  const auto failed = state.Catalog(definition, Array(), Access(), 600,
                                    "mcpl_failed", "MCP target unavailable");
  Q4T_CHECK(Ok(failed) && Ok(state.Authenticate(failed.value, Access())));
  Q4T_CHECK(
      !state.Propose(failed.value, definition, "read", args, "direct", Access())
           .ok());
  return true;
}

Q4T_TEST(responses_mcp_history_approval_index_is_atomic_under_object_quota) {
  Fixture f;
  // Catalog occupies one; approval+call require two. Neither may be left by a
  // rejected atomic creation. The key ring has a separate internal scope.
  f.options.max_client_objects = 2;
  ObjectStore objects(f.options);
  ResponsesMcpState state(objects, Config());
  const auto catalog = Catalog(state);
  Q4T_CHECK(!Propose(state, catalog).ok());
  ObjectRequest list;
  list.operation = ObjectOperation::kList;
  list.kind = ObjectKind::kToolRun;
  list.scope = Access().scope;
  auto records = objects.Execute(list, Access().deadline);
  Q4T_CHECK(Ok(records));
  const auto* data = records.value.GetArray("data");
  Q4T_CHECK(data && data->array.size() == 1 &&
            data->array.front().GetString("id") == catalog.GetString("id"));
  Q4T_CHECK(Ok(state.Authenticate(catalog, Access())));
  return true;
}
