#include "q4t/server/responses_execution.h"
#include "q4t/server/tool_result_contracts.h"

#include <algorithm>
#include <cstring>
#include <ctime>

#include "object_protocol.h"
#include "q4t/server/http_response.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/request_json.h"
#include "q4t/server/responses_owner.h"
#include "q4t/server/responses_crypto.h"

namespace q4t::server {
void ResponsesExecution::BindBackgroundControl(
    std::shared_ptr<RequestControl> control) {
  background_control_ = std::move(control);
  BindCancellation(
      background_control_->deadline(),
      [control = background_control_] { return control->Cancelled(); });
  if (!plan_.execution_deadline_at) {
    const auto remaining = std::chrono::ceil<std::chrono::seconds>(
        access_.deadline - std::chrono::steady_clock::now());
    plan_.execution_deadline_at =
        std::time(nullptr) + std::max<int64_t>(0, remaining.count());
  }
}

namespace {
using namespace object_detail;
ObjectReply OutputError(const Status& status) {
  return Error(503, "response_storage_exhausted", status.message());
}
}  // namespace

ObjectReply ResponsesExecution::BindMutationResult(ObjectReply reply) {
  if (reply.commit_unknown) {
    commit_unknown_ = true;
    if (files_) files_->RetainUncertainHolds();
    reply.message +=
        "; retrieve response " + plan_.id + " before retrying this request";
    reply.value = Object({{"response_id", String(plan_.id)}});
  }
  return reply;
}

size_t ResponsesExecution::RetainedInputBytes() const {
  size_t bytes = 0;
  for (const auto* value : {&plan_.request,
                            &plan_.history,
                            &plan_.own_input,
                            &plan_.input_snapshots,
                            &plan_.tool_contracts,
                            &plan_.conversation,
                            &plan_.execution_owner,
                            &envelope_.request,
                            &envelope_.options,
                            &generation_.generation_request,
                            &generation_.effective_tools,
                            &generation_.active_tools,
                            &tool_search_catalog_.definitions,
                            &tool_search_catalog_.deferred,
                            &tool_search_catalog_.eager,
                            &tool_search_catalog_.search,
                            &tool_search_catalog_.summaries,
                            &generation_.input.items,
                            &generation_.input.messages,
                            &generation_history_,
                            &compaction_.source,
                            &compaction_.retained,
                            &compaction_.request,
                            &compaction_window_,
                            &output_.Snapshot()})
    bytes += OutputJson(*value).size();
  if (generation_.chat.normalized_request)
    bytes += OutputJson(*generation_.chat.normalized_request).size();
  if (generation_.chat.header_tools)
    bytes += OutputJson(*generation_.chat.header_tools).size();
  for (const auto& format : generation_.chat.tool_call_formats)
    bytes += sizeof(format) + format.call_id.size();
  for (const auto& format : generation_.input.tool_call_formats)
    bytes += sizeof(format) + format.call_id.size();
  if (compaction_.chat.normalized_request)
    bytes += OutputJson(*compaction_.chat.normalized_request).size();
  for (const auto& path : generation_.input.message_paths) bytes += path.size();
  bytes += generation_.input.cache_boundaries.size() *
           sizeof(PromptCacheInputBoundary);
  bytes += generation_.input_source_indices.size() * sizeof(size_t);
  bytes += generation_.chat.constraints.structural_tag.size();
  if (files_) bytes += files_->RetainedBytes();
  if (!file_history_.IsNull()) bytes += OutputJson(file_history_).size();
  bytes += file_search_argument_bytes_;
  bytes += tool_search_argument_bytes_;
  for (const auto& binding : file_search_bindings_) {
    bytes += binding.model_name.size() +
             OutputJson(binding.tool.search.filters).size();
    for (const auto& id : binding.tool.store_ids) bytes += id.size();
  }
  return bytes;
}

ObjectReply ResponsesExecution::Prepare(const Json& request,
                                        const ChatPlanConfig& config,
                                        StateAccess access, int64_t created,
                                        bool background_executor) {
  if (prepared_ || begun_)
    return Error(500, "response_state_invalid", "Response already prepared");
  access_ = std::move(access);
  ChatPlanError error;
  Json compact_request;
  Json expanded_request;
  const Json* effective = &request;
  Status status;
  if (standalone_compaction_) {
    status = NormalizeResponsesCompactRequest(request, config, &compact_request,
                                              &error);
    effective = &compact_request;
  } else {
    // Validate the original envelope before any prompt lookup/expansion. The
    // expanded request is checked again because template defaults can change
    // the effective generation settings and client resource requirements.
    status = ParseResponsesEnvelope(request, config, &envelope_, &error);
    if (status && Nonnull(request, "prompt")) {
      auto expanded = prompts_.Expand(request, config, access_);
      if (!expanded.ok()) return expanded;
      expanded_request = std::move(expanded.value);
      effective = &expanded_request;
    }
  }
  if (status && (standalone_compaction_ || effective != &request))
    status = ParseResponsesEnvelope(*effective, config, &envelope_, &error);
  if (!status) {
    auto reply = Error(error.http_status, error.code, status.message());
    reply.value = Object({{"param", String(error.param)}});
    return reply;
  }
  auto required = RequiredExecutors(background_executor);
  if (!required.ok()) return required;
  auto resolved = state_.Resolve(envelope_, access_, &plan_);
  if (!resolved.ok()) return resolved;
  return PlanGeneration(config, created);
}

ObjectReply ResponsesExecution::RequiredExecutors(
    bool background_executor) const {
  // Required application executors remain explicit work in A07--A11. Until
  // attached, do not silently run a different foreground/uncompressed task.
  std::string required;
  if (envelope_.background && !background_executor) required = "background";
  if (!required.empty()) {
    auto reply =
        Error(503, "response_executor_unavailable",
              "Required response executor is unavailable: " + required);
    reply.value = Object({{"param", String(required)}});
    return reply;
  }
  return {};
}

ObjectReply ResponsesExecution::FitContext(const ChatPlanConfig& config,
                                           const ResponsesContextCheck& check) {
  const auto* output = output_.Snapshot().GetArray("output");
  if (!prepared_ || !generation_ready_ || plan_.finished || !output ||
      !output->array.empty())
    return Error(500, "response_state_invalid",
                 "Context must be selected before model output");
  if (envelope_.truncate && !check)
    return Error(503, "response_executor_unavailable",
                 "Context measurement is unavailable");
  ChatPlanError error;
  const auto cancelled = [&](ChatPlanError* failure) -> Status {
    if (std::chrono::steady_clock::now() >= access_.deadline) {
      *failure = {"request_deadline_exceeded", "input", 408};
      return Status::Fail("Context preparation exceeded request deadline");
    }
    if (access_.cancelled && access_.cancelled()) {
      *failure = {"request_cancelled", "input", 499};
      return Status::Fail("Context preparation cancelled");
    }
    return {};
  };
  auto status = FitResponsesContext(
      envelope_, config,
      [&](const ResponsesGenerationPlan& candidate, ChatPlanError* failure) {
        auto active = cancelled(failure);
        if (!active) return active;
        const auto measured = check(candidate, failure);
        active = cancelled(failure);
        return active ? measured : active;
      },
      &generation_, &error);
  if (status) return {};
  ResponsesInputErrorParam(generation_.input, &error.param);
  if (error.param == "max_completion_tokens") error.param = "max_output_tokens";
  auto reply = Error(error.http_status, error.code, status.message());
  reply.value = Object({{"param", String(error.param)}});
  return reply;
}

ObjectReply ResponsesExecution::PlanGeneration(const ChatPlanConfig& config,
                                               int64_t created) {
  auto history = plan_.history;
  compaction_trigger_ =
      !plan_.own_input.array.empty() &&
      plan_.own_input.array.back().GetString("type") == "compaction_trigger";
  // Old triggers remain in original input records but do not recur as future
  // model messages or contaminate an authenticated compaction prefix.
  std::erase_if(history.array, [](const auto& item) {
    return item.GetString("type") == "compaction_trigger";
  });
  const bool encrypted_input = std::any_of(
      history.array.begin(), history.array.end(), [](const auto& item) {
        return (item.GetString("type") == "reasoning" ||
                item.GetString("type") == "compaction") &&
               Nonnull(item, "encrypted_content");
      });
  const bool encrypted_state = envelope_.encrypted_reasoning || encrypted_input;
  if (encrypted_state) {
    auto opened = OpenResponsesReasoning(cipher_, history, config, access_,
                                         &generation_history_);
    if (!opened.ok()) return opened;
  } else {
    generation_history_ = std::move(history);
  }
  ChatPlanError error;
  Json effective_tools;
  auto status = PlanResponsesFileSearchBindings(envelope_.request,
                                                &file_search_bindings_, &error);
  if (status)
    status = ValidateResponsesPreparation(envelope_, generation_history_,
                                          config, &effective_tools, &error);
  if (!status) {
    auto reply = Error(error.http_status, error.code, status.message());
    reply.value = Object({{"param", String(error.param)}});
    return reply;
  }
  const auto* declarations = envelope_.request.Find("tools");
  ResponsesToolHistory tools;
  if (HasResponsesToolDiscovery(declarations ? *declarations : Json(),
                                generation_history_)) {
    status = ResolveResponsesToolHistory(declarations ? *declarations : Array(),
                                         generation_history_, &tools, &error);
    if (!status) return Error(error.http_status, error.code, status.message());
    tool_search_catalog_ = std::move(tools.initial);
  } else {
    tools.active = effective_tools;
  }
  status = BindActiveTools(tools.active, &error);
  if (!status) return Error(error.http_status, error.code, status.message());
  auto result_contracts = plan_.tool_contracts;
  if (encrypted_input) {
    // Opening an authenticated compaction may expand consumed historical
    // calls. Their results were checked before sealing, and cannot acquire
    // today's tool definitions. Newly supplied calls retain bound schemas.
    auto bound = BindToolResultContracts(Object(), generation_history_,
                                         &result_contracts);
    if (!bound.ok()) return bound;
  }
  auto result_contract = CheckToolResultContracts(
      envelope_.request, generation_history_, result_contracts, access_);
  if (!result_contract.ok()) return result_contract;
  if (HasFileInputs(generation_history_)) {
    files_ = std::make_unique<FileExpansion>(
        &objects_,
        config.file_inputs ? *config.file_inputs : FileExpansionOptions{});
    file_history_ = std::move(generation_history_);
    auto prepared =
        files_->Prepare(file_history_, FileInputProfile::kResponsesStored,
                        plan_.input_snapshots,
                        {access_.scope, access_.deadline, access_.cancelled},
                        require_frozen_inputs_);
    if (!prepared.ok()) return prepared;
  }
  if (!PendingFileSheets()) {
    auto built = BuildPreparedGeneration(config);
    if (!built.ok()) return built;
  }
  if (envelope_.encrypted_reasoning) {
    auto ready = cipher_.Ready(access_);
    if (!ready.ok()) return ready;
  }
  cipher_config_.model = config.model;
  Set(&envelope_.options, "tools", std::move(effective_tools));
  // Keep the public Responses shape, not Chat's nested function/custom shape.
  // NormalizeResponsesTools has already validated the selection and defaults.
  const auto* choice = envelope_.request.Find("tool_choice");
  Set(&envelope_.options, "tool_choice", choice ? *choice : String("auto"));
  // The local scheduler reports the tier actually used.
  Set(&envelope_.options, "service_tier", String("default"));
  status =
      output_.Init(envelope_.options, plan_.id, created, envelope_.obfuscation);
  if (!status) return OutputError(status);
  status = output_.BindTools(tools.active);
  if (!status) return OutputError(status);
  events_ = Array();
  prepared_ = true;
  return {};
}

ObjectReply ResponsesExecution::RestoreQueued(const std::string& id,
                                              const ChatPlanConfig& config,
                                              StateAccess access) {
  if (prepared_ || begun_)
    return Error(500, "response_state_invalid", "Response already prepared");
  access_ = std::move(access);
  auto restored = state_.LoadPlan(id, access_, &plan_, true);
  if (!restored.ok()) return restored;
  const auto& snapshot = Field(restored.value, "response");
  if (snapshot.GetString("status") != "queued" ||
      !snapshot.GetBool("background"))
    return Error(409, "response_not_queued",
                 "Only unexecuted queued work can resume");
  ChatPlanError error;
  auto status =
      ParseResponsesEnvelope(plan_.request, config, &envelope_, &error);
  if (!status) return Error(error.http_status, error.code, status.message());
  // Queued work may resume only with the original actual treatment. A new
  // grant/default/policy must not silently alter previously accepted work.
  for (const auto* key : {"access_programs", "q4t_access_program"}) {
    const auto* prior = snapshot.Find(key);
    const auto* current = envelope_.options.Find(key);
    if (OutputJson(prior ? *prior : Json()) !=
        OutputJson(current ? *current : Json()))
      return Error(409, "response_access_program_changed",
                   "Queued response access program policy changed");
  }
  require_frozen_inputs_ = true;
  auto result = RequiredExecutors(true);
  if (result.ok())
    result = PlanGeneration(config, snapshot.GetInt("created_at"));
  if (!result.ok()) return result;
  const auto& saved_events = Field(restored.value, "events").array;
  if (saved_events.size() != 2 ||
      saved_events[0].GetString("type") != "response.created" ||
      saved_events[1].GetString("type") != "response.queued")
    return Error(503, "response_state_invalid",
                 "Queued events are inconsistent");
  for (const auto& event : saved_events) {
    const auto* resource = event.Find("response");
    if (!resource || resource->GetString("id") != id ||
        resource->GetString("status") != "queued")
      return Error(503, "response_state_invalid",
                   "Queued event identity changed");
    status = Capture("event: " + event.GetString("type") +
                         "\r\ndata: " + OutputJson(event) + "\r\n\r\n",
                     &events_, &event_bytes_);
    if (!status) return OutputError(status);
  }
  status = output_.RestoreQueued(snapshot, events_.array.size());
  if (!status) return OutputError(status);
  plan_.execution_owner = CurrentResponseOwner();
  checkpoint_bytes_ = event_bytes_;
  checkpoint_time_ = std::chrono::steady_clock::now();
  begun_ = true;
  return {};
}

Status ResponsesExecution::Capture(std::string_view wire, Json* events,
                                   size_t* bytes) const {
  if (!plan_.store || wire.empty()) return {};
  // Retain actual event objects, never reconstructed snapshots. Both the
  // journal and the final object are additionally bounded by store quotas.
  constexpr size_t limit = 16 * 1024 * 1024;
  if (wire.size() > limit || *bytes > limit - wire.size())
    return Status::Fail("Responses event journal capacity exhausted");
  const size_t appended_bytes = wire.size();
  Json added = Array();
  while (!wire.empty()) {
    const auto line = wire.find("\r\n");
    const auto end = wire.find("\r\n\r\n");
    if (!wire.starts_with("event: ") || line == wire.npos || end == wire.npos ||
        end <= line + 8 || wire.substr(line + 2, 6) != "data: ")
      return Status::Fail("Invalid native Responses event framing");
    const auto type = wire.substr(7, line - 7);
    Json value;
    auto status =
        io::ParseJson(std::string(wire.substr(line + 8, end - line - 8)),
                      &value, true, kSchemaJsonLimits);
    if (!status || value.GetString("type") != type ||
        value.GetInt("sequence_number", -1) !=
            static_cast<int64_t>(events->array.size() + added.array.size()))
      return Status::Fail("Invalid native Responses event sequence");
    added.array.push_back(std::move(value));
    wire.remove_prefix(end + 4);
  }
  events->array.insert(events->array.end(),
                       std::make_move_iterator(added.array.begin()),
                       std::make_move_iterator(added.array.end()));
  *bytes += appended_bytes;
  return {};
}

ObjectReply ResponsesExecution::Begin(std::string* wire) {
  wire->clear();
  if (!prepared_ || begun_ || commit_unknown_)
    return Error(500, "response_state_invalid", "Invalid response begin");
  auto persisted = PersistPreparedInputs();
  if (!persisted.ok()) return persisted;
  auto candidate = output_;
  auto events = events_;
  size_t bytes = event_bytes_;
  std::string start;
  auto status = candidate.Start(&start);
  if (status) status = Capture(start, &events, &bytes);
  if (!status) return OutputError(status);
  if (plan_.store && require_cleanup_ && !cleanup_)
    return Error(503, "response_reconciliation_unavailable",
                 "Response lifecycle owner unavailable");
  if (plan_.store && cleanup_ && !cleanup_ticket_) {
    const auto reserved =
        cleanup_->Reserve(plan_.scope, plan_.id, &cleanup_ticket_);
    if (!reserved.ok()) return reserved;
  }
  // An exception after submitting the mutation is also uncertain. A missing
  // begun_ flag alone cannot prove that no durable record was written.
  durable_begin_attempted_ = plan_.store;
  auto reply = BindMutationResult(
      state_.Begin(candidate.Snapshot(), events, access_, &plan_));
  if (!reply.ok()) {
    if (reply.commit_unknown && files_) files_->RetainUncertainHolds();
    if (!reply.commit_unknown) durable_begin_attempted_ = false;
    return reply;
  }
  output_ = std::move(candidate);
  events_ = std::move(events);
  event_bytes_ = checkpoint_bytes_ = bytes;
  checkpoint_time_ = std::chrono::steady_clock::now();
  begun_ = true;
  ReleasePreparedInputs();
  *wire = std::move(start);
  return {};
}

ObjectReply ResponsesExecution::Checkpoint() {
  if (!begun_ || plan_.finished || commit_unknown_)
    return Error(500, "response_state_invalid", "Invalid response checkpoint");
  if (moderation_safe_) return {};  // Private draft is not a public checkpoint.
  auto reply = BindMutationResult(
      state_.Checkpoint(output_.Snapshot(), events_, access_, &plan_));
  if (reply.ok()) {
    checkpoint_time_ = std::chrono::steady_clock::now();
    checkpoint_bytes_ = event_bytes_;
  }
  return reply;
}

ObjectReply ResponsesExecution::CheckBatchParent(bool* cancelled) {
  if (!cancelled)
    return Error(500, "batch_response_binding_invalid",
                 "Missing parent output");
  *cancelled = false;
  if (!plan_.batch_parent) return {};
  return CheckBatchResponseParent(objects_, *plan_.batch_parent, access_,
                                  cancelled);
}

ObjectReply ResponsesExecution::StartRunning(std::string* wire) {
  wire->clear();
  if (!begun_ || plan_.finished || commit_unknown_)
    return Error(500, "response_state_invalid", "Invalid response start");
  auto candidate = output_;
  auto events = events_;
  size_t bytes = event_bytes_;
  std::string start;
  auto status = candidate.StartRunning(&start);
  if (status) status = Capture(start, &events, &bytes);
  if (!status) return OutputError(status);
  auto result = BindMutationResult(
      state_.Checkpoint(candidate.Snapshot(), events, access_, &plan_));
  if (!result.ok()) return result;
  output_ = std::move(candidate);
  events_ = std::move(events);
  event_bytes_ = checkpoint_bytes_ = bytes;
  checkpoint_time_ = std::chrono::steady_clock::now();
  *wire = std::move(start);
  return {};
}

ObjectReply ResponsesExecution::Cancel() {
  if (plan_.finished) return {};
  if (!begun_ || commit_unknown_)
    return Error(409, "response_state_invalid", "Response cannot be cancelled");
  DiscardUnreviewedOutput();
  auto candidate = output_;
  auto status = ApplyActualUsage(&candidate);
  if (status) status = candidate.Cancel();
  if (!status) return OutputError(status);
  StateAccess cleanup{
      access_.scope,
      std::chrono::steady_clock::now() + std::chrono::milliseconds(2000),
      {}};
  auto result = BindMutationResult(
      state_.Finish(candidate.Snapshot(), events_, cleanup, &plan_));
  if (!result.commit_unknown && (result.code == "object_too_large" ||
                                 result.code == "object_quota_exceeded"))
    result = BindMutationResult(
        state_.FailWithinReservation(candidate.Snapshot(), cleanup, &plan_));
  if (result.ok()) output_ = std::move(candidate);
  return result;
}

Status ResponsesExecution::Append(const ResponseDelta& delta,
                                  std::string_view probabilities,
                                  std::string* wire) {
  if (!begun_ || plan_.finished || commit_unknown_) {
    wire->clear();
    return Status::Fail("Response cannot accept execution output");
  }
  bool handled = false;
  auto status = AppendFileSearch(delta, &handled, wire);
  if (!status || handled) return status;
  status = AppendToolSearch(delta, &handled, wire);
  if (!status || handled) return status;
  return RecordOutput(output_.Append(delta, probabilities, wire), wire);
}

Status ResponsesExecution::CiteFileSearchSources(std::string* wire) {
  if (!begun_ || plan_.finished || commit_unknown_) {
    wire->clear();
    return Status::Fail("Response cannot accept file citations");
  }
  return RecordOutput(output_.CiteFileSearchSources(wire), wire);
}

Status ResponsesExecution::AppendSummary(std::string_view text,
                                         std::string* wire) {
  if (!begun_ || plan_.finished || commit_unknown_) {
    wire->clear();
    return Status::Fail("Response cannot accept summary output");
  }
  return RecordOutput(output_.AppendSummary(text, wire), wire);
}

Status ResponsesExecution::RecordOutput(Status status, std::string* wire) {
  if (!status) {
    if (moderation_safe_) wire->clear();
    return status;
  }
  status = Capture(*wire, &events_, &event_bytes_);
  if (!status) {
    if (!moderation_safe_) failure_prefix_ = std::move(*wire);
    wire->clear();
    return status;
  }
  if (moderation_safe_) {
    if (wire->size() >
        ResponsesOutput::kStorageLimit - moderation_wire_.size()) {
      wire->clear();
      return Status::Fail("Moderation output hold capacity exhausted");
    }
    moderation_wire_ += *wire;
    wire->clear();
    return {};
  }
  if (status && plan_.store &&
      (event_bytes_ - checkpoint_bytes_ >= 64 * 1024 ||
       std::chrono::steady_clock::now() - checkpoint_time_ >=
           std::chrono::milliseconds(500))) {
    auto saved = Checkpoint();
    if (!saved.ok()) {
      failure_prefix_ = std::move(*wire);
      wire->clear();
      ObserveError(this, saved.http_status, saved.message, saved.code);
      return Status::Fail(saved.message);
    }
  }
  return status;
}

ObjectReply ResponsesExecution::Finish(bool incomplete, size_t completed_calls,
                                       const ResponsesUsage& usage,
                                       int64_t completed_at, std::string* wire,
                                       bool content_filtered) {
  wire->clear();
  if (!begun_ || !generation_ready_ || plan_.finished || commit_unknown_)
    return Error(500, "response_state_invalid", "Invalid response finish");
  if (moderation_safe_)
    return Error(409, "moderation_review_pending",
                 "Unreviewed output cannot finish");
  // Work on a candidate: a failed transaction must not consume a success
  // sequence number or make the live renderer think it already completed.
  auto candidate = output_;
  auto events = events_;
  size_t bytes = event_bytes_;
  std::string terminal;
  ObjectReply encryption;
  ResponsesReasoningSeal seal;
  if (envelope_.encrypted_reasoning)
    seal = [&](size_t index, std::string* opaque) {
      encryption =
          SealResponsesReasoning(cipher_, generation_history_,
                                 *candidate.Snapshot().GetArray("output"),
                                 index, cipher_config_, access_, opaque);
      return encryption.ok() ? Status() : Status::Fail(encryption.message);
    };
  auto status =
      candidate.Finish(incomplete, completed_calls, usage, completed_at,
                       &terminal, seal, content_filtered);
  if (!encryption.ok()) return encryption;
  if (status) status = Capture(terminal, &events, &bytes);
  if (!status) return OutputError(status);
  // Cancellation and success have one monotonic decision point. A cancel that
  // wins may not be overwritten by a later durable completed response.
  if (background_control_ && !background_control_->Finish())
    return Error(409, "response_cancelled",
                 "Background response was cancelled");
  auto result = BindMutationResult(
      state_.Finish(candidate.Snapshot(), events, access_, &plan_));
  if (!result.ok()) return result;
  output_ = std::move(candidate);
  events_ = std::move(events);
  event_bytes_ = bytes;
  ReleasePreparedInputs();
  *wire = std::move(terminal);
  return {};
}

void ResponsesExecution::ObserveUsage(
    void* owner, const std::optional<ActualUsage>& usage) noexcept {
  auto& run = *static_cast<ResponsesExecution*>(owner);
  run.actual_usage_ = usage && ValidActualUsage(*usage) ? usage : std::nullopt;
}

Status ResponsesExecution::ApplyActualUsage(ResponsesOutput* output) const {
  if (output->Terminal()) return {};
  if (!actual_usage_) {
    // A still-queued original owner has not invoked any model step.
    if (output->Snapshot().GetString("status") == "queued")
      return output->SetUsage({});
    return {};
  }
  const auto& usage = *actual_usage_;
  return output->SetUsage({static_cast<int64_t>(usage.input),
                           static_cast<int64_t>(usage.cached),
                           static_cast<int64_t>(usage.output),
                           static_cast<int64_t>(usage.reasoning),
                           static_cast<int64_t>(usage.cache_write)});
}

void ResponsesExecution::ObserveError(void* owner, int status,
                                      std::string_view message,
                                      std::string_view code) noexcept {
  auto& run = *static_cast<ResponsesExecution*>(owner);
  if (run.failure_status_) return;
  run.failure_status_ = status;
  const auto save = [](auto& destination, std::string_view source) {
    const auto size = std::min(destination.size() - 1, source.size());
    std::memcpy(destination.data(), source.data(), size);
    destination[size] = '\0';
  };
  save(run.failure_code_, code.empty() ? "response_execution_failed" : code);
  save(run.failure_message_, message);
}

std::string ResponsesExecution::RenderStreamError(void* owner, int status,
                                                  const std::string& message,
                                                  std::string_view code,
                                                  std::string_view param) {
  auto& run = *static_cast<ResponsesExecution*>(owner);
  ObserveError(owner, status, message, code);
  run.DiscardUnreviewedOutput();
  std::string prefix = std::move(run.failure_prefix_);
  run.failure_prefix_.clear();
  if (run.commit_unknown_) {
    // The durable decision is unknown; do not falsely advertise failed when
    // retrieval might find a completed transaction.
    const auto event = Object({{"type", String("error")},
                               {"sequence_number", Number((*run.Sequence())++)},
                               {"code", String("object_commit_unknown")},
                               {"message", String(message)},
                               {"param", Json()}});
    prefix += "event: error\r\ndata: " + OutputJson(event) + "\r\n\r\n";
    return run.PublicWire(prefix);
  }
  // The error observer refreshed committed counters before invoking this
  // renderer, so JSON retrieval and response.failed share the same usage.
  (void)run.ApplyActualUsage(&run.output_);
  auto wire = ResponsesOutput::RenderStreamError(&run.output_, status, message,
                                                 code, param);
  // A failed response can still be retrieved when its event journal exceeded
  // its resource budget; no invented events are substituted for the originals.
  if (!run.Capture(wire, &run.events_, &run.event_bytes_))
    run.events_ = Array();
  prefix += wire;
  return run.PublicWire(prefix);
}

ObjectReply ResponsesExecution::CloseUnfinished() {
  DiscardUnreviewedOutput();
  ScopeExit release_cleanup([&] {
    if (cleanup_ticket_) {
      cleanup_->Close(cleanup_ticket_,
                      plan_.finished || !durable_begin_attempted_);
      cleanup_ticket_ = 0;
    }
  });
  if (!begun_ || plan_.finished || commit_unknown_) return {};
  if (!output_.Terminal()) {
    auto usage_status = ApplyActualUsage(&output_);
    if (!usage_status) return OutputError(usage_status);
    const auto code = failure_status_ ? failure_code_.data()
                                      : "response_execution_interrupted";
    const auto message = failure_status_
                             ? detail::NormalizeUtf8(failure_message_.data())
                             : "Response execution ended before completion";
    std::string wire;
    auto status = output_.Fail(code, message, &wire);
    if (!status) return OutputError(status);
    if (!Capture(wire, &events_, &event_bytes_)) events_ = Array();
  }
  StateAccess cleanup{
      access_.scope,
      std::chrono::steady_clock::now() + std::chrono::milliseconds(2000),
      {}};
  auto saved = BindMutationResult(
      state_.Finish(output_.Snapshot(), events_, cleanup, &plan_));
  if (!saved.commit_unknown && (saved.code == "object_too_large" ||
                                saved.code == "object_quota_exceeded"))
    return BindMutationResult(
        state_.FailWithinReservation(output_.Snapshot(), cleanup, &plan_));
  return saved;
}

}  // namespace q4t::server
