#include "q4t/server/responses_execution.h"

#include "object_protocol.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/input_snapshot_bindings.h"
#include "q4t/server/responses_owner.h"
#include "q4t/server/tool_result_contracts.h"

namespace q4t::server {
namespace {
using namespace object_detail;
ObjectReply Invalid() {
  return Error(503, "response_prepared_inputs_invalid",
               "Private queued response inputs are inconsistent");
}
bool Same(const Json& left, const Json& right) {
  std::string a, b;
  return InputValueIdentity(left, &a) && InputValueIdentity(right, &b) && a == b;
}
}  // namespace

ObjectReply ResponsesExecution::FreezeInputs(Json* inputs) {
  if (!inputs || !prepared_ || begun_ || commit_unknown_ || plan_.finished ||
      standalone_compaction_)
    return Invalid();
  if (access_.cancelled && access_.cancelled())
    return Error(499, "request_cancelled", "Input freezing cancelled");
  if (std::chrono::steady_clock::now() >= access_.deadline)
    return Error(408, "request_deadline_exceeded", "Input freezing expired");
  // Queue inputs must survive even with public store=false. No model step,
  // Response record or event is published here; pending sheets stay pending.
  if (files_) {
    auto reply = files_->Persist(
        {access_.scope, access_.deadline, access_.cancelled});
    if (!reply.ok()) {
      if (reply.commit_unknown) {
        commit_unknown_ = true;
        files_->RetainUncertainHolds();
      }
      return reply;
    }
    plan_.input_snapshots = files_->Bindings();
  }
  *inputs =
      Object({{"format", String("q4t.responses_prepared.v1")},
              {"id", String(plan_.id)},
              {"scope", String(plan_.scope)},
              {"store", Boolean(plan_.store)},
              {"ephemeral", Boolean(plan_.ephemeral)},
              {"history", plan_.history},
              {"own_input", plan_.own_input},
              {"input_snapshots", plan_.input_snapshots},
              {"tool_contracts", plan_.tool_contracts},
              {"request", plan_.request},
              {"options", envelope_.options},
              {"conversation_id", String(plan_.conversation_id)},
              {"conversation_revision", Number(plan_.conversation_revision)},
              {"conversation", plan_.conversation}});
  return {};
}

void ResponsesExecution::InputsPublished(bool uncertain) noexcept {
  if (!files_) return;
  if (uncertain) {
    files_->RetainUncertainHolds();
    return;
  }
  try {
    files_->Release(
        {access_.scope,
         std::chrono::steady_clock::now() + std::chrono::milliseconds(250),
         {}});
  } catch (...) {
    // Parent publication has already retained the immutable dependencies.
  }
}

ObjectReply ResponsesExecution::RestoreInputs(
    const Json& inputs, const ChatPlanConfig& config, StateAccess access,
    int64_t created, const BatchResponseLink* batch_parent) {
  if (prepared_ || begun_ || standalone_compaction_ || created <= 0 ||
      inputs.GetString("format") != "q4t.responses_prepared.v1" ||
      inputs.GetString("scope") != access.scope || access.scope.empty())
    return Invalid();
  const auto* request = inputs.Find("request");
  const auto* options = inputs.Find("options");
  const auto* history = inputs.GetArray("history");
  const auto* own = inputs.GetArray("own_input");
  const auto* store = inputs.Find("store");
  const auto* ephemeral = inputs.Find("ephemeral");
  const auto* bindings = inputs.Find("input_snapshots");
  const auto* revision = inputs.Find("conversation_revision");
  const auto* conversation = inputs.Find("conversation");
  const auto id = inputs.GetString("id");
  if (!request || !request->IsObject() || !options || !options->IsObject() ||
      !history || !own || history->array.size() > 4096 ||
      own->array.size() > history->array.size() || !store || !store->IsBool() ||
      !ephemeral || !ephemeral->IsBool() || !bindings ||
      !revision || !IsInputInteger(*revision) || revision->AsInt() < 0 ||
      !conversation || !id.starts_with("resp_") || !ValidId(id))
    return Invalid();
  access_ = std::move(access);
  if (access_.cancelled && access_.cancelled())
    return Error(499, "request_cancelled", "Queued input restoration cancelled");
  if (std::chrono::steady_clock::now() >= access_.deadline)
    return Error(408, "request_deadline_exceeded", "Queued inputs expired");
  ChatPlanError error;
  auto status = ParseResponsesEnvelope(*request, config, &envelope_, &error);
  if (!status) {
    auto reply = Error(error.http_status, error.code, status.message());
    reply.value = Object({{"param", String(error.param)}});
    return reply;
  }
  if (store->boolean != (envelope_.store || envelope_.background) ||
      ephemeral->boolean != (envelope_.background && !envelope_.store) ||
      inputs.GetString("conversation_id") != envelope_.conversation_id)
    return Invalid();
  const auto prefix = history->array.size() - own->array.size();
  for (size_t i = 0; i < own->array.size(); ++i)
    if (!Same(own->array[i], history->array[prefix + i])) return Invalid();
  if (envelope_.conversation_id.empty()) {
    if (revision->AsInt() != 0 || !conversation->IsNull()) return Invalid();
  } else {
    const auto* items = conversation->GetArray("items");
    if (revision->AsInt() <= 0 || !items || items->array.size() != prefix)
      return Invalid();
    for (size_t i = 0; i < prefix; ++i)
      if (!Same(items->array[i], history->array[i])) return Invalid();
  }
  if (batch_parent) {
    bool cancelled = false;
    if (!envelope_.background || batch_parent->response_id != id)
      return Invalid();
    auto parent =
        CheckBatchResponseParent(objects_, *batch_parent, access_, &cancelled);
    if (!parent.ok()) return parent;
    if (cancelled)
      return Error(499, "batch_cancelled",
                   "Original Batch no longer accepts work");
    plan_.batch_parent = *batch_parent;
  }
  plan_.id = id;
  plan_.scope = access_.scope;
  plan_.store = store->boolean;
  plan_.ephemeral = ephemeral->boolean;
  plan_.request = *request;
  plan_.history = *history;
  plan_.own_input = *own;
  plan_.conversation_id = envelope_.conversation_id;
  plan_.conversation_revision = revision->AsInt();
  plan_.conversation = *conversation;
  auto reply =
      MergeInputSnapshots(*bindings, plan_.history, &plan_.input_snapshots);
  if (!reply.ok()) return reply;
  if (plan_.store) plan_.execution_owner = CurrentResponseOwner();
  if (const auto* contracts = inputs.Find("tool_contracts")) {
    reply = MergeToolResultContracts(*contracts, plan_.history,
                                     &plan_.tool_contracts);
    if (!reply.ok()) return reply;
  }
  reply = BindToolResultContracts(object_detail::Object(), plan_.history,
                                  &plan_.tool_contracts);
  if (!reply.ok()) return reply;
  require_frozen_inputs_ = true;
  // Never Resolve, Expand a prompt, fetch a URL or publish a queued Response.
  // FileExpansion may only read the immutable snapshots saved at validation.
  reply = PlanGeneration(config, created);
  if (!reply.ok()) return reply;
  if (!Same(*options, envelope_.options)) {
    prepared_ = false;
    return Error(409, "response_prepared_policy_changed",
                 "Queued response treatment changed since validation");
  }
  return {};
}
}  // namespace q4t::server
