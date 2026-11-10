#include "q4t/server/batch_preparation.h"

#include <ctime>
#include <optional>

#include "object_protocol.h"
#include "q4t/server/batch_checkpoint.h"
#include "q4t/server/chat_file_inputs.h"
#include "q4t/server/completion_plan.h"
#include "q4t/server/input_snapshot_bindings.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/responses_execution.h"

namespace q4t::server {
namespace {
using namespace object_detail;
constexpr InputSnapshotLimits kLimits{kBackgroundInputBytes, 16384, 1};
ObjectReply Invalid() {
  return Error(503, "batch_prepared_input_invalid",
               "Private Batch input does not match its original request");
}
ObjectReply Identity(const std::string& id, std::string_view endpoint,
                      const BatchInputRow& row, Json* metadata,
                      std::string* binding) {
  if (!id.starts_with("batch_") || !ValidId(id) ||
      !ValidBatchEndpoint(endpoint) || !row.line ||
      row.line > kBatchRequests || !row.body.IsObject())
    return Invalid();
  std::string body;
  const auto status = InputValueIdentity(row.body, &body);
  if (!status)
    return Error(413, "batch_prepared_input_capacity", status.message());
  *metadata = Object({{"format", String("q4t.batch_prepared.v1")},
                      {"batch_id", String(id)},
                      {"line", Number(row.line)},
                      {"custom_id_sha256", String(Digest(row.custom_id))},
                      {"endpoint", String(std::string(endpoint))},
                      {"body_sha256", String(body)}});
  *binding = Digest(OutputJson(*metadata));
  return {};
}
ObjectReply PlanningError(const Status& status, const ChatPlanError& error) {
  auto reply = Error(error.http_status, error.code, status.message());
  reply.value = Object({{"param", String(error.param)}});
  return reply;
}
}  // namespace

ObjectReply BatchPreparation::Prepare(const BinaryAccess& access,
                                      const std::string& batch_id,
                                      std::string_view endpoint,
                                      const BatchInputRow& row,
                                      const ChatPlanConfig& config,
                                      BatchPreparedInput* output,
                                      const BatchInputMeter& meter) {
  if (!output || !output->reference.id.empty() || !output->hold.id.empty() ||
      !output->inline_input.IsNull())
    return Invalid();
  Json metadata;
  auto reply =
      Identity(batch_id, endpoint, row, &metadata, &output->binding_sha256);
  if (!reply.ok()) return reply;
  if (row.body.GetBool("stream"))
    return Error(400, "batch_stream_unsupported", "Batch requires finite JSON");
  Json prepared, dependencies = Array();
  ChatFileInputs chat(&objects_);
  std::optional<ResponsesExecution> responses;
  bool uncertain = false;
  ScopeExit close([&] {
    if (responses) responses->InputsPublished(uncertain);
    chat.Published(access, uncertain);
  });
  if (endpoint == "/v1/chat/completions") {
    ChatRequestPlan plan;
    reply = chat.Prepare(row.body, config, access, &plan);
    if (reply.ok()) reply = chat.Freeze(access, &dependencies);
    prepared = Object({{"kind", String("chat")},
                        {"input_snapshots", dependencies}});
  } else if (endpoint == "/v1/responses") {
    responses.emplace(objects_);
    reply = responses->Prepare(
        row.body, config, {access.scope, access.deadline, access.cancelled},
        std::time(nullptr), true);
    if (reply.ok()) reply = responses->FreezeInputs(&prepared);
    if (reply.ok()) dependencies = *prepared.Find("input_snapshots");
  } else if (endpoint == "/v1/completions") {
    CompletionRequestPlan plan;
    ChatPlanError error;
    auto status = PlanCompletionRequest(row.body, config, &plan, &error);
    if (!status) reply = PlanningError(status, error);
    prepared = Object({{"kind", String("completion")}});
  } else {
    // The remaining native model families are tracked by A10. This private
    // adapter cannot validate them by pretending they are Chat requests.
    return Error(503, "batch_endpoint_executor_unavailable",
                 "Batch endpoint requires its native executor");
  }
  if (!reply.ok()) {
    uncertain = reply.commit_unknown;
    return reply;
  }
  const auto text = OutputJson(prepared);
  if (meter) {
    reply =
        meter(endpoint, row, prepared, config, access, &output->input_budget);
    if (!reply.ok()) {
      uncertain = reply.commit_unknown;
      return reply;
    }
    BatchInputBudget checked;
    if (!ParseBatchInputBudget(BatchInputBudgetJson(output->input_budget),
                               &checked))
      return Error(503, "batch_budget_invalid",
                   "Native Batch input meter returned an invalid budget");
  }
  if (text.size() <= kBatchInlineBytes && dependencies.array.empty()) {
    output->inline_input =
        Object({{"format", String("q4t.batch_inline_input.v1")},
                {"binding_sha256", String(output->binding_sha256)},
                {"scope_sha256", String(Digest(access.scope))},
                {"sha256", String(Digest(text))},
                {"payload", String(text)}});
    output->canonical_model = config.model;
    return {};
  }
  const std::string_view part(text);
  reply = InputSnapshotStore(objects_).Save(
      access, output->binding_sha256, metadata, std::span(&part, 1), kLimits,
      3600, &output->reference, &output->hold, dependencies);
  uncertain = reply.commit_unknown;
  if (reply.ok()) output->canonical_model = config.model;
  return reply;
}

ObjectReply BatchPreparation::Read(
    const BinaryAccess& access, const std::string& batch_id,
    std::string_view endpoint, const BatchInputRow& row,
    const Json& checkpoint, Json* prepared) {
  if (!prepared) return Invalid();
  if (access.scope.empty()) return Invalid();
  if (access.cancelled && access.cancelled())
    return Error(499, "batch_cancelled", "Batch input read cancelled");
  if (std::chrono::steady_clock::now() >= access.deadline)
    return Error(408, "request_deadline_exceeded",
                 "Batch input read exceeded its deadline");
  Json metadata;
  std::string binding;
  auto reply = Identity(batch_id, endpoint, row, &metadata, &binding);
  if (!reply.ok()) return reply;
  std::string text;
  if (const auto* embedded = checkpoint.Find("prepared_inline")) {
    if (checkpoint.Has("input_snapshots") ||
        !ValidBatchInlineInput(*embedded) ||
        embedded->GetString("scope_sha256") != Digest(access.scope) ||
        embedded->GetString("binding_sha256") != binding)
      return Invalid();
    text = embedded->GetString("payload");
  } else {
    const auto* references = checkpoint.GetArray("input_snapshots");
    if (!references || references->array.size() != 1) return Invalid();
    const auto& entry = references->array[0];
    const auto* snapshot = entry.Find("snapshot");
    InputSnapshotRef reference;
    if (!snapshot || !ParseInputSnapshotReference(*snapshot, &reference) ||
        entry.GetString("binding_sha256") != binding)
      return Invalid();
    InputSnapshot restored;
    reply = InputSnapshotStore(objects_).Read(access, reference, binding,
                                              kLimits, &restored);
    if (!reply.ok()) return reply;
    if (OutputJson(restored.metadata) != OutputJson(metadata) ||
        restored.parts.size() != 1)
      return Invalid();
    text = std::move(restored.parts[0]);
  }
  io::JsonParseLimits limits;
  limits.max_depth = 96;
  limits.max_values = 2 * 1024 * 1024;
  limits.max_string_bytes = kBackgroundInputBytes;
  limits.reject_duplicate_keys = true;
  limits.allow_exact_number_text = true;
  Json candidate;
  if (!io::ParseJson(text, &candidate, true, limits) || !candidate.IsObject())
    return Invalid();
  if ((endpoint == "/v1/responses" &&
       candidate.GetString("format") != "q4t.responses_prepared.v1") ||
      (endpoint == "/v1/chat/completions" &&
       candidate.GetString("kind") != "chat") ||
      (endpoint == "/v1/completions" &&
       candidate.GetString("kind") != "completion"))
    return Invalid();
  *prepared = std::move(candidate);
  return {};
}

void BatchPreparation::Published(const BinaryAccess& access,
                                 BatchPreparedInput* input,
                                 bool uncertain) noexcept {
  if (!input) return;
  if (uncertain) input->hold.commit_unknown = true;
  if (input->hold.id.empty() || input->hold.commit_unknown) return;
  try {
    const auto reply = InputSnapshotStore(objects_).Release(
        {access.scope,
         std::chrono::steady_clock::now() + std::chrono::milliseconds(250),
         {}},
        &input->hold);
    if (!reply.ok()) objects_.RecordAuxiliaryCleanupFailure();
  } catch (...) {
    objects_.RecordAuxiliaryCleanupFailure();
  }
}
}  // namespace q4t::server
